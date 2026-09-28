from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from core.analysis.sector_v2 import build_context
from core.backtest.prism_compare_rules import security_rule
from core.factors.technical_v2 import compute_technical_v2
from core.data.v2_store import V2Store
from core.pipeline.prism_compare_config import file_sha256, load_config
from core.pipeline.prism_compare_data import align_panel, audit_snapshot, create_snapshot, experiment_lock, fit_common_bins
from core.technical_v2.contracts import ContractError


class PrismDataTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def fixture(self, count=110):
        dates = pd.bdate_range("2024-01-02", periods=count).strftime("%Y%m%d").tolist()
        close = 10 * np.exp(np.arange(count) * .001 + np.sin(np.arange(count)) * .003)
        raw = pd.DataFrame(dict(code="600000.SH", date=dates, open=close,
                                high=close * 1.01, low=close * .99, close=close,
                                volume_shares=100000, amount_cny=1e7,
                                completeness="COMPLETE", data_source_mode="real", source="FIXTURE"))
        adj = pd.DataFrame(dict(code="600000.SH", date=dates, adj_factor=1.0))
        roster = pd.DataFrame(dict(code=["600000.SH"], list_date=["19991110"], delist_date=[None]))
        return dates, raw, adj, roster

    def test_backup_is_read_only_and_resume_keeps_original_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.db"
            with closing(sqlite3.connect(source)) as conn:
                conn.execute("CREATE TABLE schema_migrations(version INTEGER)")
                conn.execute("INSERT INTO schema_migrations VALUES (1)")
                conn.commit()
            before = file_sha256(source)
            root = Path(directory) / "experiment"
            manifest = create_snapshot(source, root)
            self.assertEqual(file_sha256(source), before)
            self.assertEqual(file_sha256(root / "market_snapshot.db"), manifest["snapshot_sha256"])
            with closing(sqlite3.connect(source)) as conn:
                conn.execute("INSERT INTO schema_migrations VALUES (2)")
                conn.commit()
            self.assertEqual(create_snapshot(source, root), manifest)
            with closing(sqlite3.connect(f"file:{root / 'market_snapshot.db'}?mode=ro", uri=True)) as conn:
                self.assertEqual(conn.execute("SELECT version FROM schema_migrations").fetchall(), [(1,)])

    def test_concurrent_experiment_lock_cannot_change_another_run(self):
        with tempfile.TemporaryDirectory() as directory:
            with experiment_lock(directory):
                with self.assertRaises(ContractError):
                    with experiment_lock(directory):
                        self.fail("second writer must not acquire the experiment lock")
            with experiment_lock(directory):
                self.assertTrue((Path(directory) / ".compare.lock").exists())

    def test_missing_session_is_nan_not_a_multiday_return(self):
        dates, raw, adj, roster = self.fixture()
        raw = raw.loc[raw.date.ne(dates[80])]
        panel = align_panel(raw, adj, dates, roster)
        self.assertEqual(len(panel), len(dates))
        self.assertTrue(pd.isna(panel.iloc[80].adjusted_close))
        self.assertTrue(pd.isna(panel.iloc[80].volume_shares))
        context = build_context(panel, pd.DataFrame(), as_of=dates[-1], sessions=dates)
        features = compute_technical_v2(panel, context, as_of=dates[-1])
        self.assertTrue(pd.isna(features.iloc[81].Q02))

    def test_future_adjustment_cannot_rescale_past_comparison_price_or_factors(self):
        dates, raw, adj, roster = self.fixture()
        early = align_panel(raw.iloc[:100], adj.iloc[:100], dates[:100], roster)
        adj.loc[100:, "adj_factor"] = 2
        late = align_panel(raw, adj, dates, roster)
        pd.testing.assert_series_equal(early.comparison_close, late.iloc[:100].comparison_close)
        features = []
        for panel, sessions in ((early, dates[:100]), (late, dates)):
            context = build_context(panel, pd.DataFrame(), as_of=sessions[-1], sessions=sessions)
            features.append(compute_technical_v2(panel, context, as_of=sessions[-1]))
        columns = self.config["native_contract"]["factor_ids"] + self.config["native_contract"]["risk_ids"]
        pd.testing.assert_frame_equal(features[0][columns], features[1].iloc[:100][columns], atol=1e-10)

    def test_test_returns_cannot_change_common_train_model(self):
        rows = pd.DataFrame(dict(entity_type="stock", horizon=5,
                                 as_of_trade_date=["20240102", "20240103", "20240502"],
                                 formula_score=[70., 70., 70.], realized_return=[.01, .03, 999.],
                                 label_end_date=["20240110", "20240111", "20240510"],
                                 label_status="OK", split=["train", "train", "test"]))
        first = fit_common_bins(rows, "20240201")
        rows.loc[2, "realized_return"] = -999
        second = fit_common_bins(rows, "20240201")
        self.assertEqual(asdict(first), asdict(second))
        self.assertAlmostEqual(first.global_mean, .02)

    def test_unknown_board_is_not_turned_into_a_universal_100_lot(self):
        self.assertIsNone(security_rule(None, "20240102", self.config))
        self.assertIsNone(security_rule("BSE", "20240102", self.config))
        star = security_rule("STAR", "20240102", self.config)
        main = security_rule("MAIN_SH", "20240102", self.config)
        self.assertEqual(star.floor_buy_quantity(201), 201)
        self.assertEqual(main.floor_buy_quantity(201), 200)
        self.assertNotEqual(main.source, security_rule("MAIN_SH", "20260706", self.config).source)

    def test_audit_cutoff_uses_complete_audit_not_max_raw_date(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.db"
            V2Store(source, data_mode="real").migrate()
            with closing(sqlite3.connect(source)) as conn:
                for exchange in ("SSE", "SZSE"):
                    for date in ("20260102", "20260105", "20260106"):
                        conn.execute("INSERT INTO calendar(exchange,date,is_open,source,source_version,retrieved_at) VALUES (?,?,1,'FIXTURE','v1','20260106')", (exchange, date))
                conn.execute("INSERT INTO instrument_versions(code,instrument_type,exchange,listing_board,name,list_date,valid_from,source,source_version,observed_at) VALUES ('600000.SH','STOCK','SSE','MAIN_SH','*ST TODAY','19991110','20260102','FIXTURE','v1','20260106')")
                for date in ("20260102", "20260105", "20260106"):
                    conn.execute("INSERT INTO daily_raw(code,date,source_version,open,high,low,close,volume_shares,amount_cny,source,retrieved_at,completeness,data_source_mode) VALUES ('600000.SH',?,'v1',10,11,9,10,10000,100000,'FIXTURE','20260106','COMPLETE','real')", (date,))
                conn.execute("INSERT INTO sync_audits(exchange,date,source_version,expected_instruments,observed_rows,known_non_trading_rows,coverage,status,recorded_at) VALUES ('SSE','20260105','v1',1,1,0,1,'COMPLETE','20260106')")
                conn.commit()
            result = audit_snapshot(source, self.config)
            self.assertEqual(result.audit["as_of"], "20260105")
            self.assertEqual(result.sessions, ["20260102", "20260105"])
            self.assertEqual(result.audit["calendar_agreement"], "SSE_SZSE_MATCH")
            self.assertIn("RAW_PRICE_LEDGER_CORPORATE_ACTIONS_INCOMPLETE", result.audit["scope_flags"])
            self.assertNotIn("name", result.roster.columns)


if __name__ == "__main__":
    unittest.main()
