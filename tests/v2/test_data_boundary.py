from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from core.data.data_manager import DataManager
from core.data.v2_provider import DemoV2Provider, RealV2Provider, normalize_daily_frame
from core.data.v2_store import V2Store
from core.data.v2_universe import build_analysis_universe, classify_instrument, resolve_sector_membership


class _CountingTransport:
    def __init__(self) -> None:
        self.calls = 0

    def daily(self, **kwargs):
        self.calls += 1
        return pd.DataFrame()


class _MembershipTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def index_member_all(self, **kwargs):
        self.calls.append(dict(kwargs))
        suffix = kwargs["is_new"]
        return pd.DataFrame(
            [
                {
                    "l1_code": kwargs["l1_code"],
                    "l1_name": "Finance",
                    "ts_code": f"60000{1 if suffix == 'Y' else 2}.SH",
                    "in_date": "20200101",
                    "out_date": None if suffix == "Y" else "20211231",
                    "is_new": suffix,
                }
            ]
        )


class _MetadataTransport:
    def stock_basic(self, **kwargs):
        return pd.DataFrame(
            [
                {
                    "ts_code": "600000.SH",
                    "symbol": "600000",
                    "name": "Sample",
                    "market": "主板",
                    "list_date": "19991110",
                    "delist_date": None,
                    "list_status": "L",
                }
            ]
        )

    def adj_factor(self, **kwargs):
        return pd.DataFrame(
            [{"ts_code": "600000.SH", "trade_date": kwargs["trade_date"], "adj_factor": 2.5}]
        )

    def stk_limit(self, **kwargs):
        return pd.DataFrame(
            [
                {
                    "ts_code": "600000.SH",
                    "trade_date": kwargs["trade_date"],
                    "up_limit": 11.0,
                    "down_limit": 9.0,
                }
            ]
        )

    def trade_cal(self, **kwargs):
        return pd.DataFrame(
            [
                {"exchange": kwargs["exchange"], "cal_date": "20260918", "is_open": 1, "pretrade_date": "20260917"},
                {"exchange": kwargs["exchange"], "cal_date": "20260919", "is_open": 0, "pretrade_date": "20260918"},
                {"exchange": kwargs["exchange"], "cal_date": "20260921", "is_open": 1, "pretrade_date": "20260918"},
            ]
        )

    def suspend_d(self, **kwargs):
        return pd.DataFrame(
            [{"ts_code": "600000.SH", "trade_date": kwargs["trade_date"], "suspend_type": kwargs["suspend_type"], "suspend_timing": None}]
        )

    def dividend(self, **kwargs):
        return pd.DataFrame(
            [
                {
                    "ts_code": kwargs["ts_code"],
                    "ann_date": "20260501",
                    "div_proc": "implemented",
                    "stk_div": 0.1,
                    "stk_bo_rate": 0.04,
                    "stk_co_rate": 0.06,
                    "cash_div_tax": 0.2,
                    "record_date": "20260520",
                    "ex_date": "20260521",
                    "pay_date": "20260522",
                    "div_listdate": "20260523",
                }
            ]
        )

    def bak_basic(self, **kwargs):
        return pd.DataFrame(
            [
                {
                    "trade_date": kwargs["trade_date"],
                    "ts_code": "600000.SH",
                    "name": "Sample",
                    "industry": "Bank",
                    "list_date": "19991110",
                    "pe": 9.0,
                    "pb": 1.0,
                }
            ]
        )


class V2ProviderBoundaryTest(unittest.TestCase):
    def test_real_provider_without_token_never_calls_transport_or_returns_mock(self) -> None:
        transport = _CountingTransport()

        result = RealV2Provider(token="", transport=transport).daily("20260922")

        self.assertEqual(result.status, "UNAVAILABLE_CREDENTIALS")
        self.assertEqual(result.data_source_mode, "real")
        self.assertTrue(result.frame.empty)
        self.assertEqual(transport.calls, 0)

    def test_demo_provider_is_seeded_and_permanently_labeled(self) -> None:
        first = DemoV2Provider(seed=20260923).daily("20260922")
        second = DemoV2Provider(seed=20260923).daily("20260922")

        pd.testing.assert_frame_equal(first.frame, second.frame)
        self.assertEqual(first.status, "OK")
        self.assertEqual(first.data_source_mode, "demo")
        self.assertEqual(set(first.frame["data_source_mode"]), {"demo"})
        self.assertEqual(set(first.frame["source"]), {"SYNTHETIC_FIXTURE"})

    def test_tushare_daily_units_are_converted_once_and_raw_values_remain(self) -> None:
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
                    "vol": 12.0,
                    "amount": 3.5,
                }
            ]
        )

        normalized = normalize_daily_frame(raw, retrieved_at="2026-09-22T16:10:00+08:00")

        self.assertEqual(normalized.loc[0, "volume_raw"], 12.0)
        self.assertEqual(normalized.loc[0, "volume_raw_unit"], "lot")
        self.assertEqual(normalized.loc[0, "volume_shares"], 1200.0)
        self.assertEqual(normalized.loc[0, "amount_raw"], 3.5)
        self.assertEqual(normalized.loc[0, "amount_raw_unit"], "thousand_cny")
        self.assertEqual(normalized.loc[0, "amount_cny"], 3500.0)

    def test_daily_endpoint_marks_limit_hit_as_partial_instead_of_complete(self) -> None:
        class LimitTransport:
            def daily(self, **kwargs):
                return pd.DataFrame(
                    {
                        "ts_code": [f"{index:06d}.SZ" for index in range(6000)],
                        "trade_date": ["20260922"] * 6000,
                        "open": [1.0] * 6000,
                        "high": [1.0] * 6000,
                        "low": [1.0] * 6000,
                        "close": [1.0] * 6000,
                        "vol": [1.0] * 6000,
                        "amount": [1.0] * 6000,
                    }
                )

        result = RealV2Provider(token="configured", transport=LimitTransport()).daily("20260922")

        self.assertEqual(result.status, "PARTIAL")
        self.assertEqual(result.code, "TRUNCATION_SUSPECTED")
        self.assertEqual(len(result.frame), 6000)

    def test_sector_membership_fetches_current_and_removed_rows_without_offset_assumption(self) -> None:
        transport = _MembershipTransport()

        result = RealV2Provider(token="configured", transport=transport).sector_memberships(["801010.SI"])

        self.assertEqual(result.status, "OK")
        self.assertEqual([call["is_new"] for call in transport.calls], ["Y", "N"])
        self.assertEqual(set(result.frame["history_mode"]), {"RECONSTRUCTED_PIT"})
        self.assertEqual(set(result.frame["sector_id"]), {"801010.SI"})

    def test_execution_metadata_endpoints_use_documented_parameters_and_whitelist_columns(self) -> None:
        provider = RealV2Provider(token="configured", transport=_MetadataTransport())

        suspension = provider.suspensions("20260922")
        actions = provider.corporate_actions(["600000.SH"])
        historical = provider.historical_instruments("20260922")

        self.assertEqual(suspension.status, "OK")
        self.assertTrue(suspension.frame.loc[0, "is_suspended"])
        self.assertEqual(actions.status, "OK")
        self.assertEqual(actions.frame.loc[0, "cash_per_share"], "0.2")
        self.assertEqual(actions.frame.loc[0, "share_ratio"], "0.1")
        self.assertEqual(historical.status, "OK")
        self.assertEqual(historical.frame.loc[0, "code"], "600000.SH")
        self.assertNotIn("pe", historical.frame.columns)
        self.assertNotIn("pb", historical.frame.columns)

    def test_core_provider_results_can_be_written_without_field_translation(self) -> None:
        provider = RealV2Provider(token="configured", transport=_MetadataTransport())
        instruments = provider.instruments(as_of="20260922")
        adjustments = provider.adjustments("20260922")
        limits = provider.limits("20260922")

        with tempfile.TemporaryDirectory() as tmp_dir:
            store = V2Store(Path(tmp_dir) / "market.db")
            store.migrate()
            instrument_count = store.upsert_instrument_versions(instruments.frame)
            adjustment_count = store.upsert_adjustments(adjustments.frame)
            limit_count = store.upsert_trading_status(limits.frame)

        self.assertEqual((instruments.status, adjustments.status, limits.status), ("OK", "OK", "OK"))
        self.assertEqual((instrument_count, adjustment_count, limit_count), (1, 1, 1))
        self.assertEqual(instruments.frame.loc[0, "listing_board"], "MAIN_SH")
        self.assertEqual(instruments.frame.loc[0, "valid_from"], "20260922")
        self.assertEqual(adjustments.frame.loc[0, "code"], "600000.SH")
        self.assertEqual(limits.frame.loc[0, "rule_version"], "TUSHARE_STK_LIMIT")

    def test_exchange_calendar_resolves_previous_and_next_open_session(self) -> None:
        result = RealV2Provider(token="configured", transport=_MetadataTransport()).calendar(
            "SSE", "20260918", "20260921"
        )

        by_date = result.frame.set_index("date")
        self.assertEqual(result.status, "OK")
        self.assertEqual(by_date.loc["20260919", "previous_session"], "20260918")
        self.assertEqual(by_date.loc["20260919", "next_session"], "20260921")
        self.assertEqual(by_date.loc["20260918", "next_session"], "20260921")

    def test_legacy_data_manager_real_mode_does_not_call_mock_generator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = DataManager(cache_dir=Path(tmp_dir), data_mode="real")
            with mock.patch.object(manager, "_fetch_daily_tushare", return_value=pd.DataFrame()):
                with mock.patch.object(manager, "_mock_daily_data", wraps=manager._mock_daily_data) as mock_generator:
                    result = manager.get_daily_data("000001.SZ", "20260901", "20260922", use_cache=False)

        self.assertTrue(result.empty)
        mock_generator.assert_not_called()

    def test_legacy_data_manager_demo_mode_keeps_synthetic_output_labeled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = DataManager(cache_dir=Path(tmp_dir), data_mode="demo")
            with mock.patch.object(manager, "_fetch_daily_tushare", return_value=pd.DataFrame()):
                result = manager.get_daily_data("000001.SZ", "20260901", "20260922", use_cache=False)

        self.assertFalse(result.empty)
        self.assertEqual(set(result["data_source_mode"]), {"demo"})
        self.assertEqual(set(result["_source"]), {"SYNTHETIC_FIXTURE"})

    def test_real_mode_does_not_replace_missing_exchange_calendar_with_weekdays(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = DataManager(cache_dir=Path(tmp_dir), data_mode="real")
            with mock.patch("core.data.data_manager.get_pro_api", return_value=None):
                sessions = manager._recent_trade_days("20260922", lookback_trade_days=3)

        self.assertEqual(sessions, [])


class V2UniverseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.instruments = pd.DataFrame(
            [
                {"ts_code": "600000.SH", "name": "SH main", "asset_type": "stock", "market": "main", "list_date": "20000101"},
                {"ts_code": "000001.SZ", "name": "SZ main", "asset_type": "stock", "market": "main", "list_date": "20000101"},
                {"ts_code": "300001.SZ", "name": "ChiNext", "asset_type": "stock", "market": "ChiNext", "list_date": "20100101"},
                {"ts_code": "688001.SH", "name": "STAR", "asset_type": "stock", "market": "STAR", "list_date": "20190101"},
                {"ts_code": "900901.SH", "name": "B share", "asset_type": "stock", "market": "B share", "list_date": "20000101"},
                {"ts_code": "510300.SH", "name": "ETF", "asset_type": "fund", "market": "fund", "list_date": "20100101"},
                {"ts_code": "920001.BJ", "name": "Beijing", "asset_type": "stock", "market": "Beijing", "list_date": "20200101"},
            ]
        )

    def test_analysis_universe_includes_main_chinext_star_and_excludes_other_assets(self) -> None:
        universe = build_analysis_universe(self.instruments, as_of="20260922")

        self.assertEqual(
            universe["ts_code"].tolist(),
            ["000001.SZ", "300001.SZ", "600000.SH", "688001.SH"],
        )
        self.assertEqual(
            dict(zip(universe["ts_code"], universe["listing_board"])),
            {
                "000001.SZ": "MAIN_SZ",
                "300001.SZ": "CHINEXT",
                "600000.SH": "MAIN_SH",
                "688001.SH": "STAR",
            },
        )

    def test_risk_warning_remains_in_analysis_but_is_not_trade_eligible(self) -> None:
        row = {
            "ts_code": "600001.SH",
            "name": "*ST sample",
            "asset_type": "stock",
            "market": "main",
            "list_date": "20000101",
        }

        classification = classify_instrument(row, as_of="20260922")

        self.assertTrue(classification.analysis_eligible)
        self.assertFalse(classification.trade_eligible)
        self.assertIn("RISK_WARNING", classification.trade_blockers)

    def test_sector_membership_uses_validity_interval_and_does_not_backfill_current_snapshot(self) -> None:
        memberships = pd.DataFrame(
            [
                {"namespace": "SW_L1", "sector_id": "bank", "ts_code": "600000.SH", "valid_from": "20200101", "valid_to": "20211231", "history_mode": "RECONSTRUCTED_PIT"},
                {"namespace": "CURRENT", "sector_id": "finance", "ts_code": "600000.SH", "valid_from": "20260922", "valid_to": None, "history_mode": "CURRENT_SNAPSHOT_ONLY"},
            ]
        )

        historical = resolve_sector_membership(memberships, as_of="20210601")
        current = resolve_sector_membership(memberships, as_of="20260922")

        self.assertEqual(historical["sector_id"].tolist(), ["bank"])
        self.assertEqual(set(current["sector_id"]), {"finance"})


if __name__ == "__main__":
    unittest.main()
