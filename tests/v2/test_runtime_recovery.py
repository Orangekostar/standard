from __future__ import annotations

import argparse
import sqlite3
import tempfile
import tracemalloc
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core.data.v2_store import V2Store
from core.technical_v2.contracts import sha256_json
from scripts.v2 import (
    _analyze,
    _compute_feature_snapshots,
    _evaluate_matured,
    _feature_store_rows,
    _market_data_hash,
    _sync,
)
from ui.technical_v2 import load_technical_v2_snapshot


class RuntimeRecoveryTest(unittest.TestCase):
    def test_new_live_predictions_do_not_require_historical_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            args = argparse.Namespace(
                mode="demo", db_path=str(root / "market.db"),
                artifact_root=str(root / "artifacts"), history_sessions=90,
                as_of="20260924", seed=20260923, force_refresh=False,
                methods="formula",
            )
            _sync(args)
            _analyze(args)
            with closing(sqlite3.connect(args.db_path)) as conn, conn:
                conn.execute("UPDATE instrument_versions SET valid_from = '20260925'")
            outcome = _evaluate_matured(args)
            self.assertEqual(outcome.exit_code, 0)
            self.assertEqual(outcome.payload["status"], "NO_NEW_MATURE_LABELS")
            self.assertEqual(outcome.payload["label_rows"], 0)

    def test_mature_predictions_still_materialize_and_evaluate_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            args = argparse.Namespace(
                mode="demo", db_path=str(root / "market.db"),
                artifact_root=str(root / "artifacts"), history_sessions=90,
                as_of="20260924", seed=20260923, force_refresh=False,
                methods="formula",
            )
            _sync(args)
            args.as_of = "20260917"
            _analyze(args)
            outcome = _evaluate_matured(args)
            self.assertEqual(outcome.exit_code, 0)
            self.assertGreater(outcome.payload["label_rows"], 0)
            self.assertGreater(outcome.payload["evaluated_rows"], 0)

    def test_missing_market_bar_preserves_unavailable_features_as_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = V2Store(Path(tmpdir) / "market.db", data_mode="test")
            store.migrate()
            features = pd.DataFrame([{"code": "600000.SH", "price_basis": float("nan")}])
            rows = _feature_store_rows("missing-bar", "20260924", features, pd.DataFrame())
            store.upsert_feature_rows(rows)
            saved = store.read_feature_rows(run_id="missing-bar")
            self.assertEqual(len(saved), 21)
            self.assertEqual(set(saved["status"]), {"UNAVAILABLE"})
            self.assertTrue(saved["value"].isna().all())

    def test_repeated_routes_reuse_features_for_the_same_data_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            args = argparse.Namespace(
                mode="demo", db_path=str(root / "market.db"),
                artifact_root=str(root / "artifacts"), history_sessions=90,
                as_of="20260924", seed=20260923, force_refresh=False,
                methods="formula",
            )
            _sync(args)
            with closing(sqlite3.connect(args.db_path)) as conn, conn:
                conn.execute(
                    "DELETE FROM daily_raw WHERE code = ? AND date = ?",
                    ("600000.SH", "20260924"),
                )
            with patch("scripts.v2._compute_feature_snapshots", wraps=_compute_feature_snapshots) as compute:
                first = _analyze(args)
                second = _analyze(args)
            self.assertEqual(first.exit_code, 0)
            self.assertEqual(second.exit_code, 0)
            self.assertEqual(first.payload["publication_id"], second.payload["publication_id"])
            self.assertEqual(compute.call_count, 1)

    def test_latest_analysis_uses_observed_roster_without_backdating_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            args = argparse.Namespace(
                mode="demo", db_path=str(root / "market.db"),
                artifact_root=str(root / "artifacts"), history_sessions=90,
                as_of="20260924", seed=20260923, force_refresh=False,
                methods="formula",
            )
            _sync(args)
            with closing(sqlite3.connect(args.db_path)) as conn, conn:
                conn.execute(
                    "UPDATE instrument_versions SET valid_from = ?, observed_at = ?",
                    ("20260925", "2026-09-25T08:00:00+00:00"),
                )
            freshness = {
                "actual_as_of": "20260924", "expected_as_of": "20260924",
                "freshness_status": "CURRENT", "resolved_at": "2026-09-26T12:00:00+08:00",
            }
            with patch("scripts.v2.resolve_latest_as_of", return_value=freshness):
                outcome = _analyze(args)
            self.assertEqual(outcome.exit_code, 0, outcome.payload)
            snapshot = load_technical_v2_snapshot(root / "artifacts")
            self.assertEqual(snapshot.rows["entity_id"].nunique(), 5)
            self.assertFalse(snapshot.rows["entity_id"].isin(["300001.SZ", "688001.SH"]).any())
            self.assertEqual(outcome.payload["universe_history_mode"], "CURRENT_SNAPSHOT_ONLY")
            self.assertGreaterEqual(
                pd.Timestamp(snapshot.manifest["information_cutoff"]),
                pd.Timestamp("2026-09-25T08:00:00+00:00"),
            )
            store = V2Store(Path(args.db_path), data_mode="demo")
            self.assertTrue(store.read_instrument_versions("20260924").empty)
            self.assertEqual(set(snapshot.features["universe_history_mode"].dropna()), {"CURRENT_SNAPSHOT_ONLY"})

    def test_historical_analysis_cannot_use_future_roster(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            args = argparse.Namespace(
                mode="demo", db_path=str(root / "market.db"),
                artifact_root=str(root / "artifacts"), history_sessions=90,
                as_of="20260924", seed=20260923, force_refresh=False,
                methods="formula",
            )
            _sync(args)
            with closing(sqlite3.connect(args.db_path)) as conn, conn:
                conn.execute("UPDATE instrument_versions SET valid_from = '20260928'")
            freshness = {
                "actual_as_of": "20260924", "expected_as_of": "20260928",
                "freshness_status": "STALE", "resolved_at": "2026-09-28T20:00:00+08:00",
            }
            with patch("scripts.v2.resolve_latest_as_of", return_value=freshness):
                outcome = _analyze(args)
            self.assertEqual(outcome.exit_code, 2)
            self.assertIn("DATED_INSTRUMENT_UNIVERSE", outcome.payload["missing"])
            self.assertFalse((root / "artifacts" / "latest.json").exists())

    def test_market_hash_is_bounded_memory_and_preserves_canonical_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = V2Store(Path(tmpdir) / "market.db", data_mode="test")
            store.migrate()
            with closing(sqlite3.connect(store.path)) as conn, conn:
                conn.executemany(
                    "INSERT INTO calendar VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [("SSE", f"{index:08d}", 1, None, None, "source" * 200, "v1", "now")
                     for index in range(6000)],
                )
                conn.row_factory = sqlite3.Row
                tables = ("instrument_versions", "calendar", "daily_raw", "adjustments",
                          "trading_status", "sector_membership", "corporate_actions", "sync_audits")
                payload = {name: [] for name in tables}
                payload["calendar"] = [dict(row) for row in conn.execute("SELECT * FROM calendar ORDER BY exchange,date")]
                expected = sha256_json(payload)
                del payload
            tracemalloc.start()
            try:
                actual = _market_data_hash(store, "20260924")
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual(actual, expected)
            self.assertLess(peak, 10 * 1024 * 1024, f"hash allocated {peak} bytes")


if __name__ == "__main__":
    unittest.main()
