from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from core.data.v2_store import V2Store
from scripts.v2 import (
    REQUIRED_COMMANDS,
    build_parser,
    resolve_latest_as_of,
    verify_artifact_manifest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(sys.executable)


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged.update(env or {})
    return subprocess.run(
        [str(PYTHON), "-m", "scripts.v2", *args],
        cwd=PROJECT_ROOT,
        env=merged,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


class CliV2Test(unittest.TestCase):
    def test_parser_exposes_every_required_command(self) -> None:
        parser = build_parser()
        subparsers = next(action for action in parser._actions if action.dest == "command")

        self.assertEqual(set(subparsers.choices), set(REQUIRED_COMMANDS))

    def test_demo_is_deterministic_and_network_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            args = ("demo", "--seed", "20260923", "--artifact-root", str(root / "artifacts"))
            env = {
                "TUSHARE_TOKEN": "must-not-be-used",
                "TYPESAFE_API_KEY": "must-not-be-used",
                "DEMO_V2_ROOT": str(root / "demo"),
            }

            first = run_cli(*args, env=env)
            second = run_cli(*args, env=env)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        first_json = json.loads(first.stdout)
        second_json = json.loads(second.stdout)
        self.assertEqual(first_json["artifact_hash"], second_json["artifact_hash"])
        self.assertEqual(first_json["publication_id"], second_json["publication_id"])
        self.assertEqual(first_json["data_source_mode"], "demo")
        self.assertEqual(first.stderr, "")

    def test_real_doctor_missing_keys_returns_prerequisite_exit_and_one_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_cli(
                "doctor",
                "--mode",
                "real",
                "--db-path",
                str(Path(tmpdir) / "missing.db"),
                env={"TUSHARE_TOKEN": "", "TYPESAFE_API_KEY": ""},
            )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "PREREQUISITE_MISSING")
        self.assertIn("TUSHARE_TOKEN", payload["missing"])
        self.assertIn("TYPESAFE_API_KEY", payload["missing"])
        self.assertEqual(result.stdout.count("\n"), 1)

    def test_latest_resolver_separates_available_from_expected_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = V2Store(Path(tmpdir) / "market.db", data_mode="test")
            store.migrate()
            store.upsert_calendar(
                pd.DataFrame(
                    [
                        {
                            "exchange": "SSE",
                            "date": date,
                            "is_open": 1,
                            "source": "fixture",
                            "source_version": "v1",
                        }
                        for date in ("20260921", "20260922", "20260923")
                    ]
                )
            )
            daily = pd.DataFrame(
                [
                    {
                        "code": "600000.SH",
                        "date": "20260922",
                        "source_version": "fixture-v1",
                        "open": 10,
                        "high": 10,
                        "low": 10,
                        "close": 10,
                        "pre_close": 10,
                        "pct_chg": 0,
                        "volume_raw": 1,
                        "volume_raw_unit": "lot",
                        "amount_raw": 1,
                        "amount_raw_unit": "thousand_cny",
                        "volume_shares": 100,
                        "amount_cny": 1000,
                        "source": "fixture",
                        "retrieved_at": "2026-09-22T16:10:00+08:00",
                        "completeness": "COMPLETE",
                        "data_source_mode": "test",
                    }
                ]
            )
            store.upsert_daily_raw(daily)

            store.upsert_sync_audits(
                [
                    {
                        "exchange": "SSE",
                        "date": "20260922",
                        "source_version": "fixture-sync-v1",
                        "expected_instruments": 1,
                        "observed_rows": 1,
                        "known_non_trading_rows": 0,
                        "coverage": 1.0,
                        "status": "COMPLETE",
                        "details": {},
                    },
                    {
                        "exchange": "SSE",
                        "date": "20260923",
                        "source_version": "fixture-sync-v1",
                        "expected_instruments": 1,
                        "observed_rows": 0,
                        "known_non_trading_rows": 0,
                        "coverage": 0.0,
                        "status": "PARTIAL",
                        "details": {},
                    },
                ]
            )

            resolved = resolve_latest_as_of(
                store,
                now=pd.Timestamp("2026-09-23T16:11:00", tz="Asia/Shanghai"),
            )

        self.assertEqual(resolved["actual_as_of"], "20260922")
        self.assertEqual(resolved["expected_as_of"], "20260923")
        self.assertEqual(resolved["freshness_status"], "STALE")

    def test_stale_paper_command_cannot_create_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = run_cli(
                "paper",
                "--mode",
                "test",
                "--db-path",
                str(Path(tmpdir) / "missing.db"),
                "--artifact-root",
                str(Path(tmpdir) / "artifacts"),
            )

        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "PREREQUISITE_MISSING")
        self.assertEqual(payload["orders_created"], 0)

    def test_demo_sync_then_formula_analysis_computes_real_features(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "demo.db"
            artifact_root = root / "artifacts"
            common = (
                "--mode",
                "demo",
                "--db-path",
                str(db_path),
                "--artifact-root",
                str(artifact_root),
            )

            synced = run_cli(
                "sync",
                *common,
                "--history-sessions",
                "90",
                "--as-of",
                "20260922",
                "--seed",
                "20260923",
            )
            analyzed = run_cli(
                "analyze",
                *common,
                "--as-of",
                "20260922",
                "--methods",
                "formula",
            )
            reanalyzed = run_cli(
                "analyze",
                *common,
                "--as-of",
                "20260922",
                "--methods",
                "formula",
            )
            appended = run_cli(
                "analyze",
                *common,
                "--as-of",
                "20260922",
                "--methods",
                "formula,jev",
            )

            analysis_payload = json.loads(analyzed.stdout)
            repeated_payload = json.loads(reanalyzed.stdout)
            run_id = analysis_payload["run_id"]
            coverage_path = next((artifact_root / run_id / "coverage").glob("*.json"))
            coverage_rows = json.loads(coverage_path.read_text(encoding="utf-8"))["rows"]
            store = V2Store(db_path, data_mode="demo")
            persisted_predictions = store.read_prediction_rows(run_id=run_id)
            persisted_features = store.read_feature_rows(run_id=run_id)
            persisted_runs = store.read_analysis_runs()

        self.assertEqual(synced.returncode, 0, synced.stderr)
        self.assertEqual(analyzed.returncode, 0, analyzed.stderr)
        self.assertEqual(reanalyzed.returncode, 0, reanalyzed.stderr)
        self.assertEqual(appended.returncode, 0, appended.stderr)
        payload = json.loads(analyzed.stdout)
        appended_payload = json.loads(appended.stdout)
        self.assertEqual(repeated_payload["run_id"], payload["run_id"])
        self.assertEqual(repeated_payload["publication_id"], payload["publication_id"])
        self.assertEqual(appended_payload["run_id"], payload["run_id"])
        self.assertNotEqual(appended_payload["publication_id"], payload["publication_id"])
        self.assertEqual(payload["formula_status"], "OK")
        self.assertEqual(payload["formula_valid_rows"], 21)
        self.assertEqual(payload["feature_rows"], 6)
        self.assertEqual(payload["sector_feature_rows"], 1)
        self.assertEqual(payload["coverage_rows"], 42)
        self.assertEqual(payload["jev_status"], "NOT_REQUESTED")
        self.assertEqual(appended_payload["jev_status"], "DEMO_ANALYZE_NETWORK_DISABLED")
        self.assertEqual(len(coverage_rows), 42)
        self.assertEqual({row["entity_type"] for row in coverage_rows}, {"stock", "sector"})
        required = {
            "schema_version",
            "run_id",
            "information_cutoff",
            "earliest_entry_date",
            "target_definition_version",
            "feature_coverage",
            "evidence_status",
            "factor_values",
            "risk_metrics",
            "action_blockers",
            "order_status",
        }
        self.assertTrue(required.issubset(coverage_rows[0]))
        self.assertEqual(len(persisted_predictions), 42)
        self.assertGreaterEqual(len(persisted_features), 131)
        self.assertEqual(persisted_runs["run_id"].tolist(), [run_id])

    def test_demo_sync_only_fetches_missing_sessions_unless_forced(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            common = (
                "--mode",
                "demo",
                "--db-path",
                str(root / "demo.db"),
                "--artifact-root",
                str(root / "artifacts"),
                "--history-sessions",
                "5",
                "--as-of",
                "20260922",
            )
            first = run_cli("sync", *common)
            second = run_cli("sync", *common)
            forced = run_cli("sync", *common, "--force-refresh")

        self.assertEqual((first.returncode, second.returncode, forced.returncode), (0, 0, 0))
        self.assertEqual(json.loads(first.stdout)["sessions_fetched"], 5)
        self.assertEqual(json.loads(second.stdout)["sessions_fetched"], 0)
        self.assertEqual(json.loads(second.stdout)["daily_rows"], 0)
        self.assertEqual(json.loads(forced.stdout)["sessions_fetched"], 5)

    def test_release_manifest_detects_changed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir)
            report = run_dir / "TEST_REPORT.md"
            report.write_text("verified\n", encoding="utf-8")
            digest = hashlib.sha256(report.read_bytes()).hexdigest()
            (run_dir / "artifact_manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": "technical-v2-artifact-manifest.v1",
                        "run_id": "run-1",
                        "files": [
                            {
                                "path": "TEST_REPORT.md",
                                "size_bytes": report.stat().st_size,
                                "sha256": digest,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(verify_artifact_manifest(run_dir, "run-1"), [])
            report.write_text("changed\n", encoding="utf-8")

            errors = verify_artifact_manifest(run_dir, "run-1")

        self.assertTrue(any("sha256" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
