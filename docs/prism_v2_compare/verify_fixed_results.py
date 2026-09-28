"""Verify the observed all-cash fixed run; this is not a generic profit validator."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[variable] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PIL import Image, ImageStat

from core.backtest.prism_compare_engine import CELL_FILES
from core.backtest.prism_compare_metrics import SUMMARY_COLUMNS
from core.pipeline.prism_compare_config import STRATEGIES, file_sha256, implementation_identity, write_json
from core.pipeline.prism_compare_report import ROOT_FILES
from core.technical_v2.contracts import sha256_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    root = args.artifact_dir.resolve()
    manifest = json.loads((root / "artifact_manifest.json").read_text())
    frozen = json.loads((root / "protocol_frozen.json").read_text())
    dataset = json.loads((root / "dataset_manifest.json").read_text())
    config = json.loads((root / "parameters.json").read_text())
    complete = json.loads((root / "test_complete.json").read_text())
    opened = json.loads((root / "test_started.json").read_text())
    assert manifest["status"] == complete["status"] == "COMPLETE"
    assert manifest["cell_count"] == complete["cell_count"] == 12
    assert manifest["raw_vendor_database_included"] is False
    assert manifest["api_calls"] == frozen["api_calls"] == 0
    assert manifest["source_commit"] == frozen["source_identity"]["source_commit"] == opened["source_commit"]
    assert file_sha256(root / "protocol_frozen.json") == manifest["protocol_sha256"] == complete["protocol_sha256"] == opened["protocol_sha256"]
    assert datetime.fromisoformat(opened["opened_at"]) > datetime.fromisoformat(frozen["frozen_at"])
    assert not frozen["test_opened_before_freeze"] and frozen["threshold_search_trials"] == 0
    assert frozen["binding"] == complete["binding"]
    assert implementation_identity()["implementation_hash"] == frozen["binding"]["implementation_hash"]
    assert sha256_json(config) == dataset["configuration_sha256"] == frozen["binding"]["configuration_sha256"]
    assert file_sha256(root / "data_audit.json") == frozen["binding"]["data_audit_sha256"]
    assert file_sha256(root / "dataset_manifest.json") == frozen["binding"]["dataset_sha256"]
    assert file_sha256(root / "split.csv") == frozen["binding"]["split_sha256"]
    assert set(ROOT_FILES).issubset(manifest["files"])
    for name, record in manifest["files"].items():
        path = root / name
        assert not path.is_symlink() and path.resolve().is_relative_to(root)
        assert path.stat().st_size == record["bytes"] and file_sha256(path) == record["sha256"], name
    with (root / "comparison_summary.csv").open() as handle:
        reader = csv.DictReader(handle)
        assert tuple(reader.fieldnames) == SUMMARY_COLUMNS
        summary = list(reader)
    matrix = {(s, p, c) for s in STRATEGIES for p in ("validation", "test") for c in ("base", "stress")}
    assert len(summary) == 12 and {(r["strategy_id"], r["split"], r["cost_scenario"]) for r in summary} == matrix
    control = frozen["exposure_control"]
    assert control["lambda_c"] == 1 and control["status"] == "UNIDENTIFIABLE_ZERO_BASE_EXPOSURE"
    assert not control["test_returns_used"] and not control["stress_refits_lambda"]
    score_hashes, cell_evidence, account_ids = {}, [], set()
    total_decisions = 0
    for strategy, split, cost in sorted(matrix):
        cell = root / strategy / split / cost
        key = f"{strategy}/{split}/{cost}"
        receipt = json.loads((cell / "cell_manifest.json").read_text())
        assert file_sha256(cell / "cell_manifest.json") == complete["cell_manifests"][key]
        assert set(CELL_FILES) == set(receipt["files"])
        assert receipt["binding"]["implementation_hash"] == frozen["binding"]["implementation_hash"]
        assert receipt["binding"]["feature_identity"] == dataset["feature_identity"]
        assert receipt["source_identity"]["source_commit"] == manifest["source_commit"]
        metrics = json.loads((cell / "metrics.json").read_text())
        runtime = json.loads((cell / "runtime.json").read_text())
        assert runtime["api_calls"] == 0 and runtime["status"] == "COMPLETE"
        assert metrics["accounting_status"] == "OK" and metrics["unresolved_nav_days"] == 0
        assert metrics["comparison_status"] == "INSUFFICIENT_TRADING_EVIDENCE"
        for field in ("net_return", "max_drawdown", "average_gross_exposure", "fees_total", "modeled_slippage_total",
                      "filled_buy_count", "closed_trade_count", "traded_dates", "open_position_count", "unresolved_asset_count"):
            assert metrics[field] == 0, (key, field)
        for field in ("sharpe", "closed_trade_win_rate", "profit_factor", "unfilled_order_ratio"):
            assert metrics[field] is None, (key, field)
        assert metrics["initial_nav"] == metrics["final_nav"] == config["shared_portfolio"]["initial_capital_cny"]
        with gzip.open(cell / "daily_nav.csv.gz", "rt") as handle:
            nav = list(csv.DictReader(handle))
        dates = [row["date"] for row in nav]
        window = frozen["split_plan"]["replay_windows"][split]
        assert len(nav) == runtime["session_count"] == window["common_session_count"]
        assert dates[0] == window["start_date"] and dates[-1] == window["end_date"]
        assert dates == sorted(set(dates))
        initial_cents = round(metrics["initial_nav"] * 100)
        for row in nav:
            assert row["status"] == "OK" and row["accounting_reconciled"] == "True"
            assert int(row["nav_cents"]) == int(row["cash_cents"]) == initial_cents
            assert int(row["position_value_cents"]) == int(row["receivable_cents"]) == 0
        with sqlite3.connect((cell / "account.db").resolve().as_uri() + "?mode=ro", uri=True) as conn:
            accounts = conn.execute("SELECT account_id FROM paper_accounts").fetchall()
            assert len(accounts) == 1
            account_ids.add(accounts[0][0])
            assert conn.execute("SELECT SUM(amount_cents) FROM paper_cash_ledger").fetchone()[0] == initial_cents
            assert conn.execute("SELECT COUNT(*) FROM paper_valuations").fetchone()[0] == len(nav)
            for table in ("paper_orders", "paper_fills", "paper_lots"):
                assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'paper_%' AND name NOT LIKE 'sqlite_%' AND name!='schema_migrations'").fetchall()
            assert all(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] == 0 for (name,) in tables)
        count, candidates, edge_pass, risk_blocked = Counter(), 0, 0, 0
        digest = hashlib.sha256()
        previous_date, previous_code = None, ""
        signal_dates = set(window["signal_dates"])
        with gzip.open(cell / "decisions.csv.gz", "rt") as handle:
            for row in csv.DictReader(handle):
                date, code = row["date"], row["code"]
                if date != previous_date:
                    previous_date, previous_code = date, ""
                assert code > previous_code
                previous_code = code
                count[date] += 1
                assert (row["signal_enabled"] == "True") == (date in signal_dates)
                digest.update(("\0".join(row[field] for field in ("date", "code", "score1", "score3", "score5", "prediction_status")) + "\n").encode())
                if row["entry_candidate"] == "True":
                    candidates += 1
                    edge_pass += bool(row["reference_net_edge"]) and float(row["reference_net_edge"]) > config["shared_portfolio"]["net_edge_min_exclusive"]
                risk_blocked += "HISTORICAL_RISK_WARNING_UNKNOWN" in row["all_blockers"]
        assert list(count) == dates and all(n == dataset["roster_count"] for n in count.values())
        assert candidates == metrics["rejected_candidate_count"] and edge_pass == 0
        assert risk_blocked == sum(count.values())
        signature = digest.hexdigest()
        assert score_hashes.setdefault(split, signature) == signature
        total_decisions += sum(count.values())
        cell_evidence.append({"cell": key, "nav_sessions": len(nav), "decision_rows": sum(count.values()),
            "candidate_rows": candidates, "candidate_net_edge_pass": edge_pass,
            "cash_ledger_reconciled": True, "vendor_market_tables_empty": True})
    assert len(account_ids) == 12
    images = {}
    for name in ("equity", "drawdown", "exposure"):
        with Image.open(root / "figures" / f"{name}.png") as picture:
            assert picture.width >= 1000 and picture.height >= 600
            deviation = ImageStat.Stat(picture.convert("RGB")).stddev
            assert max(deviation) > 3
            images[name] = {"pixels": [picture.width, picture.height], "rgb_stddev": deviation}
    verdict = json.loads((root / "verdict.json").read_text())
    assert verdict["verdict"] == "INSUFFICIENT_TRADING_EVIDENCE"
    assert verdict["accounting_valid"] and verdict["scope_limited"] and not verdict["sample_sufficient"]
    assert not verdict["evidence_supports_historical_leader"]
    evidence = {"status": "VERIFIED_FIXED_ALL_CASH_RUN", "run_id": frozen["run_id"],
        "source_commit": manifest["source_commit"], "artifact_manifest_sha256": file_sha256(root / "artifact_manifest.json"),
        "hash_verified_file_count": len(manifest["files"]), "cell_count": 12, "isolated_accounts": len(account_ids),
        "decision_rows": total_decisions, "raw_score_sha256_by_split": score_hashes,
        "protocol_before_test": True, "no_test_exposed_code_changes": True, "images": images, "cells": cell_evidence}
    if args.evidence:
        write_json(args.evidence, evidence)
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    main()
