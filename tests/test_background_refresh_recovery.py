from __future__ import annotations

import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import pandas as pd

import core.background.precompute_worker as precompute_worker
from core.background.precompute_worker import PrecomputeWorker
from core.data.data_manager import DataManager


def _sample_market_panel() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    dates = pd.bdate_range("2026-01-01", periods=32)
    stocks = [
        ("000001.SZ", "样本A", "银行", 10.0, 1.012),
        ("000002.SZ", "样本B", "地产", 12.0, 1.004),
        ("600000.SH", "样本C", "金融", 8.0, 1.008),
    ]
    for ts_code, name, industry, base, drift in stocks:
        close = base
        for idx, day in enumerate(dates):
            close *= drift
            rows.append(
                {
                    "ts_code": ts_code,
                    "trade_date": day.strftime("%Y%m%d"),
                    "open": close * 0.995,
                    "high": close * 1.015,
                    "low": close * 0.985,
                    "close": close,
                    "vol": 100000 + idx * 1000,
                    "amount": (100000 + idx * 1000) * close,
                    "turnover_rate": 1.0 + idx * 0.01,
                    "pe": 12.0,
                    "pb": 1.2,
                    "total_mv": 1000000.0,
                    "name": name,
                    "industry": industry,
                    "market": "主板",
                }
            )
    return pd.DataFrame(rows)


class _FakeAllMarketPro:
    def __init__(self, codes: list[str], days: list[str]) -> None:
        self.codes = codes
        self.days = days

    def stock_basic(self, *args, **kwargs) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "ts_code": code,
                    "symbol": code.split(".")[0],
                    "name": f"样本{idx:02d}",
                    "area": "",
                    "industry": "测试行业",
                    "market": "主板",
                    "list_date": "20200101",
                }
                for idx, code in enumerate(self.codes, start=1)
            ]
        )

    def daily(self, *args, **kwargs) -> pd.DataFrame:
        selected_codes = self.codes
        if kwargs.get("ts_code"):
            selected_codes = [str(kwargs["ts_code"])]
        selected_days = [str(kwargs["trade_date"])] if kwargs.get("trade_date") else self.days
        rows: list[dict[str, object]] = []
        for day_idx, day in enumerate(selected_days, start=1):
            for code_idx, code in enumerate(selected_codes, start=1):
                close = 10.0 + code_idx * 0.1 + day_idx * 0.01
                rows.append(
                    {
                        "ts_code": code,
                        "trade_date": day,
                        "open": close - 0.05,
                        "high": close + 0.1,
                        "low": close - 0.1,
                        "close": close,
                        "pct_chg": 1.0,
                        "vol": 100000 + code_idx,
                        "amount": close * (100000 + code_idx),
                    }
                )
        return pd.DataFrame(rows)


class BackgroundRefreshRecoveryTest(unittest.TestCase):
    def test_worker_imports_task_status_reader_for_reconciliation(self) -> None:
        self.assertIn("read_task_status", precompute_worker.__dict__)

    def test_market_sync_writes_stock_basic_table_for_three_bull_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "cache"
            db_path = cache_dir / "market.db"
            dm = DataManager(cache_dir=cache_dir, market_db_path=db_path)

            result = dm.sync_market_data_window_db(end_date="20260706", lookback_trade_days=3, force_refresh=False)

            self.assertEqual(result["status"], "ok")
            with sqlite3.connect(db_path) as conn:
                tables = pd.read_sql_query(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
                    conn,
                )["name"].tolist()
                count = pd.read_sql_query("SELECT COUNT(*) AS n FROM stock_basic", conn).iloc[0]["n"]

        self.assertIn("daily_bars", tables)
        self.assertIn("stock_basic", tables)
        self.assertGreater(int(count), 0)

    def test_market_sync_fetches_all_stock_basic_codes_not_first_twenty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "cache"
            db_path = cache_dir / "market.db"
            dm = DataManager(cache_dir=cache_dir, market_db_path=db_path)
            codes = [f"000{idx:03d}.SZ" for idx in range(1, 26)]
            fake_pro = _FakeAllMarketPro(codes=codes, days=["20260706", "20260707"])

            with mock.patch("core.data.data_manager.get_pro_api", return_value=fake_pro):
                result = dm.sync_market_data_window_db(end_date="20260707", lookback_trade_days=2, force_refresh=True)

            with sqlite3.connect(db_path) as conn:
                stock_count = pd.read_sql_query("SELECT COUNT(DISTINCT ts_code) AS n FROM daily_bars", conn).iloc[0]["n"]
                row_count = pd.read_sql_query("SELECT COUNT(*) AS n FROM daily_bars", conn).iloc[0]["n"]

        self.assertEqual(result["codes"], 25)
        self.assertEqual(int(stock_count), 25)
        self.assertEqual(int(row_count), 50)

    def test_market_sync_does_not_rewrite_existing_db_when_missing_day_has_no_remote_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "cache"
            db_path = cache_dir / "market.db"
            dm = DataManager(cache_dir=cache_dir, market_db_path=db_path)
            codes = [f"000{idx:03d}.SZ" for idx in range(1, 26)]
            dm.market_db.write_stock_basic(_FakeAllMarketPro(codes=codes, days=[]).stock_basic())
            dm.market_db.write_daily_bars(_FakeAllMarketPro(codes=codes, days=["20260707"]).daily(trade_date="20260707"))

            class EmptyDailyPro(_FakeAllMarketPro):
                def daily(self, *args, **kwargs) -> pd.DataFrame:
                    return pd.DataFrame()

            with mock.patch("core.data.data_manager.get_pro_api", return_value=EmptyDailyPro(codes=codes, days=[])):
                result = dm.sync_market_data_window_db(end_date="20260708", lookback_trade_days=2, force_refresh=False)

            with sqlite3.connect(db_path) as conn:
                row_count = pd.read_sql_query("SELECT COUNT(*) AS n FROM daily_bars", conn).iloc[0]["n"]

        self.assertEqual(result["rows"], 0)
        self.assertEqual(result["sync_reason"], "cache")
        self.assertEqual(int(row_count), 25)

    def test_smart_pick_recent_entry_signal_scan_ignores_picker_universe_limit(self) -> None:
        worker = PrecomputeWorker(max_concurrency=1)
        captured: dict[str, int] = {}

        def fake_smart_candidates(**kwargs) -> pd.DataFrame:
            captured["smart_universe_size"] = int(kwargs.get("universe_size", -1))
            return pd.DataFrame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "trade_date": "20260708",
                        "最新收盘": 10.0,
                        "推荐评分": 80.0,
                        "建议仓位%": 5.0,
                    }
                ]
            )

        def fake_recent_signals(**kwargs) -> pd.DataFrame:
            captured["recent_universe_size"] = int(kwargs.get("universe_size", -1))
            return pd.DataFrame([{"ts_code": "000001.SZ", "信号日期": "2026-07-08"}])

        worker.data_manager.recommend_scientific_candidates = fake_smart_candidates
        worker.data_manager.list_recent_sector_entry_signals = fake_recent_signals

        worker._compute_smart_pick(params={"universe_size": 8}, force_refresh=False)

        self.assertEqual(captured["smart_universe_size"], 8)
        self.assertEqual(captured["recent_universe_size"], 0)

    def test_daily_bars_are_replaced_by_code_and_trade_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "market.db"
            dm = DataManager(cache_dir=Path(tmpdir) / "cache", market_db_path=db_path)
            first = pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "trade_date": "20260707", "open": 10, "high": 11, "low": 9, "close": 10.5},
                    {"ts_code": "000001.SZ", "trade_date": "20260707", "open": 10, "high": 12, "low": 9, "close": 11.5},
                ]
            )
            second = pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "trade_date": "20260707", "open": 10, "high": 13, "low": 9, "close": 12.5},
                ]
            )

            dm.market_db.write_daily_bars(first)
            dm.market_db.write_daily_bars(second)

            with sqlite3.connect(db_path) as conn:
                rows = pd.read_sql_query("SELECT ts_code, trade_date, close FROM daily_bars", conn)

        self.assertEqual(len(rows), 1)
        self.assertEqual(float(rows.iloc[0]["close"]), 12.5)

    def test_stale_running_status_is_reconciled_from_success_snapshot(self) -> None:
        worker = PrecomputeWorker(max_concurrency=1)
        started_at = pd.Timestamp.now() - pd.Timedelta(minutes=10)
        snapshot_at = started_at + pd.Timedelta(seconds=30)

        status = {
            "task_name": "smart_pick",
            "status": "running",
            "updated_at": started_at.isoformat(),
            "extra": {"started_at": started_at.isoformat(), "pid": 111, "child_pid": 222},
        }
        snapshot = {
            "task_name": "smart_pick",
            "status": "ok",
            "generated_at": snapshot_at.isoformat(),
            "payload": {"params": {"top_n": 8}},
        }

        reconciled = worker._reconciled_task_status("smart_pick", status, snapshot)

        self.assertEqual(reconciled["status"], "ok")
        self.assertEqual(reconciled["updated_at"], snapshot_at.isoformat())

    def test_three_bull_default_end_date_matches_compact_market_db_dates(self) -> None:
        worker = PrecomputeWorker(max_concurrency=1)
        captured: dict[str, str] = {}

        def fake_loader(db_path, start_date: str = "", end_date: str = "") -> pd.DataFrame:
            captured["start_date"] = start_date
            captured["end_date"] = end_date
            return pd.DataFrame()

        with mock.patch("scripts.three_bull_pullback_report.load_market_panel", fake_loader):
            with self.assertRaises(RuntimeError):
                worker._compute_three_bull_pullback(params={"end_date": "2026-07-06"}, force_refresh=False)

        self.assertEqual(captured["end_date"], "20260706")

    def test_three_bull_fallback_candidates_show_latest_market_rank(self) -> None:
        panel = _sample_market_panel()

        out = PrecomputeWorker._fallback_three_bull_candidates(panel, top_n=2)

        self.assertEqual(len(out), 2)
        for col in ["trade_date", "ts_code", "交易动作", "候选来源", "策略评分", "买点检查"]:
            self.assertIn(col, out.columns)
        self.assertTrue(out["交易动作"].astype(str).eq("观察等待").all())

    def test_sector_fund_flow_default_periods_are_intraday_three_and_ten_day(self) -> None:
        worker = PrecomputeWorker(max_concurrency=1)
        captured: dict[str, object] = {}

        def fake_snapshot(**kwargs):
            captured.update(kwargs)
            return {"available_indicators": kwargs["indicators"]}

        worker.data_manager.build_sector_fund_flow_snapshot = fake_snapshot

        payload = worker._compute_sector_fund_flow(params={}, force_refresh=False)

        self.assertEqual(payload["available_indicators"], ["当日", "3日", "10日"])
        self.assertEqual(captured["indicators"], ["当日", "3日", "10日"])


if __name__ == "__main__":
    unittest.main()
