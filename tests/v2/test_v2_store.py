from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import pandas as pd

from core.data.v2_provider import normalize_daily_frame
from core.data.v2_store import REQUIRED_TABLES, V2Store
from core.technical_v2.contracts import ContractError


class V2StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.store = V2Store(self.root / "real" / "market.db", data_mode="real")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_migration_creates_all_required_tables_and_records_version(self) -> None:
        version = self.store.migrate()

        with closing(sqlite3.connect(self.store.path)) as conn:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            migrations = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()

        self.assertEqual(version, 1)
        self.assertTrue(REQUIRED_TABLES.issubset(names))
        self.assertEqual(migrations, [(1,)])

    def test_daily_upsert_is_idempotent_for_code_date_source_version(self) -> None:
        self.store.migrate()
        raw = pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20260922",
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "pre_close": 9.9,
                    "vol": 100,
                    "amount": 200,
                }
            ]
        )
        rows = normalize_daily_frame(raw, source_version="fixture-v1", retrieved_at="2026-09-22T16:10:00+08:00")

        self.store.upsert_daily_raw(rows)
        self.store.upsert_daily_raw(rows.assign(close=10.3))
        stored = self.store.read_daily_raw("20260922", "20260922")

        self.assertEqual(len(stored), 1)
        self.assertEqual(stored.loc[0, "close"], 10.3)
        self.assertEqual(stored.loc[0, "volume_shares"], 10000.0)

    def test_real_and_demo_stores_are_physically_isolated(self) -> None:
        demo = V2Store(self.root / "demo" / "market.db", data_mode="demo")
        self.store.migrate()
        demo.migrate()
        demo.write_analysis_run(
            {
                "run_id": "demo-run",
                "as_of_trade_date": "20260922",
                "mode": "EOD_FINAL",
                "data_source_mode": "demo",
                "status": "OK",
            }
        )

        self.assertEqual(self.store.read_analysis_runs().shape[0], 0)
        self.assertEqual(demo.read_analysis_runs().loc[0, "run_id"], "demo-run")

    def test_real_store_rejects_demo_daily_rows(self) -> None:
        self.store.migrate()
        rows = normalize_daily_frame(
            pd.DataFrame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": "20260922",
                        "open": 10,
                        "high": 10,
                        "low": 10,
                        "close": 10,
                        "vol": 1,
                        "amount": 1,
                    }
                ]
            ),
            data_source_mode="demo",
        )

        with self.assertRaises(ContractError):
            self.store.upsert_daily_raw(rows)

    def test_calendar_resolves_only_stored_open_sessions(self) -> None:
        self.store.migrate()
        calendar = pd.DataFrame(
            [
                {"exchange": "SSE", "date": "20260918", "is_open": 1, "source": "fixture", "source_version": "v1"},
                {"exchange": "SSE", "date": "20260919", "is_open": 0, "source": "fixture", "source_version": "v1"},
                {"exchange": "SSE", "date": "20260921", "is_open": 1, "source": "fixture", "source_version": "v1"},
                {"exchange": "SSE", "date": "20260922", "is_open": 1, "source": "fixture", "source_version": "v1"},
            ]
        )
        self.store.upsert_calendar(calendar)

        self.assertEqual(self.store.open_sessions("SSE", end_date="20260922", limit=2), ["20260921", "20260922"])
        self.assertEqual(self.store.next_session("SSE", "20260918"), "20260921")
        self.assertIsNone(self.store.next_session("SSE", "20260922"))

    def test_dated_instrument_and_sector_reads_respect_validity_intervals(self) -> None:
        self.store.migrate()
        observed_at = "2026-09-22T16:10:00+08:00"
        self.store.upsert_instrument_versions(
            pd.DataFrame(
                [
                    {
                        "code": "600000.SH",
                        "instrument_type": "stock",
                        "exchange": "SSE",
                        "listing_board": "MAIN_SH",
                        "name": "Sample",
                        "list_date": "19991110",
                        "delist_date": None,
                        "listing_status": "L",
                        "valid_from": "19991110",
                        "valid_to": None,
                        "source": "fixture",
                        "source_version": "v1",
                        "observed_at": observed_at,
                    }
                ]
            )
        )
        self.store.upsert_sector_membership(
            pd.DataFrame(
                [
                    {
                        "namespace": "SW_L1",
                        "sector_id": "old",
                        "code": "600000.SH",
                        "valid_from": "20200101",
                        "valid_to": "20211231",
                        "observed_at": observed_at,
                        "history_mode": "RECONSTRUCTED_PIT",
                        "source": "fixture",
                        "source_version": "v1",
                    },
                    {
                        "namespace": "SW_L1",
                        "sector_id": "new",
                        "code": "600000.SH",
                        "valid_from": "20220101",
                        "valid_to": None,
                        "observed_at": observed_at,
                        "history_mode": "RECONSTRUCTED_PIT",
                        "source": "fixture",
                        "source_version": "v1",
                    },
                ]
            )
        )

        instruments = self.store.read_instrument_versions("20260922")
        old = self.store.read_sector_membership("20210601")
        new = self.store.read_sector_membership("20260922")

        self.assertEqual(instruments["code"].tolist(), ["600000.SH"])
        self.assertEqual(old["sector_id"].tolist(), ["old"])
        self.assertEqual(new["sector_id"].tolist(), ["new"])

    def test_corporate_action_upsert_preserves_single_versioned_event(self) -> None:
        self.store.migrate()
        actions = pd.DataFrame(
            [
                {
                    "event_id": "event-1",
                    "code": "600000.SH",
                    "event_type": "DIVIDEND_AND_SHARE",
                    "record_date": "20260520",
                    "ex_date": "20260521",
                    "pay_date": "20260522",
                    "list_date": "20260523",
                    "cash_per_share": "0.2",
                    "share_ratio": "0.1",
                    "split_ratio": None,
                    "status": "implemented",
                    "source": "fixture",
                    "source_version": "v1",
                    "retrieved_at": "2026-05-01T20:30:00+08:00",
                }
            ]
        )

        self.store.upsert_corporate_actions(actions)
        self.store.upsert_corporate_actions(actions.assign(status="settled"))
        stored = self.store.read_corporate_actions("600000.SH")

        self.assertEqual(len(stored), 1)
        self.assertEqual(stored.loc[0, "status"], "settled")


if __name__ == "__main__":
    unittest.main()
