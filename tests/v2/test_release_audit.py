from __future__ import annotations

import unittest
from pathlib import Path

from core.background.snapshot_store import read_latest_manifest
from scripts.v2 import verify_artifact_manifest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "technical_v2"
RUN_ID = "demo-20260922-480fdd78cb43"


class ReleaseArtifactAuditTest(unittest.TestCase):
    def test_required_artifacts_exist_and_match_manifest_hashes(self) -> None:
        run_dir = ARTIFACT_ROOT / RUN_ID
        required = {
            "run_manifest.json",
            "selection.json",
            "summary_metrics.csv",
            "coverage_summary.csv",
            "TEST_REPORT.md",
            "artifact_manifest.json",
        }

        missing = sorted(name for name in required if not (run_dir / name).is_file())
        mismatches = verify_artifact_manifest(run_dir, RUN_ID)
        latest = read_latest_manifest(ARTIFACT_ROOT)

        self.assertEqual(missing, [])
        self.assertEqual(mismatches, [])
        self.assertIsNotNone(latest)
        self.assertEqual(latest["run_id"], RUN_ID)
        self.assertEqual(latest["data_source_mode"], "demo")


if __name__ == "__main__":
    unittest.main()
