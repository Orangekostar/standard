from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pandas as pd


MODULE_PATH = Path(__file__).resolve().parents[1] / "core" / "data" / "data_manager.py"
SPEC = spec_from_file_location("data_manager_under_test", MODULE_PATH)
DATA_MANAGER_MODULE = module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(DATA_MANAGER_MODULE)

DataManager = DATA_MANAGER_MODULE.DataManager


class _FakeMarketDB:
    def __init__(self, flow_df: pd.DataFrame) -> None:
        self._flow_df = flow_df

    def read_sector_fund_flow(self, indicator: str, sector_type: str) -> pd.DataFrame:
        return self._flow_df.copy()


class SectorFundFlowSnapshotTest(unittest.TestCase):
    def _sample_flow_df(self, *, indicator: str, source: str, updated_at: str) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "industry": "半导体",
                    "涨跌幅": 1.25,
                    "主力净流入净额": 123456.0,
                    "主力净流入净占比": 8.5,
                    "超大单净流入净额": 60000.0,
                    "超大单净流入净占比": 4.3,
                    "大单净流入净额": 40000.0,
                    "大单净流入净占比": 2.6,
                    "中单净流入净额": -20000.0,
                    "中单净流入净占比": -1.4,
                    "小单净流入净额": -30000.0,
                    "小单净流入净占比": -2.1,
                    "主力净流入最大股": "测试股",
                    "资金流周期": indicator,
                    "板块资金流类型": "行业资金流",
                    "资金流来源": source,
                    "更新时间": updated_at,
                }
            ]
        )

    def test_db_read_keeps_underlying_flow_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dm = object.__new__(DataManager)
            dm.cache_dir = Path(tmpdir)
            dm.market_db = _FakeMarketDB(
                self._sample_flow_df(
                    indicator="10日",
                    source="market_db_proxy",
                    updated_at="2026-04-27 16:00:00",
                )
            )

            out = DataManager.get_sector_fund_flow_rank(
                dm,
                indicator="10日",
                sector_type="行业资金流",
                force_refresh=False,
            )

        self.assertEqual(out.iloc[0]["资金流来源"], "market_db_proxy")

    def test_live_sector_flow_bypasses_proxy_when_direct_enabled(self) -> None:
        captured: dict[str, str | None] = {}

        def fake_rank(indicator: str, sector_type: str) -> pd.DataFrame:
            captured["indicator"] = indicator
            captured["http_proxy"] = os.environ.get("HTTP_PROXY")
            captured["https_proxy"] = os.environ.get("HTTPS_PROXY")
            captured["all_proxy"] = os.environ.get("ALL_PROXY")
            return self._sample_flow_df(
                indicator=indicator,
                source="akshare_raw",
                updated_at="2026-04-27 16:00:00",
            ).rename(columns={"industry": "名称"})

        fake_ak = types.SimpleNamespace(stock_sector_fund_flow_rank=fake_rank)
        old_ak = sys.modules.get("akshare")
        sys.modules["akshare"] = fake_ak
        old_env = {key: os.environ.get(key) for key in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "QUANT_EASTMONEY_DIRECT"]}
        try:
            os.environ["HTTP_PROXY"] = "http://127.0.0.1:7890"
            os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
            os.environ["ALL_PROXY"] = "http://127.0.0.1:7890"
            os.environ["QUANT_EASTMONEY_DIRECT"] = "1"
            dm = object.__new__(DataManager)
            dm.cache_dir = Path(tempfile.gettempdir())
            dm.market_db = _FakeMarketDB(pd.DataFrame())

            out = DataManager.get_sector_fund_flow_rank(
                dm,
                indicator="当日",
                sector_type="行业资金流",
                force_refresh=True,
            )
        finally:
            if old_ak is None:
                sys.modules.pop("akshare", None)
            else:
                sys.modules["akshare"] = old_ak
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(captured["indicator"], "今日")
        self.assertIsNone(captured["http_proxy"])
        self.assertIsNone(captured["https_proxy"])
        self.assertIsNone(captured["all_proxy"])
        self.assertEqual(os.environ.get("HTTP_PROXY"), old_env["HTTP_PROXY"])
        self.assertEqual(out.iloc[0]["资金流来源"], "akshare_live")

    def test_sector_flow_uses_ths_live_fallback_when_eastmoney_rank_fails(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_rank(indicator: str, sector_type: str) -> pd.DataFrame:
            calls.append(("eastmoney", indicator))
            raise RuntimeError("eastmoney disconnected")

        def fake_ths(symbol: str = "即时") -> pd.DataFrame:
            calls.append(("ths", symbol))
            return pd.DataFrame(
                [
                    {
                        "行业": "半导体",
                        "行业指数": 21134.30,
                        "阶段涨跌幅": "1.38%",
                        "流入资金": 866.19,
                        "流出资金": 763.23,
                        "净额": 102.96,
                        "公司家数": 180,
                    }
                ]
            )

        fake_ak = types.SimpleNamespace(
            stock_sector_fund_flow_rank=fake_rank,
            stock_fund_flow_industry=fake_ths,
        )
        old_ak = sys.modules.get("akshare")
        sys.modules["akshare"] = fake_ak
        try:
            dm = object.__new__(DataManager)
            dm.cache_dir = Path(tempfile.gettempdir())
            dm.market_db = _FakeMarketDB(pd.DataFrame())

            out = DataManager.get_sector_fund_flow_rank(
                dm,
                indicator="3日",
                sector_type="行业资金流",
                force_refresh=True,
            )
        finally:
            if old_ak is None:
                sys.modules.pop("akshare", None)
            else:
                sys.modules["akshare"] = old_ak

        self.assertIn(("eastmoney", "3日"), calls)
        self.assertIn(("ths", "3日排行"), calls)
        self.assertEqual(out.iloc[0]["资金流来源"], "akshare_ths_live")
        self.assertEqual(out.iloc[0]["industry"], "半导体")
        self.assertEqual(float(out.iloc[0]["主力净流入净额"]), 102.96)

    def test_build_snapshot_adds_mode_and_intraday_overlay(self) -> None:
        dm = object.__new__(DataManager)

        def fake_get_sector_fund_flow_rank(
            indicator: str = "10日",
            sector_type: str = "行业资金流",
            refresh_hours: int = 8,
            force_refresh: bool = False,
        ) -> pd.DataFrame:
            if indicator == "当日":
                return self._sample_flow_df(
                    indicator="当日",
                    source="akshare_live",
                    updated_at="2026-04-27 16:05:00",
                )
            if indicator == "10日":
                return self._sample_flow_df(
                    indicator="10日",
                    source="market_db_proxy",
                    updated_at="2026-04-27 16:04:00",
                )
            return pd.DataFrame()

        dm.get_sector_fund_flow_rank = fake_get_sector_fund_flow_rank

        payload = DataManager.build_sector_fund_flow_snapshot(
            dm,
            indicators=["当日", "10日"],
            sector_type="行业资金流",
            force_refresh=True,
        )

        self.assertEqual(payload["available_indicators"], ["当日", "10日"])
        flow_df_map = payload["sector_fund_flow_df_map"]
        summary_map = payload["sector_fund_flow_summary_map"]
        self.assertIn("10日", flow_df_map)
        self.assertIn("10日", summary_map)

        display_df = flow_df_map["10日"]
        self.assertEqual(display_df.iloc[0]["资金流模式"], "代理资金流")
        self.assertEqual(display_df.iloc[0]["当日资金流模式"], "真实资金流")
        self.assertEqual(display_df.iloc[0]["当日资金流来源"], "akshare_live")
        self.assertEqual(summary_map["10日"]["资金流模式"], "代理资金流")
        self.assertEqual(summary_map["10日"]["当日资金流模式"], "真实资金流")


if __name__ == "__main__":
    unittest.main()
