from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from core.factors.technical_v2 import DIRECTIONAL_FACTOR_IDS
from core.strategies.formula_v2 import (
    FORMULA_CONFIG_IDS,
    FORMULA_WEIGHTS,
    SECTOR_WEIGHTS,
    estimate_formula_return,
    fit_return_bins,
    score_sector,
    score_stock,
)
from core.technical_v2.config import TechnicalV2Config
from core.technical_v2.contracts import ContractError

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class FormulaV2Test(unittest.TestCase):
    def test_all_positive_factors_score_one_hundred_without_probability_fields(self) -> None:
        result = score_stock({factor_id: 1.0 for factor_id in DIRECTIONAL_FACTOR_IDS}, 5, "F0_BALANCED")

        self.assertEqual(result.status, "OK")
        self.assertEqual(result.formula_score, 100.0)
        self.assertEqual(result.forecast_class, "up")
        self.assertEqual(result.observed_trend, "RISING")
        self.assertIsNone(result.p_raw_up)
        self.assertIsNone(result.p_cal_up)

    def test_group_means_and_variant_weights_match_hand_calculation(self) -> None:
        features = {factor_id: 0.0 for factor_id in DIRECTIONAL_FACTOR_IDS}
        features.update({"F01": 1.0, "F02": 0.5, "F03": 0.0, "F09": -1.0, "F10": -0.5, "F11": 0.0})

        balanced = score_stock(features, 5, "F0_BALANCED")
        trend = score_stock(features, 5, "F1_TREND")

        self.assertAlmostEqual(balanced.group_scores["T"], 0.5)
        self.assertAlmostEqual(balanced.group_scores["V"], -0.5)
        self.assertAlmostEqual(balanced.formula_score, 53.75)
        self.assertAlmostEqual(trend.formula_score, 56.25)

    def test_insufficient_factor_or_missing_group_has_no_score(self) -> None:
        too_few = {factor_id: 0.1 for factor_id in DIRECTIONAL_FACTOR_IDS[:11]}
        missing_relative = {factor_id: 0.1 for factor_id in DIRECTIONAL_FACTOR_IDS if factor_id not in {"F04", "F05"}}

        first = score_stock(too_few, 5, "F0_BALANCED")
        second = score_stock(missing_relative, 5, "F0_BALANCED")

        self.assertEqual(first.status, "INSUFFICIENT_FEATURES")
        self.assertIsNone(first.formula_score)
        self.assertEqual(second.status, "INSUFFICIENT_FEATURES")
        self.assertIn("GROUP_R_UNAVAILABLE", second.reason_codes)

    def test_every_frozen_weight_vector_is_positive_and_sums_to_one(self) -> None:
        self.assertEqual(set(FORMULA_CONFIG_IDS), {"F0_BALANCED", "F1_TREND", "F2_STRUCTURE"})
        for config_id in FORMULA_CONFIG_IDS:
            for horizon in (1, 3, 5):
                weights = FORMULA_WEIGHTS[config_id][horizon]
                self.assertTrue(all(value >= 0 for value in weights.values()))
                self.assertAlmostEqual(sum(weights.values()), 1.0)

    def test_formula_and_sector_weights_match_checked_in_config(self) -> None:
        config = TechnicalV2Config.load(PROJECT_ROOT / "configs" / "technical_v2.json")
        for config_id in FORMULA_CONFIG_IDS:
            deltas = config.factors.formula_variants[config_id]
            for horizon in (1, 3, 5):
                expected = {
                    group: float(config.factors.base_weights[str(horizon)][group])
                    + float(deltas.get(group, 0.0))
                    for group in ("T", "R", "S", "V", "C")
                }
                self.assertEqual(FORMULA_WEIGHTS[config_id][horizon], expected)
        for horizon in (1, 3, 5):
            self.assertEqual(
                SECTOR_WEIGHTS[horizon],
                tuple(float(value) for value in config.factors.sector_weights[str(horizon)]),
            )

    def test_sector_score_uses_five_frozen_components_and_h5_action(self) -> None:
        result = score_sector({"z1": 1.0, "z2": 1.0, "z3": 1.0, "z4": 1.0, "z5": 1.0}, 5)
        diagnostic = score_sector({"z1": -1.0, "z2": -1.0, "z3": -1.0, "z4": -1.0, "z5": -1.0}, 3)

        self.assertEqual(result.formula_score, 100.0)
        self.assertEqual(result.forecast_class, "up")
        self.assertEqual(result.sector_intent, "INCREASE_EXPOSURE")
        self.assertEqual(diagnostic.forecast_class, "down")
        self.assertEqual(diagnostic.sector_intent, "DIAGNOSTIC_ONLY")

    def test_return_bins_use_fixed_support_and_shrinkage(self) -> None:
        rows = []
        scores = [10.0, 25.0, 40.0, 55.0, 70.0, 90.0]
        returns = [-0.03, -0.015, -0.005, 0.005, 0.02, 0.04]
        for index in range(1200):
            bucket = index % len(scores)
            rows.append(
                {
                    "entity_type": "stock",
                    "horizon": 5,
                    "split": "train",
                    "as_of_trade_date": f"2026{1 + (index % 40):04d}",
                    "formula_score": scores[bucket],
                    "realized_return": returns[bucket],
                }
            )
        model = fit_return_bins(pd.DataFrame(rows), entity_type="stock", horizon=5)
        estimate = estimate_formula_return(model, score=70.0, estimated_round_trip_cost=0.003)
        target_bin = next(item for item in model.bins if item.lower == 65.0)
        expected = (target_bin.records * target_bin.mean_return + 100 * model.global_mean) / (target_bin.records + 100)

        self.assertEqual(estimate.status, "OK")
        self.assertAlmostEqual(estimate.expected_gross_return, expected)
        self.assertAlmostEqual(estimate.expected_net_edge, expected - 0.003)
        self.assertEqual(estimate.return_estimate_basis, "ADJUSTED_PRICE_PROXY")

    def test_low_global_support_returns_null_not_zero(self) -> None:
        frame = pd.DataFrame(
            {
                "entity_type": ["stock"] * 20,
                "horizon": [5] * 20,
                "split": ["train"] * 20,
                "as_of_trade_date": [f"202601{index:02d}" for index in range(1, 21)],
                "formula_score": np.linspace(0, 100, 20),
                "realized_return": np.linspace(-0.01, 0.01, 20),
            }
        )
        model = fit_return_bins(frame, entity_type="stock", horizon=5)
        estimate = estimate_formula_return(model, score=80.0, estimated_round_trip_cost=0.003)

        self.assertEqual(estimate.status, "INSUFFICIENT_GLOBAL_SUPPORT")
        self.assertIsNone(estimate.expected_gross_return)
        self.assertIsNone(estimate.expected_net_edge)

    def test_last_bin_includes_one_hundred_and_low_bin_support_uses_global_mean(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "entity_type": "stock",
                    "horizon": 5,
                    "split": "train",
                    "as_of_trade_date": f"D{index % 40:02d}",
                    "formula_score": 100.0 if index == 0 else 10.0,
                    "realized_return": 0.20 if index == 0 else 0.01,
                }
                for index in range(1000)
            ]
        )
        model = fit_return_bins(frame, entity_type="stock", horizon=5)
        estimate = estimate_formula_return(model, score=100.0, estimated_round_trip_cost=0.0)

        self.assertEqual(model.bins[-1].records, 1)
        self.assertEqual(estimate.status, "POOLED_LOW_SUPPORT")
        self.assertEqual(estimate.expected_gross_return, model.global_mean)

    def test_return_bin_fit_rejects_non_training_or_mixed_targets(self) -> None:
        base = pd.DataFrame(
            [
                {"entity_type": "stock", "horizon": 5, "split": "train", "as_of_trade_date": "20260101", "formula_score": 50, "realized_return": 0.01},
                {"entity_type": "sector", "horizon": 5, "split": "test", "as_of_trade_date": "20260102", "formula_score": 50, "realized_return": 0.02},
            ]
        )

        with self.assertRaises(ContractError):
            fit_return_bins(base, entity_type="stock", horizon=5)


if __name__ == "__main__":
    unittest.main()
