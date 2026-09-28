from __future__ import annotations

import hashlib
import json
import random
import tempfile
import unittest
from pathlib import Path

from core.pipeline.prism_compare_config import file_sha256, write_json
from core.technical_v2.contracts import ContractError


class PrismDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.report = self.root / "report"
        (self.report / "figures").mkdir(parents=True)
        write_json(self.report / "parameters.json", {"delivery": {"max_archive_part_mib": 40}})
        self.payload = random.Random(17).randbytes(1100000)
        (self.report / "figures/equity.png").write_bytes(self.payload)
        self.records = {name: {"bytes": (self.report / name).stat().st_size,
            "sha256": file_sha256(self.report / name)} for name in ("parameters.json", "figures/equity.png")}
        self.manifest = self.report / "artifact_manifest.json"
        write_json(self.manifest, {"status": "COMPLETE", "source_commit": "UNIT_FIXTURE_ONLY",
                   "raw_vendor_database_included": False, "files": self.records})

    def test_parts_obey_limit_and_restoration_checks_original_bytes_and_reuses_success(self):
        from core.pipeline.prism_compare_delivery import package, restore
        result = package(self.report, self.root / "packages", part_mib=1)
        self.assertEqual(result["status"], "PACKAGED")
        path = Path(result["manifest_path"])
        mtime = path.stat().st_mtime_ns
        receipt = json.loads(path.read_text())
        self.assertEqual(receipt["part_bytes_limit"], 1048576)
        self.assertEqual(len(receipt["parts"]), 2)
        for record in receipt["parts"]:
            part = path.parent / record["file"]
            self.assertLessEqual(part.stat().st_size, 1048576)
            self.assertEqual(hashlib.sha256(part.read_bytes()).hexdigest(), record["sha256"])
        restored = self.root / "restored"
        verified = restore(path, restored)
        self.assertEqual(verified["status"], "VERIFIED_RESTORED")
        self.assertEqual((restored / "figures/equity.png").read_bytes(), self.payload)
        self.assertEqual((restored / "artifact_manifest.json").read_bytes(), self.manifest.read_bytes())
        self.assertEqual(json.loads((restored / "parameters.json").read_text()), {"delivery": {"max_archive_part_mib": 40}})
        again = package(self.report, self.root / "packages", part_mib=1)
        self.assertTrue(again["reused"])
        self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_tampered_part_is_rejected_before_creating_restored_output(self):
        from core.pipeline.prism_compare_delivery import package, restore
        result = package(self.report, self.root / "packages", part_mib=1)
        path = Path(result["manifest_path"])
        receipt = json.loads(path.read_text())
        part = path.parent / receipt["parts"][0]["file"]
        part.write_bytes(part.read_bytes() + b"TAMPERED")
        target = self.root / "must-not-exist"
        with self.assertRaisesRegex(ContractError, "SHA256|bytes|hash"):
            restore(path, target)
        self.assertFalse(target.exists())

    def test_unlisted_raw_or_credential_files_cannot_enter_a_package(self):
        from core.pipeline.prism_compare_delivery import package
        for name in ("market_snapshot.db", ".env"):
            with self.subTest(name=name):
                path = self.report / name
                path.write_bytes(b"UNIT_FIXTURE_ONLY")
                records = {**self.records, name: {"bytes": path.stat().st_size, "sha256": file_sha256(path)}}
                write_json(self.manifest, {"status": "COMPLETE", "source_commit": "UNIT_FIXTURE_ONLY",
                    "raw_vendor_database_included": False, "files": records})
                with self.assertRaisesRegex(ContractError, "whitelist|raw|credential"):
                    package(self.report, self.root / "forbidden")
                self.assertFalse((self.root / "forbidden").exists())


if __name__ == "__main__":
    unittest.main()
