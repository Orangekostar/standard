from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from core.models.calibration_v2 import (
    TemperatureFit,
    fit_class_return_means,
    fit_temperature,
    probability_metrics,
    select_formula_candidate,
    select_jev_pool,
    write_selection_artifact,
)
from core.technical_v2.contracts import ContractError

CLASSES = ("up", "flat", "down")


def _calibration_rows() -> pd.DataFrame:
    rows = []
    for index in range(1200):
        label = CLASSES[index % 3]
        correct = index % 5 != 0
        predicted = label if correct else CLASSES[(index + 1) % 3]
        probabilities = {item: 0.025 for item in CLASSES}
        probabilities[predicted] = 0.95
        rows.append(
            {
                "split": "calibration",
                "entity_type": "stock",
                "horizon": 5,
                "pool_id": "J0_EQUAL_POOL",
                "as_of_trade_date": f"D{index % 40:02d}",
                "entity_id": f"E{index:04d}",
                "target_class": label,
                "response_status": "OK",
                "model_id": "jev-1.13.0",
                "data_version": "fixture-v1",
                **{f"p_raw_{key}": value for key, value in probabilities.items()},
            }
        )
    return pd.DataFrame(rows)


class CalibrationTest(unittest.TestCase):
    def test_probability_metrics_are_date_equal_not_record_weighted(self) -> None:
        rows = []
        for index in range(100):
            rows.append(
                {
                    "as_of_trade_date": "D1",
                    "target_class": "up",
                    "p_raw_up": 0.9,
                    "p_raw_flat": 0.05,
                    "p_raw_down": 0.05,
                }
            )
        rows.append(
            {
                "as_of_trade_date": "D2",
                "target_class": "up",
                "p_raw_up": 0.1,
                "p_raw_flat": 0.45,
                "p_raw_down": 0.45,
            }
        )

        metrics = probability_metrics(pd.DataFrame(rows))
        expected = (-np.log(0.9) - np.log(0.1)) / 2

        self.assertAlmostEqual(metrics.log_loss, expected)
        self.assertEqual(metrics.dates, 2)
        self.assertEqual(metrics.records, 101)

    def test_temperature_grid_fit_uses_calibration_only_and_support_gates(self) -> None:
        rows = _calibration_rows()

        fit = fit_temperature(rows, entity_type="stock", horizon=5, pool_id="J0_EQUAL_POOL")
        short = fit_temperature(rows.iloc[:50], entity_type="stock", horizon=5, pool_id="J0_EQUAL_POOL")

        self.assertEqual(fit.status, "OK")
        self.assertIn(fit.temperature, (0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0))
        self.assertGreater(fit.temperature, 1.0)
        self.assertEqual(short.status, "INSUFFICIENT_CALIBRATION_SUPPORT")
        contaminated = rows.copy()
        contaminated.loc[0, "split"] = "test"
        with self.assertRaises(ContractError):
            fit_temperature(contaminated, entity_type="stock", horizon=5, pool_id="J0_EQUAL_POOL")

    def test_class_return_means_are_training_only_date_equal_and_unshrunk(self) -> None:
        rows = []
        for class_index, label in enumerate(CLASSES):
            for index in range(120):
                rows.append(
                    {
                        "split": "train",
                        "entity_type": "stock",
                        "horizon": 5,
                        "as_of_trade_date": f"D{index % 20:02d}",
                        "target_class": label,
                        "realized_return": (-0.02, 0.0, 0.03)[class_index],
                    }
                )
        model = fit_class_return_means(pd.DataFrame(rows), entity_type="stock", horizon=5)

        self.assertEqual(model.status, "OK")
        for label, expected in {"up": -0.02, "flat": 0.0, "down": 0.03}.items():
            self.assertAlmostEqual(model.class_means[label], expected)


class SelectionTest(unittest.TestCase):
    @staticmethod
    def _fit(pool_id: str, temperature: float = 1.0) -> TemperatureFit:
        return TemperatureFit(
            status="OK",
            entity_type="stock",
            horizon=5,
            pool_id=pool_id,
            temperature=temperature,
            records=1000,
            dates=30,
            class_counts={label: 30 for label in CLASSES},
            calibration_log_loss=0.0,
            calibration_id=f"fixture-{pool_id}",
            model_id="jev-1.13.0",
            data_version="fixture-v1",
            reason_codes=(),
        )

    def test_formula_selection_applies_eligibility_then_frozen_ties(self) -> None:
        candidates = pd.DataFrame(
            [
                {"split": "validation", "config_id": "F0_BALANCED", "net_sharpe": 1.00, "turnover": 0.20, "closed_trades": 50, "trade_dates": 30, "max_drawdown": 0.10, "unresolved": False},
                {"split": "validation", "config_id": "F1_TREND", "net_sharpe": 1.04, "turnover": 0.30, "closed_trades": 50, "trade_dates": 30, "max_drawdown": 0.10, "unresolved": False},
                {"split": "validation", "config_id": "F2_STRUCTURE", "net_sharpe": 2.00, "turnover": 0.01, "closed_trades": 50, "trade_dates": 30, "max_drawdown": 0.25, "unresolved": False},
            ]
        )

        selection = select_formula_candidate(candidates)

        self.assertEqual(selection.status, "SELECTED")
        self.assertEqual(selection.selected_config_id, "F0_BALANCED")
        contaminated = candidates.copy()
        contaminated.loc[0, "split"] = "test"
        with self.assertRaises(ContractError):
            select_formula_candidate(contaminated)

    def test_no_eligible_formula_keeps_f0_as_unvalidated_reference(self) -> None:
        candidates = pd.DataFrame(
            [
                {"split": "validation", "config_id": config, "net_sharpe": 1.0, "turnover": 0.2, "closed_trades": 2, "trade_dates": 3, "max_drawdown": 0.1, "unresolved": False}
                for config in ("F0_BALANCED", "F1_TREND", "F2_STRUCTURE")
            ]
        )
        selection = select_formula_candidate(candidates)

        self.assertEqual(selection.status, "NO_ELIGIBLE_CANDIDATE")
        self.assertEqual(selection.selected_config_id, "F0_BALANCED")

    def test_jev_selection_keeps_identity_without_validation_gain_and_ties_to_j0(self) -> None:
        base = _calibration_rows().iloc[:90].copy()
        base["split"] = "validation"
        j1 = base.copy()
        j1["pool_id"] = "J1_WEIGHTED_POOL"
        rows = pd.concat([base, j1], ignore_index=True)
        fits = {
            "J0_EQUAL_POOL": self._fit("J0_EQUAL_POOL"),
            "J1_WEIGHTED_POOL": self._fit("J1_WEIGHTED_POOL"),
        }

        selection = select_jev_pool(rows, fits)

        self.assertEqual(selection.status, "SELECTED")
        self.assertEqual(selection.selected_pool_id, "J0_EQUAL_POOL")
        self.assertEqual(selection.selected_variant, "identity")
        self.assertTrue(all(result.selected_variant == "identity" for result in selection.pool_results))

    def test_jev_selection_uses_calibration_only_when_validation_logloss_improves(self) -> None:
        j0 = _calibration_rows().iloc[:90].copy()
        j0["split"] = "validation"
        j1 = j0.copy()
        j1["pool_id"] = "J1_WEIGHTED_POOL"

        selection = select_jev_pool(
            pd.concat([j0, j1], ignore_index=True),
            {
                "J0_EQUAL_POOL": self._fit("J0_EQUAL_POOL", 2.0),
                "J1_WEIGHTED_POOL": self._fit("J1_WEIGHTED_POOL", 1.0),
            },
        )

        self.assertEqual(selection.selected_pool_id, "J0_EQUAL_POOL")
        self.assertEqual(selection.selected_variant, "calibrated")
        j0_result = next(result for result in selection.pool_results if result.pool_id == "J0_EQUAL_POOL")
        self.assertLess(j0_result.calibrated_metrics.log_loss, j0_result.identity_metrics.log_loss)

    def test_selection_artifact_is_content_addressed_and_immutable(self) -> None:
        formula = select_formula_candidate(
            pd.DataFrame(
                [{"split": "validation", "config_id": "F0_BALANCED", "net_sharpe": 1.0, "turnover": 0.2, "closed_trades": 50, "trade_dates": 30, "max_drawdown": 0.1, "unresolved": False}]
            )
        )
        base = _calibration_rows().iloc[:30].copy()
        base["split"] = "validation"
        j1 = base.copy()
        j1["pool_id"] = "J1_WEIGHTED_POOL"
        jev = select_jev_pool(
            pd.concat([base, j1], ignore_index=True),
            {
                "J0_EQUAL_POOL": self._fit("J0_EQUAL_POOL"),
                "J1_WEIGHTED_POOL": self._fit("J1_WEIGHTED_POOL"),
            },
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "selection.json"
            first_hash = write_selection_artifact(path, formula, jev, metadata={"protocol": "fixed-v1"})
            second_hash = write_selection_artifact(path, formula, jev, metadata={"protocol": "fixed-v1"})
            payload = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(first_hash, second_hash)
            self.assertEqual(payload["selection_sha256"], first_hash)
            with self.assertRaises(ContractError):
                write_selection_artifact(path, formula, jev, metadata={"protocol": "changed"})


if __name__ == "__main__":
    unittest.main()
