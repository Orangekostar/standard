from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from core.backtest.prism_compare_engine import CELL_FILES
from core.backtest.prism_compare_metrics import SUMMARY_COLUMNS, comparison_verdict, paired_bootstrap, test_subperiods
from core.pipeline.prism_compare_config import REPOSITORY_ROOT, STRATEGIES, file_sha256, implementation_identity, write_json
from core.pipeline.prism_compare_data import _verify_cache_file, experiment_lock
from core.technical_v2.contracts import ContractError

ROOT_FILES = ("protocol_frozen.json", "dataset_manifest.json", "data_audit.json", "split.csv", "parameters.json",
    "shared_fixes.md", "baseline_identity.json", "comparison_summary.csv", "paired_bootstrap.json", "verdict.json",
    "regime_summary.csv", "sector_summary.csv", "gate_funnel.csv", "test_subperiods.csv", "RESEARCH_REPORT.md", "TEST_REPORT.md",
    "year_quarter_summary.csv", "exit_summary.csv", "cost_summary.csv", "candidate_label_diagnostic.csv",
    "figures/equity.png", "figures/drawdown.png", "figures/exposure.png")


def _csv(path: Path, **kwargs) -> pd.DataFrame:
    return pd.read_csv(path, dtype={"date": str, "trade_date": str, "entry_date": str, "closed_at": str}, **kwargs)


def _safe_number(value):
    return float(value) if pd.notna(value) and np.isfinite(float(value)) else None


def _cell_tables(directory, matrix):
    tables, metrics, funnels = {}, [], []
    for strategy, split, cost in matrix:
        path = directory / strategy / split / cost
        receipt = json.loads((path / "cell_manifest.json").read_text())
        if receipt["status"] != "COMPLETE":
            raise ContractError("report requires completed cells")
        for name in CELL_FILES:
            record = receipt["files"][name]
            if (path / name).stat().st_size != record["bytes"] or file_sha256(path / name) != record["sha256"]:
                raise ContractError(f"report cell SHA256 mismatch: {path / name}")
        metrics.append(json.loads((path / "metrics.json").read_text()))
        tables[strategy, split, cost] = {name: _csv(path / f"{name}.csv.gz") for name in ("daily_nav", "fills", "closed_trades")}
        funnels.extend({"strategy_id": strategy, "split": split, "cost_scenario": cost, **row}
                       for row in json.loads((path / "gate_funnel.json").read_text()))
    return tables, pd.DataFrame(metrics), pd.DataFrame(funnels)


def _attribution(tables):
    regimes, periods, sectors, exits = [], [], [], []
    for (strategy, split, cost), cell in tables.items():
        common = dict(strategy_id=strategy, split=split, cost_scenario=cost)
        nav, closed = cell["daily_nav"].copy(), cell["closed_trades"].copy()
        values = pd.to_numeric(nav.nav_cents, errors="coerce")
        complete = nav.status.eq("OK").all() and np.isfinite(values).all()
        nav["daily_return"] = values.pct_change(fill_method=None).fillna(0) if complete else np.nan
        nav["year"] = nav.date.str[:4]
        nav["quarter"] = pd.to_datetime(nav.date, format="%Y%m%d").dt.to_period("Q").astype(str)
        for dimension in ("market_state", "year", "quarter"):
            for name, group in nav.groupby(dimension, dropna=False, sort=True):
                row = {**common, "dimension": dimension, "group": str(name), "session_count": len(group),
                    "start_date": group.date.iloc[0], "end_date": group.date.iloc[-1],
                    "conditional_daily_compound": float((1 + group.daily_return).prod() - 1) if complete else None,
                    "average_gross_exposure": _safe_number(group.gross_exposure.mean()) if complete else None,
                    "status": "OK" if complete else "NAV_INCOMPLETE",
                    "definition": "DAILY_RETURNS_ATTRIBUTED_BY_CLOSE_DATE; REGIME_GROUPS_NOT_CONTIGUOUS_PORTFOLIOS"}
                (regimes if dimension == "market_state" else periods).append(row)
        exposure: dict[str, float] = defaultdict(float)
        for raw in nav.sector_exposure:
            for sector, weight in json.loads(raw or "{}").items():
                exposure[sector] += weight / len(nav)
        if closed.empty:
            sectors.append({**common, "sector_id": "UNALLOCATED", "closed_trade_count": 0,
                "net_closed_pnl_cny": 0., "closed_win_rate": None, "mean_entry_sector_exposure": 0., "status": "NO_CLOSED_TRADES"})
            exits.append({**common, "exit_reason": "NONE", "closed_trade_count": 0,
                          "net_closed_pnl_cny": 0., "status": "NO_CLOSED_TRADES"})
        else:
            for sector, group in closed.groupby(closed.sector_id.fillna("UNKNOWN"), sort=True):
                sectors.append({**common, "sector_id": sector, "closed_trade_count": len(group),
                    "net_closed_pnl_cny": float(group.realized_pnl_cents.sum()) / 100,
                    "closed_win_rate": float(group.realized_pnl_cents.gt(0).mean()),
                    "mean_entry_sector_exposure": exposure.pop(sector, 0.), "status": "CLOSED_ECONOMIC_LOTS_BY_ENTRY_SECTOR"})
            for reason, group in closed.groupby(closed.exit_reason.fillna("UNKNOWN"), sort=True):
                exits.append({**common, "exit_reason": reason, "closed_trade_count": len(group),
                    "net_closed_pnl_cny": float(group.realized_pnl_cents.sum()) / 100, "status": "CLOSED_ECONOMIC_LOTS"})
        for sector, weight in sorted(exposure.items()):
            sectors.append({**common, "sector_id": sector, "closed_trade_count": 0,
                "net_closed_pnl_cny": 0., "closed_win_rate": None, "mean_entry_sector_exposure": weight, "status": "NO_CLOSED_TRADES"})
    return {"regime_summary": regimes, "year_quarter_summary": periods, "sector_summary": sectors, "exit_summary": exits}


def _label_diagnostic(root: Path, config: dict[str, Any]) -> pd.DataFrame:
    manifest = json.loads((root / "feature_manifest.json").read_text())
    assignments = pd.read_csv(root / "split.csv", dtype={"signal_date": str})
    allowed = assignments.loc[assignments.signal_allowed] if "signal_allowed" in assignments else assignments
    pieces = []
    for feature, label in zip(manifest.get("feature_chunks", []), manifest.get("label_chunks", [])):
        _verify_cache_file(feature)
        _verify_cache_file(label)
        scores = pd.read_parquet(feature["path"], columns=["code", "date", "score5", "score3", "prediction_status", "overextended"])
        native = config["native_contract"]
        scores = scores.loc[scores.prediction_status.eq("OK") & scores.score5.ge(native["entry_score5_at_least"])
            & scores.score3.ge(native["entry_score3_at_least"]) & scores.overextended.eq(False)]
        scores = scores.merge(allowed[["signal_date", "split"]], left_on="date", right_on="signal_date", how="inner")
        labels = pd.read_parquet(label["path"], columns=["code", "as_of_trade_date", "label_status", "realized_return"])
        selected = scores.merge(labels.loc[labels.label_status.eq("OK")], left_on=["code", "date"],
                                right_on=["code", "as_of_trade_date"], how="inner", validate="one_to_one")
        pieces.append(selected[["split", "date", "realized_return"]])
    rows = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame(columns=["split", "date", "realized_return"])
    result = []
    for split in config["split"]["counts"]:
        group = rows.loc[rows.split.eq(split)]
        values = pd.to_numeric(group.realized_return, errors="coerce").dropna()
        result.append(dict(split=split, candidate_label_count=len(values), candidate_signal_dates=group.date.nunique(),
            mean_adjusted_label=float(values.mean()) if len(values) else None,
            **{f"p{int(q * 100):02d}": float(values.quantile(q)) if len(values) else None for q in (.05, .25, .5, .75, .95)},
            status="ADJUSTED_LABEL_SIGNAL_DIAGNOSTIC_NOT_ACCOUNT_NET_RETURN" if len(values) else
                "NO_RAW_ENTRY_CANDIDATES" if pieces else "NO_LABEL_CACHE",
            condition="ORIGINAL_FLAT_65_55_NOT_OVEREXTENDED; COMMON_ALLOWED_SIGNAL_DATES",
            interpretation="SAME_F0_FOR_ALL_GROUPS; NOT_EVIDENCE_OF_B_DIRECTIONAL_PREDICTION_IMPROVEMENT"))
    return pd.DataFrame(result)


def _figures(directory, tables, config, frozen):
    try:
        import matplotlib
    except ModuleNotFoundError:
        sys.path.insert(0, str(REPOSITORY_ROOT / "cache/experiments/prism_v2/plotting"))
        try:
            import matplotlib
        except ModuleNotFoundError as exc:
            raise ContractError("install isolated plotting dependencies with --target cache/experiments/prism_v2/plotting --no-deps -r configs/prism_v2_plotting_requirements.txt") from exc
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter

    path = directory / "figures"
    path.mkdir(exist_ok=True)
    colors, styles, markers = ("#16766e", "#b64343", "#565656"), ("-", "--", ":"), ("o", "s", "x")
    for name, title in (("equity", "NAV / Initial Capital"), ("drawdown", "Drawdown Loss"), ("exposure", "Actual Gross Exposure")):
        figure, axes = plt.subplots(2, 2, figsize=(13.6, 8.0), constrained_layout=True)
        for r, split in enumerate(config["evaluation"]["splits"]):
            for column, cost in enumerate(config["evaluation"]["cost_cases"]):
                ax = axes[r, column]
                for index, strategy in enumerate(STRATEGIES):
                    nav = tables[strategy, split, cost]["daily_nav"]
                    x = pd.to_datetime(nav.date, format="%Y%m%d")
                    values = pd.to_numeric(nav.nav_cents, errors="coerce")
                    y = values / config["shared_portfolio"]["initial_capital_cny"] / 100 if name == "equity" else \
                        1 - values / values.cummax() if name == "drawdown" else nav.gross_exposure
                    ax.plot(x, y, color=colors[index], linestyle=styles[index], linewidth=1.7,
                            marker=markers[index], markevery=17 + index * 3, markersize=3.8, label="ABC"[index])
                window = frozen["split_plan"]["replay_windows"][split]
                ax.set_title(f"{split.title()} / {cost} / {window['start_date']} - {window['end_date']}", fontsize=10)
                ax.grid(alpha=.22)
                ax.tick_params(axis="x", labelrotation=20, labelsize=8)
                ax.legend(loc="best", frameon=False, ncol=3)
                if name != "equity":
                    ax.yaxis.set_major_formatter(PercentFormatter(1.))
                ax.set_ylabel(title)
        scope = "SCOPE LIMITED" if frozen["scope_flags"] else "AVAILABLE AUDITED SCOPE"
        figure.suptitle(f"{title} | Independent Validation/Test Accounts | {scope}", fontsize=13)
        figure.savefig(path / f"{name}.png", dpi=120, metadata={"Software": f"Matplotlib {matplotlib.__version__}"})
        plt.close(figure)


def _number(value, percent=False):
    numeric = _safe_number(value)
    return "null" if numeric is None else f"{numeric:.2%}" if percent else f"{numeric:.4f}"


def _reports(directory, root, full, frozen, verdict):
    selected = full.loc[full.split.eq("test") & full.cost_scenario.eq("base")]
    lines = ["# Prism / V2 Fixed Historical Comparison", "",
        "| Strategy | Net Return | Drawdown | Sharpe | Mean Exposure | Closed Trades | Traded Dates |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in selected.itertuples():
        lines.append(f"| {row.strategy_id} | {_number(row.net_return, True)} | {_number(row.max_drawdown, True)} | "
                     f"{_number(row.sharpe)} | {_number(row.average_gross_exposure, True)} | {row.closed_trade_count} | {row.traded_dates} |")
    dataset = json.loads((directory / "dataset_manifest.json").read_text())
    audit = json.loads((directory / "data_audit.json").read_text())
    lines.extend(["", f"Verdict: `{verdict['verdict']}`. No universal live-trading superiority is claimed.", "",
        "## Protocol And Data", "", f"SOURCE_COMMIT: `{frozen['source_identity']['source_commit']}`.",
        f"Snapshot SHA256: `{dataset['snapshot']['snapshot_sha256']}`; audited cutoff: `{dataset['as_of']}`.",
        "Scope: " + (", ".join(frozen["scope_flags"]) or "AVAILABLE_SCOPE") + ".",
        f"Old portfolio test: `{audit['old_portfolio_test_executed']}`; old summary exposure: `{audit['old_test_summary_exposure']}`.",
        "No Jev/LLM/API inference, threshold search, production publication, or strategy activation.", "",
        f"C budget multiplier: `{frozen['exposure_control']['lambda_c']}`; `{frozen['exposure_control']['status']}`.",
        "C scales stock/sector/gross/risk caps, never cash or terminal returns; it is not exact risk matching.", "",
        "## Dates And Boundaries", "", "`split.csv` preserves train252/calibration63/validation63/test126 and the common allowed signals.",
        "Train labels mature strictly before calibration. Validation excludes signals whose five-session expiry crosses test.",
        "Each replay contains its full common settlement tail; the two test blocks share one continuous account."])
    for split, window in frozen["split_plan"]["replay_windows"].items():
        lines.append(f"- {split}: {window['start_date']} - {window['end_date']}; {len(window['signal_dates'])} allowed signals.")
    lines.extend(["", "## Interpretation And Attribution", "",
        "A keeps original F0/score/expiry behavior. B adds only market-state quantity scaling, adaptive close-stop management and actual-flat cooldown.",
        "All groups share the same original fifteen factors, three horizon scores, train return bins, legal-unit rules, fills, fees and NAV accounting.",
        "Shared repairs are recorded in `shared_fixes.md`; their effect is not a Prism benefit or an exact native-A reproduction.",
        "`regime_summary.csv` attributes daily account returns by closing market state; non-contiguous state groups are not separate account backtests.",
        "`sector_summary.csv` uses actual lot entry sectors; `exit_summary.csv` uses fully closed economic lots, including bonus descendants and confirmed dividends.",
        "`cost_summary.csv` reports actual costs from independent base/stress replays; no terminal fee subtraction is used.",
        "`year_quarter_summary.csv` and `test_subperiods.csv` are descriptive attribution only, not interval selection.",
        "`gate_funnel.csv` distinguishes cumulative gates, first/all blockers and actual orders/fills/closed lots.",
        "`candidate_label_diagnostic.csv` is fixed five-session adjusted-price signal diagnosis, never account net return or improved B directional prediction.",
        "Zero orders imply null unfilled-order ratio; zero volatility/trades imply null Sharpe/win rate. Profit factor is null without closed-loss denominator.",
        "Turnover is both-side fill notional / mean common-date NAV; daily turnover divides all common sessions.", "",
        "## Evidence", "", "```json", json.dumps(verdict, indent=2), "```", "",
        "`paired_bootstrap.json` uses the same test-session moving blocks for A/B, not independently sampled stock trades.",
        "Confidence intervals describe fixed-history stability, not future returns. Available-universe, action and holdout limitations remain binding.",
        "All detailed cells and three independent figures are listed with bytes/SHA256 in `artifact_manifest.json`.",
        "Raw vendor market databases and shared feature caches remain local; only derived research outputs are eligible for delivery.", ""])
    (directory / "RESEARCH_REPORT.md").write_text("\n".join(lines))
    evidence_path = root / "regression_evidence.json"
    evidence = json.loads(evidence_path.read_text()) if evidence_path.exists() else {"status": "NOT_SUPPLIED", "reason": "No bound regression receipt supplied"}
    if evidence_path.exists():
        (directory / "regression_evidence.json").write_bytes(evidence_path.read_bytes())
    test_lines = ["# Test And Runtime Evidence", "", "## Automated Regression", "", "```json", json.dumps(evidence, indent=2), "```", "",
        "Fixture outcomes are implementation checks, not profitable historical evidence.", "",
        "## Actual Cell Runtime", "", "| Strategy | Split | Cost | Seconds | Peak Process RSS Bytes | API Calls |",
        "| --- | --- | --- | ---: | ---: | ---: |"]
    for row in full.itertuples():
        runtime = json.loads((directory / row.strategy_id / row.split / row.cost_scenario / "runtime.json").read_text())
        test_lines.append(f"| {row.strategy_id} | {row.split} | {row.cost_scenario} | {runtime['elapsed_seconds']:.3f} | "
                          f"{runtime['peak_process_rss_bytes']} | {runtime['api_calls']} |")
    test_lines.extend(["", "## Integrity", "", "All successful cells are hash-verified before reporting; actual source/configuration is frozen before test.",
        "Failed partial attempts are preserved separately; completed matching cells are reused without opening a different strategy search.", ""])
    (directory / "TEST_REPORT.md").write_text("\n".join(test_lines))


def report(experiment_root: str | Path) -> dict[str, Any]:
    root = Path(experiment_root).resolve()
    with experiment_lock(root):
        if not (root / "test_complete.json").exists() or not (root / "freeze_receipt.json").exists():
            raise ContractError("report requires a complete frozen test matrix")
        seal = json.loads((root / "freeze_receipt.json").read_text())
        path = Path(seal["protocol_path"])
        if file_sha256(path) != seal["protocol_sha256"]:
            raise ContractError("report frozen protocol SHA256 differs")
        frozen = json.loads(path.read_text())
        config, directory = frozen["parameters"], Path(frozen["artifact_dir"])
        if implementation_identity()["implementation_hash"] != frozen["binding"]["implementation_hash"]:
            raise ContractError("report implementation differs from the frozen source")
        complete = json.loads((root / "test_complete.json").read_text())
        if complete["status"] != "COMPLETE" or complete["binding"] != frozen["binding"]:
            raise ContractError("report test binding/status differs")
        matrix = [(strategy, split, cost) for strategy in STRATEGIES for split in config["evaluation"]["splits"]
                  for cost in config["evaluation"]["cost_cases"]]
        for strategy, split, cost in matrix:
            name = f"{strategy}/{split}/{cost}"
            if file_sha256(directory / name / "cell_manifest.json") != complete["cell_manifests"][name]:
                raise ContractError("report completed cell manifest SHA256 differs")
        tables, full, funnel = _cell_tables(directory, matrix)
        full[list(SUMMARY_COLUMNS)].to_csv(directory / "comparison_summary.csv", index=False)
        paired = paired_bootstrap(tables[STRATEGIES[0], "test", "base"]["daily_nav"],
                                  tables[STRATEGIES[1], "test", "base"]["daily_nav"], config)
        halves = pd.DataFrame([row for strategy in STRATEGIES for cost in config["evaluation"]["cost_cases"]
            for row in test_subperiods(tables[strategy, "test", cost]["daily_nav"],
                frozen["split_plan"]["replay_windows"]["test"]["signal_dates"], config,
                strategy_id=strategy, cost_scenario=cost)])
        verdict = comparison_verdict(full, paired, halves, config, frozen["scope_flags"])
        write_json(directory / "paired_bootstrap.json", paired)
        write_json(directory / "verdict.json", verdict)
        halves.to_csv(directory / "test_subperiods.csv", index=False)
        funnel.to_csv(directory / "gate_funnel.csv", index=False)
        for name, rows in _attribution(tables).items():
            pd.DataFrame(rows).to_csv(directory / f"{name}.csv", index=False)
        full[["strategy_id", "split", "cost_scenario", "net_return", "fees_total", "modeled_slippage_total",
              "filled_buy_count", "closed_trade_count"]].to_csv(directory / "cost_summary.csv", index=False)
        _label_diagnostic(root, config).to_csv(directory / "candidate_label_diagnostic.csv", index=False)
        _figures(directory, tables, config, frozen)
        _reports(directory, root, full, frozen, verdict)
        names = list(ROOT_FILES)
        names.extend(f"{strategy}/{split}/{cost}/{name}" for strategy, split, cost in matrix for name in (*CELL_FILES, "cell_manifest.json"))
        names.extend(name for name in ("regression_evidence.json", "run_identity.json", "validation_complete.json", "test_started.json", "test_complete.json")
                     if (directory / name).exists())
        manifest = {"status": "COMPLETE", "source_commit": frozen["source_identity"]["source_commit"],
            "protocol_sha256": seal["protocol_sha256"], "cell_count": len(matrix), "api_calls": 0,
            "raw_vendor_database_included": False, "self_hash_excluded": True,
            "files": {name: {"bytes": (directory / name).stat().st_size, "sha256": file_sha256(directory / name)} for name in names}}
        write_json(directory / "artifact_manifest.json", manifest)
        return {"status": "COMPLETE", "artifact_dir": str(directory), "run_id": frozen["run_id"],
                "verdict": verdict["verdict"], "source_commit": frozen["source_identity"]["source_commit"],
                "artifact_manifest_sha256": file_sha256(directory / "artifact_manifest.json")}
