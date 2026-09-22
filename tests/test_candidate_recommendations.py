from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path

import numpy as np
import pandas as pd

from core.data.data_manager import DataManager


def _sample_panel() -> pd.DataFrame:
    days = pd.bdate_range("2026-01-01", periods=45)
    rows = []
    specs = [
        ("000001.SZ", "趋势A", 10.0, 0.018),
        ("600001.SH", "趋势B", 8.0, 0.012),
        ("000002.SZ", "弱势C", 12.0, -0.004),
    ]
    for code, name, start, drift in specs:
        close = start * np.cumprod(np.full(len(days), 1.0 + drift))
        if code == "600001.SH":
            close[-1] = close[-5:].mean() * 1.003
        for i, day in enumerate(days):
            rows.append(
                {
                    "ts_code": code,
                    "name": name,
                    "trade_date": day.strftime("%Y%m%d"),
                    "open": close[i] * 0.99,
                    "high": close[i] * 1.02,
                    "low": close[i] * 0.98,
                    "close": close[i],
                    "pct_chg": drift * 100.0,
                    "vol": 100000 + i * 100,
                    "amount": close[i] * (100000 + i * 100),
                }
            )
    return pd.DataFrame(rows)


def _entry_signal_panel() -> pd.DataFrame:
    days = pd.bdate_range("2026-01-01", periods=60)
    rows = []
    specs = [
        ("000001.SZ", "主板新信号", "银行", "主板", 42, 0.030),
        ("600001.SH", "沪市老信号", "制造", "主板", 25, 0.020),
        ("300001.SZ", "创业板排除", "科技", "创业板", 45, 0.050),
        ("688001.SH", "科创板排除", "半导体", "科创板", 45, 0.050),
    ]
    for code, name, industry, market, start_idx, drift in specs:
        close = 10.0
        for idx, day in enumerate(days):
            if idx >= start_idx:
                close *= 1.0 + drift
            rows.append(
                {
                    "ts_code": code,
                    "name": name,
                    "industry": industry,
                    "market": market,
                    "trade_date": day.strftime("%Y%m%d"),
                    "open": close * 0.99,
                    "high": close * 1.02,
                    "low": close * 0.98,
                    "close": close,
                    "pct_chg": drift * 100.0 if idx >= start_idx else 0.0,
                    "vol": 100000 + idx * 100,
                    "amount": close * (100000 + idx * 100),
                }
            )
    return pd.DataFrame(rows)


class CandidateRecommendationTest(unittest.TestCase):
    def _manager_with_panel(self) -> DataManager:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        dm = DataManager(cache_dir=Path(tmpdir.name) / "cache", market_db_path=Path(tmpdir.name) / "market.db")
        dm.market_db.write_daily_bars(_sample_panel())
        dm.market_db.write_stock_basic(
            pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "symbol": "000001", "name": "趋势A", "industry": "银行", "market": "主板"},
                    {"ts_code": "600001.SH", "symbol": "600001", "name": "趋势B", "industry": "制造", "market": "主板"},
                    {"ts_code": "000002.SZ", "symbol": "000002", "name": "弱势C", "industry": "地产", "market": "主板"},
                ]
            )
        )
        return dm

    def test_scientific_candidates_rank_non_empty_market_data(self) -> None:
        dm = self._manager_with_panel()

        out = dm.recommend_scientific_candidates(end_date="20260131", lookback_days=45, top_n=2, universe_size=3)

        self.assertGreater(len(out), 0)
        self.assertIn("推荐评分", out.columns)
        self.assertIn("建议仓位%", out.columns)
        self.assertEqual(out.iloc[0]["ts_code"], "000001.SZ")

    def test_ma5_pullback_candidates_rank_near_ma5_setups(self) -> None:
        dm = self._manager_with_panel()

        out = dm.recommend_ma5_pullback_candidates(end_date="20260131", lookback_days=45, top_n=2, universe_size=3)

        self.assertGreater(len(out), 0)
        self.assertIn("5日线偏离%", out.columns)
        self.assertEqual(out.iloc[0]["ts_code"], "600001.SH")

    def test_bottom_rebound_candidates_rank_oversold_recoveries(self) -> None:
        dm = self._manager_with_panel()

        out = dm.recommend_bottom_rebound_candidates(end_date="20260131", lookback_days=45, top_n=2, universe_size=3)

        self.assertGreater(len(out), 0)
        self.assertIn("反弹确认分", out.columns)
        self.assertIn("超跌幅度%", out.columns)

    def test_sector_fund_flow_fallback_has_all_requested_periods(self) -> None:
        dm = self._manager_with_panel()

        payload = dm.build_sector_fund_flow_snapshot(indicators=["当日", "3日", "10日"], force_refresh=True)

        self.assertEqual(payload["available_indicators"], ["当日", "3日", "10日"])
        for key in ["当日", "3日", "10日"]:
            self.assertIn(key, payload["sector_fund_flow_df_map"])
            self.assertFalse(payload["sector_fund_flow_df_map"][key].empty)

    def test_sector_fund_flow_fallback_uses_local_industry_rank(self) -> None:
        dm = self._manager_with_panel()

        with mock.patch("akshare.stock_sector_fund_flow_rank", side_effect=RuntimeError("eastmoney offline")):
            with mock.patch("akshare.stock_fund_flow_industry", side_effect=RuntimeError("ths offline")):
                payload = dm.build_sector_fund_flow_snapshot(indicators=["当日"], force_refresh=True)

        df = payload["sector_fund_flow_df_map"]["当日"]
        self.assertGreaterEqual(len(df), 2)
        self.assertTrue(df["资金流来源"].astype(str).eq("local_market_proxy").all())
        self.assertIn("主力净流入净额", df.columns)

    def test_recent_entry_signals_scan_all_buyable_sh_sz_and_sort_by_signal_date(self) -> None:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        dm = DataManager(cache_dir=Path(tmpdir.name) / "cache", market_db_path=Path(tmpdir.name) / "market.db")
        dm.market_db.write_daily_bars(_entry_signal_panel())
        dm.market_db.write_stock_basic(
            pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "symbol": "000001", "name": "主板新信号", "industry": "银行", "market": "主板"},
                    {"ts_code": "600001.SH", "symbol": "600001", "name": "沪市老信号", "industry": "制造", "market": "主板"},
                    {"ts_code": "300001.SZ", "symbol": "300001", "name": "创业板排除", "industry": "科技", "market": "创业板"},
                    {"ts_code": "688001.SH", "symbol": "688001", "name": "科创板排除", "industry": "半导体", "market": "科创板"},
                ]
            )
        )

        out = dm.list_recent_sector_entry_signals(
            end_date="20260325",
            lookback_days=80,
            universe_size=0,
            recent_signal_days=60,
            threshold=0.1,
            enabled_factors=["momentum_20"],
            factor_weights={"momentum_20": 1.0},
        )

        self.assertEqual(out["ts_code"].tolist(), ["000001.SZ", "600001.SH"])
        self.assertTrue(pd.to_datetime(out["信号日期"]).is_monotonic_decreasing)
        self.assertTrue(out["分组"].astype(str).eq("全市场刚出现建仓信号").all())


if __name__ == "__main__":
    unittest.main()
