from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from core.background.precompute_worker import PrecomputeWorker, _build_arg_parser
from core.background.task_rules import dependency_snapshot_matches
from core.background.technical_v2_tasks import (
    TECHNICAL_V2_DEPENDENCIES,
    TECHNICAL_V2_TASKS,
    TechnicalV2TaskRunner,
)


class TechnicalV2WorkerProfileTest(unittest.TestCase):
    def test_profile_contains_only_v2_dag_tasks(self) -> None:
        worker = PrecomputeWorker(profile="technical_v2", max_concurrency=1)

        self.assertEqual(tuple(worker.task_specs), TECHNICAL_V2_TASKS)
        self.assertEqual(worker.scheduled_slots, ((9, 0), (16, 10), (20, 10)))
        self.assertNotIn("smart_pick", worker.task_specs)
        for task_name, dependencies in TECHNICAL_V2_DEPENDENCIES.items():
            self.assertEqual(worker.task_specs[task_name].dependencies, dependencies)

    def test_cli_parser_accepts_technical_v2_profile(self) -> None:
        args = _build_arg_parser().parse_args(["--profile", "technical_v2", "--once"])

        self.assertEqual(args.profile, "technical_v2")
        self.assertTrue(args.once)

    def test_technical_schedule_uses_minute_level_slots(self) -> None:
        worker = PrecomputeWorker(profile="technical_v2", max_concurrency=1)

        before = worker._latest_scheduled_slot(
            pd.Timestamp("2026-09-22 16:09:59"), worker.scheduled_slots
        )
        at_slot = worker._latest_scheduled_slot(
            pd.Timestamp("2026-09-22 16:10:00"), worker.scheduled_slots
        )

        self.assertEqual(before, pd.Timestamp("2026-09-22 09:00:00"))
        self.assertEqual(at_slot, pd.Timestamp("2026-09-22 16:10:00"))

    def test_dependency_requires_same_run_asof_and_data_hash(self) -> None:
        expected = {
            "run_id": "run-new",
            "as_of_trade_date": "20260922",
            "data_hash": "hash-new",
        }
        matching = {
            "status": "OK",
            "payload": {
                **expected,
                "artifact_id": "artifact-a",
            },
        }
        stale = {
            "status": "OK",
            "payload": {
                **expected,
                "as_of_trade_date": "20260921",
                "artifact_id": "artifact-old",
            },
        }

        self.assertTrue(dependency_snapshot_matches(matching, expected))
        self.assertFalse(dependency_snapshot_matches(stale, expected))
        self.assertFalse(dependency_snapshot_matches({"status": "PARTIAL", "payload": {}}, expected))

    def test_worker_blocks_stale_dependency_snapshot(self) -> None:
        worker = PrecomputeWorker(profile="technical_v2", max_concurrency=1)
        spec = worker.task_specs["features_v2"]
        expected = {
            "run_id": "run-new",
            "as_of_trade_date": "20260922",
            "data_hash": "hash-new",
        }
        stale = {
            "status": "OK",
            "payload": {
                **expected,
                "as_of_trade_date": "20260921",
                "artifact_id": "artifact-old",
            },
        }
        matching = {
            "status": "OK",
            "payload": {**expected, "artifact_id": "artifact-new"},
        }

        with mock.patch("core.background.precompute_worker.read_snapshot", return_value=stale):
            self.assertTrue(worker._task_dependency_blocked(spec, expected))
        with mock.patch("core.background.precompute_worker.read_snapshot", return_value=matching):
            self.assertFalse(worker._task_dependency_blocked(spec, expected))

    def test_task_progress_is_derived_from_completed_entities(self) -> None:
        runner = TechnicalV2TaskRunner(
            {
                "features_v2": lambda params, force: {
                    "status": "PARTIAL",
                    "completed_entities": 25,
                    "total_entities": 100,
                }
            }
        )

        result = runner.dispatch(
            "features_v2",
            {
                "run_id": "run-a",
                "as_of_trade_date": "20260922",
                "data_hash": "hash-a",
            },
            False,
        )

        self.assertEqual(result["progress_pct"], 25)

    def test_root_sync_task_can_create_its_own_artifact_binding(self) -> None:
        runner = TechnicalV2TaskRunner(
            {
                "data_v2_sync": lambda params, force: {
                    "status": "OK",
                    "run_id": "run-root",
                    "as_of_trade_date": "20260922",
                    "data_hash": "hash-root",
                    "completed_entities": 1,
                    "total_entities": 1,
                }
            }
        )

        result = runner.dispatch("data_v2_sync", {"mode": "demo"}, False)

        self.assertEqual(result["run_id"], "run-root")
        self.assertEqual(result["data_hash"], "hash-root")
        self.assertEqual(result["progress_pct"], 100)

    def test_root_sync_prerequisite_failure_remains_typed_without_binding(self) -> None:
        runner = TechnicalV2TaskRunner(
            {
                "data_v2_sync": lambda params, force: {
                    "status": "PREREQUISITE_MISSING",
                    "code": "TUSHARE_TOKEN_MISSING",
                    "message": "TUSHARE_TOKEN is required",
                    "completed_entities": 0,
                    "total_entities": 1,
                }
            }
        )

        result = runner.dispatch("data_v2_sync", {"mode": "real"}, False)

        self.assertEqual(result["status"], "PREREQUISITE_MISSING")
        self.assertEqual(result["code"], "TUSHARE_TOKEN_MISSING")
        self.assertEqual(result["run_id"], "")
        self.assertEqual(result["progress_pct"], 0)

    def test_default_runner_executes_demo_sync_and_formula_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            params = {
                "mode": "demo",
                "db_path": str(root / "demo.db"),
                "artifact_root": str(root / "artifacts"),
                "as_of": "20260922",
                "history_sessions": 90,
                "seed": 20260923,
            }
            runner = TechnicalV2TaskRunner()

            synced = runner.dispatch("data_v2_sync", params, False)
            bound = {
                **params,
                **{key: synced[key] for key in ("run_id", "as_of_trade_date", "data_hash")},
                "dependency_artifacts": {"data_v2_sync": synced["artifact_id"]},
            }
            features = runner.dispatch("features_v2", bound, False)
            formula = runner.dispatch("formula_v2", bound, False)
            jev = runner.dispatch("jev_v2", bound, False)
            paper = runner.dispatch("paper_v2", bound, False)
            matured = runner.dispatch("evaluate_matured_v2", bound, False)
            published = runner.dispatch("publish_technical_v2", bound, False)

        self.assertEqual(synced["status"], "OK")
        self.assertEqual(features["status"], "OK")
        self.assertEqual(formula["status"], "OK")
        self.assertEqual(jev["status"], "OK")
        self.assertEqual(jev["runtime_status"], "PARTIAL")
        self.assertEqual(paper["status"], "OK")
        self.assertEqual(matured["status"], "OK")
        self.assertEqual(published["status"], "OK")
        self.assertEqual(formula["run_id"], synced["run_id"])
        self.assertEqual(published["data_hash"], synced["data_hash"])
        self.assertNotEqual(formula.get("code"), "TASK_HANDLER_NOT_CONFIGURED")


if __name__ == "__main__":
    unittest.main()
