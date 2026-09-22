from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "core" / "background" / "task_rules.py"
SPEC = spec_from_file_location("task_rules_under_test", MODULE_PATH)
TASK_RULES = module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(TASK_RULES)

latest_success_timestamp = TASK_RULES.latest_success_timestamp
should_hide_legacy_timeout_error = TASK_RULES.should_hide_legacy_timeout_error
should_write_error_snapshot = TASK_RULES.should_write_error_snapshot


class TaskRulesTest(unittest.TestCase):
    def test_keep_previous_success_snapshot_on_error(self) -> None:
        self.assertFalse(should_write_error_snapshot({"status": "ok", "generated_at": "2026-04-19T19:00:00"}))
        self.assertTrue(should_write_error_snapshot({"status": "error", "generated_at": "2026-04-19T19:00:00"}))
        self.assertTrue(should_write_error_snapshot(None))

    def test_hide_legacy_timeout_error_when_worker_has_recovered(self) -> None:
        now = datetime(2026, 4, 19, 22, 30, 0)
        task_status = {
            "status": "error",
            "error": "smart_pick 后台任务执行超时（>420s）",
            "updated_at": (now - timedelta(minutes=20)).isoformat(),
        }
        worker_status = {
            "updated_at": (now - timedelta(seconds=30)).isoformat(),
        }
        self.assertTrue(
            should_hide_legacy_timeout_error(
                task_status,
                worker_status,
                now=now,
                worker_stale_seconds=90,
            )
        )

    def test_do_not_hide_recent_or_non_timeout_errors(self) -> None:
        now = datetime(2026, 4, 19, 22, 30, 0)
        recent_timeout = {
            "status": "error",
            "error": "smart_pick 后台任务执行超时（>420s）",
            "updated_at": (now - timedelta(minutes=2)).isoformat(),
        }
        non_timeout = {
            "status": "error",
            "error": "smart_pick 后台任务异常退出，exit_code=1",
            "updated_at": (now - timedelta(minutes=20)).isoformat(),
        }
        healthy_worker = {
            "updated_at": (now - timedelta(seconds=30)).isoformat(),
        }
        self.assertFalse(
            should_hide_legacy_timeout_error(
                recent_timeout,
                healthy_worker,
                now=now,
                worker_stale_seconds=90,
            )
        )
        self.assertFalse(
            should_hide_legacy_timeout_error(
                non_timeout,
                healthy_worker,
                now=now,
                worker_stale_seconds=90,
            )
        )

    def test_latest_success_timestamp_only_uses_success_state(self) -> None:
        task_status_error = {
            "status": "error",
            "updated_at": "2026-04-19T20:12:05",
            "extra": {"finished_at": "2026-04-19T20:12:05"},
        }
        snapshot_ok = {
            "status": "ok",
            "generated_at": "2026-04-19T19:55:00",
        }
        self.assertEqual(
            latest_success_timestamp(task_status_error, snapshot_ok),
            "2026-04-19T19:55:00",
        )
        self.assertEqual(
            latest_success_timestamp(
                {
                    "status": "ok",
                    "updated_at": "2026-04-19T20:12:05",
                    "extra": {"finished_at": "2026-04-19T20:10:00"},
                },
                snapshot_ok,
            ),
            "2026-04-19T20:10:00",
        )
        self.assertEqual(latest_success_timestamp(task_status_error, {"status": "error"}), "")


if __name__ == "__main__":
    unittest.main()
