from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Any

import pandas as pd

from core.data.symbols import is_buyable_mainboard_ts_code, is_risk_warning_name
from core.pipeline.prism_compare_config import REPOSITORY_ROOT, file_sha256, implementation_identity, load_config, write_json
from core.pipeline.prism_compare_data import (
    SnapshotAudit, _cache_features, _create_snapshot, _data_identity, _verify_cache_file,
    audit_snapshot, experiment_lock, load_daily_cache, readonly_connection,
)
from core.technical_v2.contracts import ContractError, sha256_json

DEFAULT_CONFIG = REPOSITORY_ROOT / "configs/rank_rotation_3d_v1.json"


def load_rotation_config(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    policy = json.loads(Path(path).read_text())
    if (policy["schema_version"] != "rank-rotation-research.v1" or policy["horizon"] not in {1, 3, 5}
            or policy["max_names"] != 3 or policy["forecast_method"] != "formula"
            or policy["risk_warning_unknown"] not in {"BLOCK_NEW_ENTRIES", "ALLOW_RESEARCH_ONLY"}
            or policy["production_activation"] is not False or policy["fixed_holding_expiry"] is not False
            or policy["end_liquidation"] is not False or policy["cost_cases"] != ["base", "stress"]
            or policy["slot_allocation"] != "PREVIOUS_CLOSE_NAV_DIVIDED_BY_THREE_INCLUDING_BUY_FEES"
            or policy["holding_score"] != "CURRENT_SIGNAL_DAY_SCORE"
            or policy["rotation_comparison"] != "STRICTLY_GREATER"
            or policy["flat_action"] != "HOLD_UNLESS_REPLACED_BY_HIGHER_UP"
            or policy["down_action"] != "SELL_AT_NEXT_ELIGIBLE_OPEN"
            or policy["st_filter"] != "FROZEN_SNAPSHOT_NAME_NOT_HISTORICAL_STATUS"):
        raise ContractError("unsupported rotation research policy")
    config = load_config(REPOSITORY_ROOT / policy["base_config"])
    config["rank_rotation"] = policy
    return config


def filter_research_roster(audit: SnapshotAudit, names: pd.DataFrame) -> SnapshotAudit:
    names = names[["code", "name"]].drop_duplicates("code", keep="last")
    roster = audit.roster.merge(names, on="code", how="left", validate="one_to_one")
    keep = roster.code.map(is_buyable_mainboard_ts_code) & ~roster.name.fillna("").map(is_risk_warning_name)
    excluded = roster.loc[~keep, ["code", "name"]].to_dict("records")
    roster = roster.loc[keep].reset_index(drop=True)
    codes = set(roster.code)
    information = {**audit.audit, "roster_code_count": len(roster), "research_name_exclusions": excluded,
        "scope_flags": sorted(set(audit.audit["scope_flags"]) | {
            "FROZEN_NAME_FILTER_NOT_HISTORICAL_ST_CLASSIFICATION",
            "FROZEN_NAME_FILTER_SELECTION_BIAS", "REUSED_HOLDOUT",
        })}
    return SnapshotAudit(information, audit.sessions, roster,
        audit.memberships.loc[audit.memberships.code.isin(codes)].copy(),
        audit.actions.loc[audit.actions.code.isin(codes)].copy())


def prepare_rotation(source_experiment: str | Path, experiment_root: str | Path,
                     config: dict[str, Any]) -> dict[str, Any]:
    source, root = Path(source_experiment).resolve(), Path(experiment_root).resolve()
    if source == root:
        raise ContractError("rotation must not overwrite its reference experiment")
    prepared_path = root / "rank_dataset_manifest.json"
    previous = json.loads(prepared_path.read_text()) if prepared_path.exists() else None
    if previous is not None and (previous["status"] != "COMPLETE"
            or previous["configuration_sha256"] != sha256_json(config)):
        raise ContractError("completed rotation preparation policy differs; use a new experiment root")
    reference_path = source / "dataset_manifest.json"
    reference = json.loads(reference_path.read_text())
    if reference["status"] != "OK":
        raise ContractError("rotation reference has no complete frozen evaluation windows")
    if file_sha256(source / "market_snapshot.db") != reference["snapshot"]["snapshot_sha256"]:
        raise ContractError("reference snapshot SHA256 differs from frozen evidence")
    with experiment_lock(root):
        snapshot = _create_snapshot(source / "market_snapshot.db", root)
        audit = audit_snapshot(snapshot["snapshot_db"], config)
        with closing(readonly_connection(snapshot["snapshot_db"])) as conn:
            names = pd.read_sql_query(
                "SELECT code,name FROM instrument_versions WHERE valid_from<=? "
                "ORDER BY code,valid_from,observed_at,source_version", conn, params=[audit.audit["as_of"]],
            ).drop_duplicates("code", keep="last")
        audit = filter_research_roster(audit, names)
        if previous is not None:
            if (previous["reference_experiment"] != str(source)
                    or previous["reference_dataset_sha256"] != file_sha256(reference_path)
                    or previous["feature_identity"] != _data_identity(snapshot, audit, config)):
                raise ContractError("completed rotation preparation binding differs; preserve the old namespace")
            for name, key in (("feature_manifest.json", "feature_manifest_sha256"), ("roster.parquet", "roster_sha256"),
                              ("corporate_actions.parquet", "corporate_actions_sha256"), ("data_audit.json", "data_audit_sha256")):
                if file_sha256(root / name) != previous[key]:
                    raise ContractError(f"completed rotation preparation SHA256 differs: {name}")
            cached = json.loads((root / "feature_manifest.json").read_text())
            for record in [*cached["feature_chunks"], *cached["label_chunks"], *cached["replay_chunks"]]:
                _verify_cache_file(record)
            return previous
        write_json(root / "parameters.json", config, immutable=True)
        write_json(root / "data_audit.json", audit.audit, immutable=True)
        audit.roster.to_parquet(root / "roster.parquet", index=False)
        audit.actions.to_parquet(root / "corporate_actions.parquet", index=False)
        features = _cache_features(root, snapshot, audit, config,
                                   workers=min(4, config["resources"]["max_cpu_threads"]))
        windows = {}
        for split in ("validation", "test"):
            window = reference["split_plan"]["replay_windows"][split]
            dates = audit.sessions[audit.sessions.index(window["start_date"]):audit.sessions.index(window["end_date"]) + 1]
            if len(dates) < 2:
                raise ContractError("rotation window needs at least two actual sessions")
            windows[split] = dates
        manifest = {"status": "COMPLETE", "reference_experiment": str(source),
            "reference_dataset_sha256": file_sha256(reference_path), "snapshot": snapshot,
            "configuration_sha256": sha256_json(config), "feature_identity": features["feature_identity"],
            "feature_manifest_sha256": file_sha256(root / "feature_manifest.json"),
            "roster_sha256": file_sha256(root / "roster.parquet"),
            "corporate_actions_sha256": file_sha256(root / "corporate_actions.parquet"),
            "data_audit_sha256": file_sha256(root / "data_audit.json"),
            "roster_count": len(audit.roster), "windows": windows, "scope_flags": audit.audit["scope_flags"],
            "signal_dates": "EVERY_WINDOW_SESSION_EXCEPT_LAST_NO_FIXED_EXPIRY_TAIL",
            "fitting": "NONE_FIXED_F0_WEIGHTS_NO_RETURN_BINS_OR_THRESHOLD_SEARCH", "api_calls": 0}
        write_json(root / "rank_dataset_manifest.json", manifest, immutable=True)
        print(json.dumps({"event": "rotation_prepare_complete", "roster_count": len(audit.roster),
                          "windows": {key: [dates[0], dates[-1], len(dates)] for key, dates in windows.items()}}, ensure_ascii=True), flush=True)
        return manifest


def run_rotation(experiment_root: str | Path, config: dict[str, Any],
                 output_root: str | Path) -> dict[str, Any]:
    from core.backtest.rank_rotation_v2 import replay_rotation

    root, output = Path(experiment_root).resolve(), Path(output_root).resolve()
    if output == root:
        raise ContractError("rotation result directory must be separate from prepared data")
    with experiment_lock(root):
        manifest_path = root / "rank_dataset_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        if manifest["status"] != "COMPLETE" or manifest["configuration_sha256"] != sha256_json(config):
            raise ContractError("rotation prepared data and policy differ")
        for name, key in (("feature_manifest.json", "feature_manifest_sha256"), ("roster.parquet", "roster_sha256"),
                          ("corporate_actions.parquet", "corporate_actions_sha256"), ("data_audit.json", "data_audit_sha256")):
            if file_sha256(root / name) != manifest[key]:
                raise ContractError(f"rotation prepared file SHA256 differs: {name}")
        feature_manifest = json.loads((root / "feature_manifest.json").read_text())
        for record in feature_manifest["replay_chunks"]:
            _verify_cache_file(record)
        identity = implementation_identity()
        binding = {"dataset_sha256": file_sha256(manifest_path), "configuration_sha256": sha256_json(config),
                   "implementation_hash": identity["implementation_hash"]}
        result_path = output / "run_manifest.json"
        if result_path.exists():
            previous = json.loads(result_path.read_text())
            if previous["status"] != "COMPLETE" or previous["binding"] != binding:
                raise ContractError("completed rotation result binding differs; use a new output directory")
            for name, record in previous["files"].items():
                path = output / name
                if path.stat().st_size != record["bytes"] or file_sha256(path) != record["sha256"]:
                    raise ContractError(f"completed rotation result SHA256 differs: {name}")
            return {"status": "COMPLETE", "cells": previous["cells"], "output_root": str(output), "reused": True}
        if output.exists() and any(output.iterdir()):
            raise ContractError("incomplete rotation results are preserved; use a fresh output directory")
        output.mkdir(parents=True, exist_ok=True)
        write_json(output / "frozen_protocol.json", {"binding": binding, "parameters": config,
            "source_identity": identity, "dataset": manifest,
            "interpretation": "RESEARCH_REPLAY_ON_PREVIOUSLY_INSPECTED_HOLDOUT_NOT_A_NEW_UNTOUCHED_TEST"}, immutable=True)
        roster = pd.read_parquet(root / "roster.parquet")
        actions = pd.read_parquet(root / "corporate_actions.parquet")

        def frames(dates):
            for date, frame in load_daily_cache(root, dates):
                yield date, frame.merge(roster[["code", "name"]], on="code", how="left", validate="one_to_one")

        cells = []
        for split in ("validation", "test"):
            dates = manifest["windows"][split]
            if not set(dates).issubset(feature_manifest["sessions"]):
                raise ContractError("rotation evaluation window is outside the cached calendar")
            for cost in config["rank_rotation"]["cost_cases"]:
                result = replay_rotation(frames(dates), sessions=dates, config=config,
                    cost_scenario=cost, output_dir=output / split / cost,
                    run_id=f"rotation-{binding['dataset_sha256'][:12]}", split=split,
                    scope_flags=manifest["scope_flags"], corporate_actions=actions)
                cells.append(result["metrics"])
                print(json.dumps({"event": "rotation_cell_complete", "split": split, "cost": cost,
                    "net_return": result["metrics"]["net_return"], "buys": result["metrics"]["filled_buy_count"],
                    "peak_position_count": result["metrics"]["peak_position_count"]}, ensure_ascii=True), flush=True)
        fields = ["split", "cost_scenario", "start_date", "end_date", "net_return", "max_drawdown", "sharpe",
            "filled_buy_count", "filled_sell_count", "closed_trade_count", "closed_trade_win_rate",
            "average_holding_sessions", "rotation_exit_count", "down_exit_count", "peak_position_count",
            "open_position_count", "fees_total", "modeled_slippage_total", "status", "result_status"]
        pd.DataFrame(cells)[fields].to_csv(output / "summary.csv", index=False)
        _write_report(output, cells, manifest, config)
        files = {str(path.relative_to(output)): {"bytes": path.stat().st_size, "sha256": file_sha256(path)}
                 for path in sorted(output.rglob("*")) if path.is_file() and path.name != "run_manifest.json"}
        write_json(result_path, {"status": "COMPLETE", "binding": binding, "cells": cells, "files": files}, immutable=True)
        return {"status": "COMPLETE", "cells": cells, "output_root": str(output), "reused": False}


def _write_report(output, cells, manifest, config):
    def percent(value):
        return "N/A" if value is None else f"{value * 100:.2f}%"

    lines = ["# Three-Stock Rank Rotation Research Replay", "",
        f"Prediction: fixed F0_BALANCED formula, horizon {config['rank_rotation']['horizon']}; roster: {manifest['roster_count']} stocks.", "",
        "| Window | Cost | Dates | Net Ledger Return | Max Drawdown | Closed Trades | Win Rate | Mean Holding Sessions |",
        "|---|---|---|---:|---:|---:|---:|---:|"]
    for cell in cells:
        holding = "N/A" if cell["average_holding_sessions"] is None else f"{cell['average_holding_sessions']:.2f}"
        lines.append(f"| {cell['split']} | {cell['cost_scenario']} | {cell['start_date']} - {cell['end_date']} | "
            f"{percent(cell['net_return'])} | {percent(cell['max_drawdown'])} | {cell['closed_trade_count']} | "
            f"{percent(cell['closed_trade_win_rate'])} | {holding} |")
    lines += ["", "## Rules", "",
        "- At most three actual securities, including pending corporate share entitlements.",
        "- UP candidates rank by the selected horizon's current score; strictly higher scores replace the weakest comparable holding. Ties retain holdings.",
        "- DOWN exits take priority. FLAT is held unless replaced. Unavailable current predictions are not fabricated as weak scores.",
        "- Close signals execute at the next open. Sales precede purchases; failed sales do not free capacity or provide cash.",
        "- Entry budget is previous close NAV / 3, including buy fees, bounded by actual cash after sales; retained positions are not rebalanced daily.",
        "- No fixed holding expiry, forced end liquidation, return-bin gate, 65/55 score gate, breadth cap, sector cap, ATR budget or overextension gate.",
        "- T+1, dated security rules, observed opening limit bounds, suspensions, commissions and one-time slippage remain enforced.",
        "- A failed buy is not backfilled at that open; candidates are ranked again at the next close. Pending exits retry at eligible opens.",
        "- Validation and test use independent accounts with identical initial capital; costs are rerun with fresh accounts, not subtracted afterwards.",
        "- Evaluation dates match the old windows, but signals continue through the penultimate date because this strategy has no fixed-expiry settlement tail.",
        "", "## Interpretation Boundaries", "",
        "These are research ledger returns, not certified executable or dividend-complete historical total returns.",
        "Frozen snapshot names exclude known ST across the roster and factor context; this is not dated historical ST classification and can introduce selection bias.",
        "Unknown historical ST is allowed only under this separately approved research policy. Production and the archived comparison remain strict.",
        "The holdout has been inspected previously. No fresh out-of-sample claim, parameter search or winner selection is made.",
        "Reported end NAV includes unsold positions valued at the last available close; final sale costs are not incurred for still-open holdings.",
        "Opening sizing/fills are an auction approximation, not an order-book or market-impact model.", "",
        "Scope flags: " + ", ".join(manifest["scope_flags"]), "",
        "Snapshot SHA256: " + manifest["snapshot"]["snapshot_sha256"], ""]
    (output / "RESEARCH_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
