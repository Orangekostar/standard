from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from core.factors.technical import Alpha9ReversalFactor
from core.factors.volume_price import Alpha6TurnoverCovFactor
from core.strategies.factor_selection import FactorSelectionStrategy


class LegacyCausalityTest(unittest.TestCase):
    def setUp(self) -> None:
        dates = pd.bdate_range("2026-01-05", periods=50)
        self.daily = pd.DataFrame(
            {
                "trade_date": dates,
                "ts_code": "600000.SH",
                "open": np.linspace(10.0, 12.0, len(dates)),
                "high": np.linspace(10.2, 12.2, len(dates)),
                "low": np.linspace(9.8, 11.8, len(dates)),
                "close": np.linspace(10.1, 12.1, len(dates)) + np.sin(np.arange(len(dates))),
                "vol": np.linspace(1000, 2000, len(dates)),
                "amount": np.linspace(10000, 24000, len(dates)),
                "turnover_rate": np.linspace(1.0, 3.0, len(dates)),
            }
        )

    def test_legacy_rank_factors_do_not_change_when_future_rows_are_appended(self) -> None:
        cutoff = 35
        for factor in (Alpha9ReversalFactor(), Alpha6TurnoverCovFactor()):
            before = factor.compute(self.daily.iloc[:cutoff]).reset_index(drop=True)
            after = factor.compute(self.daily).iloc[:cutoff].reset_index(drop=True)
            pd.testing.assert_series_equal(before, after)
            self.assertIn("legacy_causal", factor.name)

    def test_legacy_factor_alias_preserves_configured_weight(self) -> None:
        strategy = FactorSelectionStrategy(
            enabled_factors=["alpha_9_reversal"],
            factor_weights={"alpha_9_reversal": 3.5},
        )

        self.assertEqual(strategy.enabled_factors, ["legacy_causal_alpha9_reversal_v2"])
        self.assertEqual(strategy.factor_weights["legacy_causal_alpha9_reversal_v2"], 3.5)

    def test_intraday_batch_deduplicates_timestamp_before_accumulating_volume(self) -> None:
        strategy = FactorSelectionStrategy(enabled_factors=["momentum_20"])
        trade_date = self.daily["trade_date"].max() + pd.Timedelta(days=1)
        minutes = pd.DataFrame(
            [
                {"trade_time": trade_date + pd.Timedelta(hours=9, minutes=31), "close": 12.2, "vol": 100, "amount": 1220},
                {"trade_time": trade_date + pd.Timedelta(hours=9, minutes=31), "close": 12.3, "vol": 120, "amount": 1476},
                {"trade_time": trade_date + pd.Timedelta(hours=9, minutes=32), "close": 12.4, "vol": 80, "amount": 992},
            ]
        )

        result = strategy.generate_intraday_signals(self.daily, minutes)

        self.assertEqual(len(result), 2)
        self.assertEqual(float(result.iloc[-1]["vol"]), 200.0)
        self.assertEqual(float(result.iloc[-1]["amount"]), 2468.0)

    def test_intraday_signal_discards_daily_rows_at_or_after_minute_date(self) -> None:
        strategy = FactorSelectionStrategy(enabled_factors=["momentum_20"])
        minute_date = self.daily.iloc[-2]["trade_date"]
        minute = {
            "trade_time": minute_date + pd.Timedelta(hours=10),
            "close": 11.5,
            "vol": 10,
            "amount": 115,
        }

        latest, merged, _ = strategy.generate_intraday_signal(self.daily, minute)

        self.assertEqual(pd.Timestamp(latest["trade_date"]), minute_date.normalize())
        self.assertEqual(pd.Timestamp(merged["trade_date"].max()), minute_date.normalize())


if __name__ == "__main__":
    unittest.main()
