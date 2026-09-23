from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from core.analysis.sector_v2 import build_context
from core.factors.technical_v2 import (
    DIRECTIONAL_FACTOR_IDS,
    RISK_INDICATOR_IDS,
    TECHNICAL_V2_REGISTRY,
    apply_adjustment_factors,
    compute_technical_v2,
)
from tests.v2.test_sector_context import make_context_fixture


def make_feature_fixture(days: int = 70) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    close_panel, memberships, universe = make_context_fixture(days=days)
    panel = close_panel.copy()
    panel["adjusted_open"] = panel["adjusted_close"] * 0.998
    panel["adjusted_high"] = panel["adjusted_close"] * 1.01
    panel["adjusted_low"] = panel["adjusted_close"] * 0.99
    panel["volume_shares"] = panel["amount_cny"] / panel["adjusted_close"]
    return panel, memberships, universe


class TechnicalV2FeatureTest(unittest.TestCase):
    def _compute(self, panel: pd.DataFrame, memberships: pd.DataFrame, universe: list[str]):
        as_of = panel["date"].max()
        context = build_context(panel, memberships, as_of=as_of, universe=universe)
        return compute_technical_v2(panel, context, as_of=as_of)

    def test_registry_is_exactly_fifteen_directional_and_six_risk_fields(self) -> None:
        self.assertEqual(DIRECTIONAL_FACTOR_IDS, tuple(f"F{index:02d}" for index in range(1, 16)))
        self.assertEqual(RISK_INDICATOR_IDS, tuple(f"Q{index:02d}" for index in range(1, 7)))
        self.assertEqual(set(TECHNICAL_V2_REGISTRY), set(DIRECTIONAL_FACTOR_IDS + RISK_INDICATOR_IDS))
        self.assertEqual({TECHNICAL_V2_REGISTRY[key].kind for key in DIRECTIONAL_FACTOR_IDS}, {"directional"})
        self.assertEqual({TECHNICAL_V2_REGISTRY[key].kind for key in RISK_INDICATOR_IDS}, {"risk"})
        self.assertTrue(all(TECHNICAL_V2_REGISTRY[key].inputs for key in TECHNICAL_V2_REGISTRY))

    def test_empty_panel_returns_fixed_metric_schema(self) -> None:
        columns = [
            "code",
            "date",
            "adjusted_open",
            "adjusted_high",
            "adjusted_low",
            "adjusted_close",
            "volume_shares",
            "amount_cny",
        ]
        empty = pd.DataFrame(columns=columns)
        context = build_context(
            empty[["code", "date", "adjusted_close", "amount_cny"]],
            pd.DataFrame(),
            as_of="20260922",
            universe=[],
        )

        result = compute_technical_v2(empty, context, as_of="20260922")

        self.assertTrue(result.empty)
        self.assertTrue(set(DIRECTIONAL_FACTOR_IDS + RISK_INDICATOR_IDS).issubset(result.columns))

    def test_adjustment_factor_uses_as_of_factor_for_all_ohlc_fields(self) -> None:
        raw = pd.DataFrame(
            [
                {"code": "600000.SH", "date": "20260105", "open": 10, "high": 11, "low": 9, "close": 10},
                {"code": "600000.SH", "date": "20260106", "open": 5, "high": 5.5, "low": 4.5, "close": 5},
            ]
        )
        adjustments = pd.DataFrame(
            [
                {"code": "600000.SH", "date": "20260105", "adj_factor": 1.0},
                {"code": "600000.SH", "date": "20260106", "adj_factor": 2.0},
            ]
        )

        adjusted = apply_adjustment_factors(raw, adjustments, as_of="20260106")

        self.assertEqual(adjusted["adjustment_status"].tolist(), ["OK", "OK"])
        self.assertEqual(adjusted["adjusted_close"].tolist(), [5.0, 5.0])
        self.assertEqual(adjusted["adjusted_high"].tolist(), [5.5, 5.5])

    def test_hand_calculated_momentum_and_flat_range_boundaries(self) -> None:
        panel, memberships, universe = make_feature_fixture()
        code = universe[0]
        stock = panel.loc[panel["code"] == code].copy()
        stock["adjusted_open"] = stock["adjusted_close"]
        stock["adjusted_high"] = stock["adjusted_close"]
        stock["adjusted_low"] = stock["adjusted_close"]
        panel.loc[stock.index, ["adjusted_open", "adjusted_high", "adjusted_low"]] = stock[
            ["adjusted_open", "adjusted_high", "adjusted_low"]
        ]

        result = self._compute(panel, memberships, universe)
        row = result.loc[result["code"] == code].iloc[-1]
        expected = np.tanh(np.log(row["adjusted_close"] / stock.iloc[-21]["adjusted_close"]) / (0.005 * np.sqrt(20)))

        self.assertAlmostEqual(float(row["F01"]), float(expected), places=12)
        self.assertEqual(float(row["F07"]), 1.0)
        self.assertEqual(float(row["F08"]), 0.0)
        self.assertFalse(np.isinf(row[list(DIRECTIONAL_FACTOR_IDS + RISK_INDICATOR_IDS)].astype(float)).any())

    def test_future_append_does_not_change_historical_features(self) -> None:
        panel, memberships, universe = make_feature_fixture(days=75)
        cutoff = sorted(panel["date"].unique())[69]
        before_panel = panel.loc[panel["date"] <= cutoff]

        before = self._compute(before_panel, memberships, universe)
        after = self._compute(panel, memberships, universe)
        columns = list(DIRECTIONAL_FACTOR_IDS + RISK_INDICATOR_IDS) + [f"{key}_reason" for key in DIRECTIONAL_FACTOR_IDS]
        left = before.loc[before["date"] == cutoff, ["code", *columns]].reset_index(drop=True)
        right = after.loc[after["date"] == cutoff, ["code", *columns]].reset_index(drop=True)

        pd.testing.assert_frame_equal(left, right)

    def test_input_order_and_uniform_price_scale_do_not_change_directional_signals(self) -> None:
        panel, memberships, universe = make_feature_fixture()
        baseline = self._compute(panel, memberships, universe)
        changed = panel.sample(frac=1.0, random_state=42).copy()
        for column in ("adjusted_open", "adjusted_high", "adjusted_low", "adjusted_close"):
            changed[column] *= 7.5
        scaled = self._compute(changed, memberships, universe)
        columns = ["code", "date", *DIRECTIONAL_FACTOR_IDS, "Q01", "Q02", "Q04", "Q05", "Q06"]

        pd.testing.assert_frame_equal(
            baseline[columns].reset_index(drop=True),
            scaled[columns].reset_index(drop=True),
            check_exact=False,
            rtol=1e-11,
            atol=1e-12,
        )
        self.assertTrue(
            baseline[list(DIRECTIONAL_FACTOR_IDS)].stack().dropna().between(-1.0, 1.0).all()
        )


if __name__ == "__main__":
    unittest.main()
