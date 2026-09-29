from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from core.backtest.strategy_search_v2 import (
    SearchMarket, candidate_grid, choose_candidate, enrich_features, qualification,
    replay_search, search_windows,
)
from core.data.symbols import is_buyable_mainboard_ts_code, is_risk_warning_name
from core.pipeline.prism_compare_config import REPOSITORY_ROOT, file_sha256, implementation_identity, write_json
from core.pipeline.prism_compare_data import REPLAY_COLUMNS, _verify_cache_file, experiment_lock
from core.pipeline.rank_rotation_research import load_rotation_config
from core.technical_v2.contracts import ContractError, sha256_json

DEFAULT_CONFIG = REPOSITORY_ROOT / "configs/strategy_search_36_v1.json"
SEARCH_COLUMNS = list(dict.fromkeys([*REPLAY_COLUMNS, "comparison_open", "F02", "F03", "Q05"]))


def load_search_config(path=DEFAULT_CONFIG):
    policy = json.loads(Path(path).read_text())
    if (policy["schema_version"] != "bounded-strategy-search.v1" or policy["candidate_count"] != 36
            or policy["candidate_grid"] != "FIXED_3_FAMILIES_X_2_LEVELS_X_3_HOLDINGS_X_2_TAKE_PROFITS"
            or policy["cost_cases"] != ["base", "stress"] or policy["max_names"] != 3
            or policy["production_activation"] is not False or policy["jev_api_calls"] is not False
            or policy["positive_return_required_both_costs"] is not True or policy["flat_after_tail_required"] is not True
            or policy["minimum_adv20_cny"] != 50000000 or policy["max_adv_participation"] != .01
            or policy["entry_budget"] != "MIN_PREVIOUS_CLOSE_NAV_DIVIDED_BY_THREE_ACTUAL_CASH_ONE_PERCENT_PRIOR_ADV20"
            or policy["trigger_timing"] != "CLOSE_SIGNAL_NEXT_OPEN_NOT_INTRADAY_HIGH_LOW_TOUCH"
            or policy["return_aggregation"] != "MEAN_INDEPENDENT_EQUAL_CAPITAL_WINDOW_RETURNS_NOT_CONTINUOUS_ACCOUNT_RETURN"
            or policy["no_qualified_policy"] != "ONE_PREDECLARED_DIAGNOSTIC_RECHECK_NO_WINNER"
            or policy["settlement_tail_sessions"] < 11 or policy["min_closed_trades"] < 1
            or policy["min_traded_dates"] < 1 or not 0 < policy["max_drawdown_limit"] <= .15
            or policy["holdout_policy"] != "FREEZE_ONE_SELECTION_BEFORE_LOADING_HOLDOUT_NO_RESELECTION"
            or policy["ranking"] != "QUALIFIED_BASE_WIN_RATE_THEN_NET_RETURN_THEN_LOWER_DRAWDOWN_THEN_ID"):
        raise ContractError("unsupported bounded strategy search protocol")
    config = load_rotation_config(REPOSITORY_ROOT / policy["base_config"])
    config["strategy_search"] = policy
    return config


def summarize_candidate(candidate, cells, *, min_closed=30, min_dates=20, max_drawdown=.15):
    if not cells or any(not cell["split"].startswith("selection_") for cell in cells):
        raise ContractError("selection aggregation cannot consume holdout results")
    records = [(cell["split"], cell["cost_scenario"]) for cell in cells]
    splits = {split for split, _ in records}
    if len(records) != len(set(records)) or set(records) != {(split, cost) for split in splits for cost in ("base", "stress")}:
        raise ContractError("selection aggregation requires matched independent cost/window cells")
    aggregates, reasons = {}, []
    for cost in ("base", "stress"):
        rows = [cell for cell in cells if cell["cost_scenario"] == cost]
        count = sum(cell["closed_trade_count"] for cell in rows)
        wins = sum(cell["winning_closed_trade_count"] for cell in rows)
        complete = all(cell["status"] == "OK" and cell["net_return"] is not None for cell in rows)
        aggregates[cost] = dict(status="OK" if complete else "NAV_INCOMPLETE",
            closed_trade_count=count, winning_closed_trade_count=wins,
            win_rate=wins/count if count else None,
            traded_date_count=sum(cell["traded_date_count"] for cell in rows),
            net_return=sum(cell["net_return"] for cell in rows)/len(rows) if complete else None,
            max_drawdown=max(cell["max_drawdown"] for cell in rows) if complete else None,
            open_position_count=sum(cell["open_position_count"] for cell in rows),
            fees_total=sum(cell["fees_total"] for cell in rows),
            modeled_slippage_total=sum(cell["modeled_slippage_total"] for cell in rows),
            average_holding_sessions=sum((cell["average_holding_sessions"] or 0.)*cell["closed_trade_count"] for cell in rows)/count if count else None,
            unrealized_pnl_cny=sum(cell["unrealized_pnl_cny"] for cell in rows))
        reasons.extend(cost + ":" + reason for reason in qualification(aggregates[cost],
            min_closed=min_closed, min_dates=min_dates, max_drawdown=max_drawdown))
    return {"candidate_id": candidate.candidate_id, **asdict(candidate), **aggregates["base"],
            "stress_win_rate": aggregates["stress"]["win_rate"],
            "stress_net_return": aggregates["stress"]["net_return"],
            "stress_max_drawdown": aggregates["stress"]["max_drawdown"],
            "qualified": not reasons, "rejection_reasons": reasons, "cost_aggregates": aggregates}


def _load_market(root, manifest, start, end):
    features = json.loads((root / "feature_manifest.json").read_text())
    names = pd.read_parquet(root / "roster.parquet")[["code", "name"]]
    pieces = []
    for record in features["feature_chunks"]:
        # Read only through the permitted endpoint; rolling inputs remain strictly backward-looking.
        frame = pd.read_parquet(record["path"], columns=SEARCH_COLUMNS, filters=[("date", "<=", end)])
        frame = enrich_features(frame)
        frame = frame.loc[frame.date.ge(start) & frame.date.le(end)]
        pieces.append(frame)
    panel = pd.concat(pieces, ignore_index=True).merge(names, on="code", how="left", validate="many_to_one")
    market = SearchMarket({str(date): frame for date, frame in panel.groupby("date", sort=True)})
    if any(len(frame) != manifest["roster_count"] for frame in market.frames.values()):
        raise ContractError("search factor coverage differs from the frozen roster")
    return market


def _artifact_records(output):
    return {str(path.relative_to(output)): {"bytes": path.stat().st_size, "sha256": file_sha256(path)}
            for path in sorted(output.rglob("*")) if path.is_file()
            and path.name not in {"run_manifest.json", ".compare.lock"}}


def run_search(experiment_root, config, output_root):
    root, output = Path(experiment_root).resolve(), Path(output_root).resolve()
    if root == output or root in output.parents or output in root.parents:
        raise ContractError("search results must be isolated from the frozen dataset")
    policy = config["strategy_search"]
    manifest_path = root / "rank_dataset_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    base = {key:value for key,value in config.items() if key != "strategy_search"}
    if manifest["status"] != "COMPLETE" or manifest["configuration_sha256"] != sha256_json(base):
        raise ContractError("search reference and frozen base data policy differ")
    for name, key in (("feature_manifest.json", "feature_manifest_sha256"), ("roster.parquet", "roster_sha256"),
            ("corporate_actions.parquet", "corporate_actions_sha256"), ("data_audit.json", "data_audit_sha256")):
        if file_sha256(root / name) != manifest[key]:
            raise ContractError("search source SHA256 differs: " + name)
    features = json.loads((root / "feature_manifest.json").read_text())
    for record in features["feature_chunks"]:
        _verify_cache_file(record)
    if file_sha256(manifest["snapshot"]["snapshot_db"]) != manifest["snapshot"]["snapshot_sha256"]:
        raise ContractError("search frozen snapshot SHA256 differs")
    roster = pd.read_parquet(root / "roster.parquet")
    if (not roster.code.map(is_buyable_mainboard_ts_code).all()
            or roster.name.fillna("").map(is_risk_warning_name).any()):
        raise ContractError("search roster includes excluded securities")
    source = implementation_identity()
    binding = dict(dataset_sha256=file_sha256(manifest_path), configuration_sha256=sha256_json(config),
                   implementation_hash=source["implementation_hash"])
    with experiment_lock(output):
        result_path = output / "run_manifest.json"
        if result_path.exists():
            result = json.loads(result_path.read_text())
            if result["status"] != "COMPLETE" or result["binding"] != binding or _artifact_records(output) != result["files"]:
                raise ContractError("completed search binding or artifacts differ; preserve it and use a new namespace")
            return {**result["result"], "reused": True}
        if any(path.name != ".compare.lock" for path in output.iterdir()):
            raise ContractError("incomplete search is preserved; use a new output namespace")
        windows = search_windows(features["sessions"], manifest["windows"]["test"],
            window_count=policy["selection_window_count"], window_sessions=policy["selection_window_sessions"],
            minimum_context=policy["minimum_context_sessions"])
        if any(len(dates) <= policy["settlement_tail_sessions"]+1 for dates in windows.values()):
            raise ContractError("search windows are too short for the common settlement tail")
        candidates = candidate_grid()
        flags = sorted(set(manifest["scope_flags"]) | {"MULTIPLE_STRATEGY_SELECTION_RESEARCH_ONLY", "REUSED_HOLDOUT"})
        write_json(output / "frozen_protocol.json", dict(binding=binding, parameters=config,
            source_identity=source, windows=windows, candidates=[dict(candidate_id=item.candidate_id, **asdict(item)) for item in candidates],
            scope_flags=flags, selection_return_aggregation=policy["return_aggregation"],
            interpretation="FINITE_CANDIDATE_RESEARCH_NOT_GLOBAL_OPTIMUM_OR_FRESH_UNTOUCHED_HOLDOUT"), immutable=True)
        actions = pd.read_parquet(root / "corporate_actions.parquet")
        selection_dates = [date for name, dates in windows.items() if name != "holdout" for date in dates]
        market = _load_market(root, manifest, selection_dates[0], selection_dates[-1])
        summaries, all_cells = [], []
        run_id = "search-" + binding["dataset_sha256"][:12]
        for index, candidate in enumerate(candidates):
            cells = []
            for split, dates in windows.items():
                if split == "holdout":
                    continue
                for cost in policy["cost_cases"]:
                    result = replay_search(market, sessions=dates, candidate=candidate, config=config,
                        cost_scenario=cost, output_dir=output / "selection" / candidate.candidate_id / split / cost,
                        run_id=run_id, split=split, scope_flags=flags, corporate_actions=actions,
                        tail_sessions=policy["settlement_tail_sessions"])
                    cells.append(result["metrics"])
            summary = summarize_candidate(candidate, cells, min_closed=policy["min_closed_trades"],
                min_dates=policy["min_traded_dates"], max_drawdown=policy["max_drawdown_limit"])
            summaries.append(summary)
            all_cells.extend(cells)
            print(json.dumps(dict(event="search_candidate_complete", completed=index+1, total=len(candidates),
                candidate_id=candidate.candidate_id, win_rate=summary["win_rate"], closed=summary["closed_trade_count"],
                net_return=summary["net_return"], max_drawdown=summary["max_drawdown"], qualified=summary["qualified"])), flush=True)
        del market
        selection = choose_candidate(summaries)
        flat = [{key:value for key,value in row.items() if key not in {"cost_aggregates", "rejection_reasons"}}
                | {"rejection_reasons": "|".join(row["rejection_reasons"])} for row in summaries]
        leaderboard = pd.DataFrame(flat).set_index("candidate_id").loc[selection["ranking"]].reset_index()
        leaderboard.to_csv(output / "leaderboard.csv", index=False)
        write_json(output / "selection_cells.json", all_cells, immutable=True)
        target = selection["chosen_candidate_id"] or selection["diagnostic_candidate_id"]
        write_json(output / "selection_freeze.json", dict(binding=binding, selection=selection,
            frozen_at=datetime.now(timezone.utc).isoformat(), leaderboard_sha256=file_sha256(output / "leaderboard.csv"),
            selection_cells_sha256=file_sha256(output / "selection_cells.json"), holdout_candidate_id=target,
            diagnostic_only=selection["chosen_candidate_id"] is None), immutable=True)
        print(json.dumps(dict(event="search_selection_frozen", chosen=selection["chosen_candidate_id"],
                              diagnostic=selection["diagnostic_candidate_id"], qualified_count=selection["qualified_candidate_count"])), flush=True)
        candidate = next(item for item in candidates if item.candidate_id == target)
        dates = windows["holdout"]
        market = _load_market(root, manifest, dates[0], dates[-1])
        holdout_cells, holdout_rejections = [], []
        for cost in policy["cost_cases"]:
            result = replay_search(market, sessions=dates, candidate=candidate, config=config,
                cost_scenario=cost, output_dir=output / "holdout" / target / cost, run_id=run_id, split="holdout",
                scope_flags=flags, corporate_actions=actions, tail_sessions=policy["settlement_tail_sessions"])
            metrics = result["metrics"]
            holdout_cells.append(metrics)
            holdout_rejections.extend(cost+":"+reason for reason in qualification(metrics,
                min_closed=policy["min_closed_trades"], min_dates=policy["min_traded_dates"], max_drawdown=policy["max_drawdown_limit"]))
            print(json.dumps(dict(event="search_holdout_complete", cost=cost, win_rate=metrics["closed_trade_win_rate"],
                closed=metrics["closed_trade_count"], net_return=metrics["net_return"], max_drawdown=metrics["max_drawdown"])), flush=True)
        verdict = ("NO_QUALIFIED_CANDIDATE" if selection["chosen_candidate_id"] is None else
                   "SELECTION_PASS_HOLDOUT_FAIL" if holdout_rejections else "SELECTION_AND_HOLDOUT_PASS_RESEARCH_ONLY")
        result = dict(status="COMPLETE", verdict=verdict, selection=selection, summaries=summaries,
            holdout_candidate=dict(candidate_id=target, **asdict(candidate)), holdout_cells=holdout_cells,
            holdout_rejections=holdout_rejections, output_root=str(output), api_calls=0, reused=False)
        _write_report(output, result, windows, flags)
        write_json(result_path, dict(status="COMPLETE", binding=binding, files=_artifact_records(output), result=result), immutable=True)
        return result


def _write_report(output, result, windows, flags):
    def pct(value):
        return "N/A" if value is None else f"{value*100:.2f}%"
    selection = result["selection"]
    rows = {row["candidate_id"]:row for row in result["summaries"]}
    lines = ["# Bounded 36-Rule Strategy Search", "", "Verdict: " + result["verdict"], "",
        f"Qualified discovery candidates: {selection['qualified_candidate_count']}/36.",
        "Frozen discovery choice: " + str(selection["chosen_candidate_id"]),
        "Predeclared diagnostic leader: " + selection["diagnostic_candidate_id"], "",
        "## Discovery Leaderboard", "",
        "| Candidate | Qualified | Closed | Win Rate | Mean Window Return | Worst Window Drawdown | Stress Mean Return |",
        "|---|---|---:|---:|---:|---:|---:|"]
    for key in selection["ranking"][:10]:
        row = rows[key]
        lines.append(f"| {key} | {row['qualified']} | {row['closed_trade_count']} | {pct(row['win_rate'])} | "
            f"{pct(row['net_return'])} | {pct(row['max_drawdown'])} | {pct(row['stress_net_return'])} |")
    lines += ["", "## Frozen Candidate Holdout", "",
        "Candidate: " + result["holdout_candidate"]["candidate_id"], "",
        "| Cost | Closed | Win Rate | Net Ledger Return | Max Drawdown | Mean Holding Sessions | Open Positions | Unrealized PnL CNY |",
        "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for cell in result["holdout_cells"]:
        holding = "N/A" if cell["average_holding_sessions"] is None else f"{cell['average_holding_sessions']:.2f}"
        lines.append(f"| {cell['cost_scenario']} | {cell['closed_trade_count']} | {pct(cell['closed_trade_win_rate'])} | "
            f"{pct(cell['net_return'])} | {pct(cell['max_drawdown'])} | {holding} | {cell['open_position_count']} | {cell['unrealized_pnl_cny']:.2f} |")
    lines += ["", "Holdout rejection reasons: " + (", ".join(result["holdout_rejections"]) or "NONE"), "",
        "## Protocol", "",
        "- Fixed 36 candidates: three entry families, two entry strengths, 3/5/10-session planned maximum holdings, 2%/4% close take profit, 3% close stop loss.",
        "- No daily score replacement. At most three actual securities. New entries target previous-close NAV/3 including buy fees, capped by actual cash and 1% of previous signal-day ADV20.",
        "- Common entry gates: real active mainboard stock, known non-suspension, no known ST/name ST, valid factors, known non-STRESS market, ADV20 >= CNY 50 million.",
        "- TREND_PULLBACK: MA20 > MA60 (F03 > 0), positive 60-day momentum (F02 >= 0 or >= 0.25), close/MA20 in [0.98,1.03] or [0.98,1.01], positive daily return >= 0.1% or >= 0.5% and <= 3%, 3-day return <= 2%, drawdown20 in [-12%,-2%]. Rank by native F0 score3.",
        "- REBOUND: 3-day return <= -4% or <= -6%, daily rebound >= 0.5% or >= 1%, drawdown20 in [-20%,-5%] or [-20%,-8%], close > open. Rank by deepest 3-day decline. This is a distinct rule, not an UP forecast, and does not sell merely because F0 is DOWN.",
        "- LOW_FREQ_UP: native forecast_class3 UP, score3 >= 65 or >= 75, not overextended, ATR14/close <= 4% or <= 3%. Rank by score3. DOWN is an additional exit only in this family.",
        "- Stop/target use split-adjusted comparison close against actual entry fill basis; trigger at close and submit for next open. Intraday high/low touches are never treated as realizable fills. Opening gaps and blocked sales can exceed planned stop or holding horizon.",
        "- Sell before buy, T+1, opening price limits, dated quantity rules, fee rounding and one-time slippage reuse the existing ledger engine. Failed sales do not free capacity or provide cash.",
        "- Stop new entries for a common 11-session settlement tail. Any remaining position disqualifies a candidate; its marked unrealized loss is still included in NAV and reported, not hidden from returns.",
        "- Each discovery window/cost has an independent CNY 1 million account. Discovery return is the arithmetic mean of equal-capital window returns, not a claimed continuous-account compounded return. Drawdown is the worst window drawdown; win rate pools winning closed trades divided by all closed trades, not the mean of window percentages.",
        "- Both cost cases must have >=30 closed trades, >=20 traded dates, positive mean window net return, drawdown <=15%, complete NAV and no unsettled positions. Rank qualified rules by standard-cost win rate, return, lower drawdown, then ID.",
        "- Freeze selection and its evidence hashes before loading holdout prices. Run only the frozen choice (or one diagnostic leader if no rule qualifies). No post-holdout reselection, extra threshold trials, paid API or production activation.", "",
        "## Data Limits", "",
        "Historical ST status and corporate-action data are incomplete. Frozen-name filtering can create selection bias. Unknown historical ST is allowed only under the separately approved research scope.",
        "These are raw-price research ledger returns with observed corporate actions only, not certified dividend-complete executable returns.",
        "The original holdout was already inspected. This finite search does not establish a global optimum, fresh untouched out-of-sample performance, adjusted statistical significance or future win-rate guarantee.", "",
        "Scope flags: " + ", ".join(flags), "", "## Windows", ""]
    for name, dates in windows.items():
        lines.append(f"- {name}: {dates[0]} - {dates[-1]}, {len(dates)} actual sessions.")
    (output / "RESEARCH_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
