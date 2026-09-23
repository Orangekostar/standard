from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.data.v2_store import V2Store
from core.pipeline.evaluation_v2 import run_fixed_evaluation
from scripts.audit_v2_prompt import audit
from tests.v2.test_cli_v2 import run_cli


class EvaluationRuntimeTest(unittest.TestCase):
    def test_short_history_writes_complete_honest_result_package(self) -> None:
        required = {
            "dataset_manifest.json",
            "data_audit.json",
            "coverage_by_date.csv",
            "coverage_by_sector.csv",
            "factor_ic.csv",
            "factor_missingness.csv",
            "candidate_comparison.csv",
            "probability_metrics.csv",
            "calibration_bins.csv",
            "daily_nav.csv",
            "trades.csv",
            "orders.csv",
            "selection.json",
            "run_manifest.json",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            common = (
                "--mode",
                "demo",
                "--db-path",
                str(root / "demo.db"),
                "--artifact-root",
                str(root / "artifacts"),
            )
            synced = run_cli(
                "sync",
                *common,
                "--history-sessions",
                "140",
                "--as-of",
                "20260922",
            )
            evaluated = run_cli("fit-evaluate", *common, "--protocol", "fixed-v1")
            payload = json.loads(evaluated.stdout)
            run_dir = root / "artifacts" / payload["run_id"]
            selection = json.loads((run_dir / "selection.json").read_text(encoding="utf-8"))
            manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            audit_payload = audit(
                project_root=Path(__file__).resolve().parents[2],
                db_path=root / "demo.db",
                artifact_root=root / "artifacts",
                mode="demo",
                evaluation_run_id=payload["run_id"],
            )

            self.assertEqual({path.name for path in run_dir.iterdir()}, required)
            with patch(
                "core.pipeline.evaluation_v2._coverage_by_date",
                side_effect=AssertionError("completed evaluation must not run again"),
            ):
                replay = run_fixed_evaluation(
                    V2Store(root / "demo.db", data_mode="demo"),
                    root / "artifacts",
                )

        self.assertEqual(synced.returncode, 0, synced.stderr)
        self.assertEqual(evaluated.returncode, 2, evaluated.stderr)
        self.assertEqual(payload["status"], "INSUFFICIENT_HISTORY")
        self.assertEqual(payload["selection_status"], "INSUFFICIENT_HISTORY")
        self.assertFalse(payload["final_test_opened"])
        self.assertEqual(selection["formula"]["status"], "INSUFFICIENT_HISTORY")
        self.assertFalse(manifest["final_test_opened"])
        self.assertEqual(audit_payload["checks"]["evaluation_package"]["status"], "PASS")
        self.assertEqual(
            audit_payload["checks"]["evaluation_package"]["evaluation_status"],
            "INSUFFICIENT_HISTORY",
        )
        self.assertGreater(payload["label_rows"], 0)
        self.assertEqual(replay.run_id, payload["run_id"])
        self.assertEqual(replay.artifact_hashes, payload["artifact_hashes"])


if __name__ == "__main__":
    unittest.main()
