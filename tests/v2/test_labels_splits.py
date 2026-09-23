from __future__ import annotations

import hashlib
import unittest

import numpy as np
import pandas as pd

from core.pipeline.technical_v2 import (
    build_fixed_split,
    build_labels,
    build_sector_labels,
    purge_cross_boundary,
    select_cohort,
)


class LabelContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions = ["20260921", "20260922", "20260923", "20260924"]
        self.calendar = pd.DataFrame({"date": self.sessions, "is_open": 1})
        self.prices = pd.DataFrame(
            {
                "code": ["000001.SZ"] * 4,
                "date": self.sessions,
                "adjusted_open": [10.0, 10.0, 10.1, 9.9],
                "Q02": [0.01] * 4,
                "mark_only": [False] * 4,
            }
        )

    def test_h1_uses_next_open_and_open_after_one_held_session(self) -> None:
        label = build_labels(self.prices, self.calendar, horizons=[1]).iloc[0]

        self.assertEqual(label.entry_date, self.sessions[1])
        self.assertEqual(label.exit_date, self.sessions[2])
        self.assertEqual(label.label_end_date, self.sessions[2])
        self.assertAlmostEqual(label.realized_return, 0.01)
        self.assertEqual(label.target_class, "up")

    def test_flat_boundaries_are_inclusive(self) -> None:
        prices = self.prices.copy()
        prices.loc[prices["date"] == "20260923", "adjusted_open"] = 10.05

        row = build_labels(prices, self.calendar, horizons=[1]).iloc[0]

        self.assertAlmostEqual(row.delta, 0.005)
        self.assertEqual(row.target_class, "flat")

    def test_unobservable_and_unmatured_rows_are_retained(self) -> None:
        prices = self.prices.copy()
        prices.loc[prices["date"] == "20260922", "mark_only"] = True

        labels = build_labels(prices, self.calendar, horizons=[1])

        first = labels.loc[labels["as_of_trade_date"] == "20260921"].iloc[0]
        last = labels.loc[labels["as_of_trade_date"] == "20260924"].iloc[0]
        self.assertEqual(first.label_status, "UNOBSERVABLE_LABEL")
        self.assertTrue(np.isnan(first.realized_return))
        self.assertEqual(last.label_status, "LABEL_NOT_MATURED")
        self.assertEqual(last.target_class, "unknown")

    def test_observed_return_is_retained_when_threshold_is_unavailable(self) -> None:
        labels = build_labels(self.prices.drop(columns="Q02"), self.calendar, horizons=[1])
        first = labels.iloc[0]

        self.assertEqual(first.label_status, "TARGET_THRESHOLD_UNAVAILABLE")
        self.assertAlmostEqual(first.realized_return, 0.01)
        self.assertEqual(first.target_class, "unknown")

    def test_sector_label_requires_all_frozen_members(self) -> None:
        stock_labels = build_labels(self.prices, self.calendar, horizons=[1])
        second = self.prices.copy()
        second["code"] = "000002.SZ"
        second["adjusted_open"] = [20.0, 20.0, 20.2, 20.4]
        both = build_labels(pd.concat([self.prices, second], ignore_index=True), self.calendar, horizons=[1])
        assignments = pd.DataFrame(
            [
                {"date": date, "namespace": "SW_L1", "sector_id": "S1", "code": code}
                for date in self.sessions
                for code in ("000001.SZ", "000002.SZ")
            ]
        )
        sector_context = pd.DataFrame(
            [{"date": date, "namespace": "SW_L1", "sector_id": "S1", "sigma20": 0.01} for date in self.sessions]
        )

        complete = build_sector_labels(both, assignments, sector_context)
        without_stock_thresholds = build_sector_labels(
            build_labels(
                pd.concat([self.prices, second], ignore_index=True).drop(columns="Q02"),
                self.calendar,
                horizons=[1],
            ),
            assignments,
            sector_context,
        )
        incomplete = build_sector_labels(stock_labels, assignments, sector_context)

        complete_first = complete.loc[complete["as_of_trade_date"] == "20260921"].iloc[0]
        incomplete_first = incomplete.loc[incomplete["as_of_trade_date"] == "20260921"].iloc[0]
        self.assertEqual(complete_first.label_status, "OK")
        self.assertAlmostEqual(complete_first.realized_return, 0.01)
        self.assertEqual(
            without_stock_thresholds.loc[
                without_stock_thresholds["as_of_trade_date"] == "20260921", "label_status"
            ].iloc[0],
            "OK",
        )
        self.assertEqual(incomplete_first.label_status, "INCOMPLETE_MEMBER_COVERAGE")
        self.assertTrue(np.isnan(incomplete_first.realized_return))


class SplitAndCohortTest(unittest.TestCase):
    def test_fixed_split_uses_last_504_mature_dates_and_two_test_blocks(self) -> None:
        all_sessions = pd.bdate_range("2023-01-02", periods=700).strftime("%Y%m%d").tolist()
        mature_dates = all_sessions[-520:]

        plan = build_fixed_split(mature_dates, all_sessions=all_sessions)

        self.assertEqual(plan.status, "OK")
        counts = plan.assignments.groupby("split")["signal_date"].nunique().to_dict()
        self.assertEqual(counts, {"calibration": 63, "test": 126, "train": 252, "validation": 63})
        test_counts = (
            plan.assignments.loc[plan.assignments["split"] == "test"]
            .groupby("reporting_block")["signal_date"]
            .nunique()
            .to_dict()
        )
        self.assertEqual(test_counts, {"test_1": 63, "test_2": 63})
        self.assertEqual(len(plan.warmup_dates["train"]), 120)
        self.assertEqual(plan.assignments.iloc[0]["signal_date"], mature_dates[-504])

    def test_short_history_is_not_relabelled_as_fixed_protocol(self) -> None:
        plan = build_fixed_split([f"2026{index:04d}" for index in range(100)])

        self.assertEqual(plan.status, "INSUFFICIENT_HISTORY")
        self.assertTrue(plan.assignments.empty)

    def test_purge_requires_label_end_before_next_block(self) -> None:
        rows = pd.DataFrame(
            {
                "label_end_date": ["20260731", "20260803", "20260804", None],
                "value": [1, 2, 3, 4],
            }
        )

        kept = purge_cross_boundary(rows, next_start="2026-08-03")

        self.assertEqual(kept["value"].tolist(), [1])
        self.assertTrue((kept["label_end_date"] < "20260803").all())

    def test_stock_cohort_is_pre_cutoff_sector_round_robin_and_order_invariant(self) -> None:
        universe = pd.DataFrame(
            [
                {"code": "A", "valid_from": "20200101", "valid_to": None},
                {"code": "B", "valid_from": "20200101", "valid_to": None},
                {"code": "C", "valid_from": "20200101", "valid_to": None},
                {"code": "FUTURE", "valid_from": "20270101", "valid_to": None},
            ]
        )
        memberships = pd.DataFrame(
            [
                {"code": "A", "sector_id": "S1", "valid_from": "20200101", "valid_to": None},
                {"code": "B", "sector_id": "S1", "valid_from": "20200101", "valid_to": None},
                {"code": "C", "sector_id": "S2", "valid_from": "20200101", "valid_to": None},
                {"code": "FUTURE", "sector_id": "S3", "valid_from": "20270101", "valid_to": None},
            ]
        )

        selected = select_cohort(
            universe,
            memberships,
            entity_type="stock",
            as_of="20260923",
            seed=20260923,
            max_size=2,
        )
        reordered = select_cohort(
            universe.sample(frac=1, random_state=7),
            memberships.sample(frac=1, random_state=8),
            entity_type="stock",
            as_of="20260923",
            seed=20260923,
            max_size=2,
        )

        self.assertEqual(selected["code"].tolist(), reordered["code"].tolist())
        self.assertEqual(set(selected["sector_id"]), {"S1", "S2"})
        self.assertNotIn("FUTURE", selected["code"].tolist())
        expected_hashes = {
            code: hashlib.sha256(f"seed=20260923|{code}".encode()).hexdigest()
            for code in ("A", "B", "C")
        }
        s1_pick = min(("A", "B"), key=expected_hashes.get)
        self.assertIn(s1_pick, selected["code"].tolist())


if __name__ == "__main__":
    unittest.main()
