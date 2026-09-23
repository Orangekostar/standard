from __future__ import annotations

import unittest
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


if __name__ == "__main__":
    unittest.main()
