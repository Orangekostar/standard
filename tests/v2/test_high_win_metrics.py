import copy
import unittest

import pandas as pd

from core.backtest.high_win_metrics import (
    choose_primary,
    episode_statistics,
    nav_statistics,
    review_qualification,
    selection_summary,
    wilson,
)
from core.technical_v2.contracts import ContractError


TRADE_COLUMNS = [
    "status",
    "lot_id",
    "code",
    "entry_date",
    "closed_at",
    "holding_sessions",
    "entry_cost_cents",
    "realized_pnl_cents",
]


def trade_fixture(pnls=(100, -200, 0), codes=("600000.SH", "600001.SH", "600002.SH")):
    rows = []
    for index, (pnl, code) in enumerate(zip(pnls, codes)):
        rows.append(
            {
                "status": "CLOSED",
                "lot_id": f"lot-{index}",
                "code": code,
                "entry_date": "20240102" if index < 2 else "20240104",
                "closed_at": "20240104" if index < 2 else "20240106",
                "holding_sessions": 2,
                "entry_cost_cents": 10000,
                "realized_pnl_cents": pnl,
            }
        )
    return pd.DataFrame(rows, columns=TRADE_COLUMNS)


def empty_trade_fixture():
    return pd.DataFrame(columns=TRADE_COLUMNS)


def nav_fixture(values=(10000, 9000), statuses=None, positions=None):
    statuses = statuses or ["OK"] * len(values)
    positions = positions or [0] * len(values)
    return pd.DataFrame(
        {
            "date": [f"2024010{index + 2}" for index in range(len(values))],
            "status": statuses,
            "nav_cents": values,
            "cash_cents": values,
            "position_value_cents": positions,
        }
    )


def selection_cells(
    policy_id="S01_CONFIRMED",
    returns=None,
    episode_count=20,
    wins=14,
    losses=6,
):
    returns = returns or [0.02] * 6
    cells = []
    for cost in ("base", "stress"):
        for window, net_return in enumerate(returns, start=1):
            dates = [f"D{window:02d}-{index:03d}" for index in range(episode_count)]
            codes = [f"CODE{index:02d}.SH" for index in range(20)]
            positive_pnl = float(wins * 2)
            negative_pnl = float(losses * 2)
            positive_returns = wins * 0.02
            negative_returns = -losses * 0.02
            cells.append(
                {
                    "strategy_id": policy_id,
                    "split": f"selection_{window}",
                    "cost_scenario": cost,
                    "net_return": net_return,
                    "max_drawdown": 0.03,
                    "nav_complete": True,
                    "open_position_count": 0,
                    "closed_episode_count": episode_count,
                    "wins": wins,
                    "loss_count": losses,
                    "positive_pnl_cny": positive_pnl,
                    "negative_pnl_abs_cny": negative_pnl,
                    "sum_positive_episode_return": positive_returns,
                    "sum_negative_episode_return": negative_returns,
                    "sum_episode_net_return": positive_returns + negative_returns,
                    "distinct_entry_dates": dates,
                    "distinct_codes": codes,
                }
            )
    return cells


def review_fixture():
    return {
        "closed_episode_count": 40,
        "wins": 27,
        "loss_count": 13,
        "win_rate": 27 / 40,
        "wilson95": wilson(27, 40),
        "profit_factor": 2.0,
        "payoff_ratio_return": 1.0,
        "mean_episode_net_return": 0.01,
        "distinct_entry_dates": [f"D{index:02d}" for index in range(20)],
        "distinct_codes": [f"CODE{index:02d}.SH" for index in range(20)],
        "nav_complete": True,
        "net_return": 0.10,
        "max_drawdown": 0.05,
        "open_position_count": 0,
    }


class WilsonTests(unittest.TestCase):
    def test_wilson_interval_and_invalid_counts(self):
        self.assertEqual(wilson(0, 0), [None, None])
        lower, upper = wilson(65, 100)
        self.assertAlmostEqual(lower, 0.55254, places=4)
        self.assertAlmostEqual(upper, 0.73636, places=4)
        with self.assertRaises(ContractError):
            wilson(101, 100)


class EpisodeStatisticsTests(unittest.TestCase):
    def test_closed_episode_statistics_use_economic_lots(self):
        stats = episode_statistics(trade_fixture())

        self.assertEqual(stats["closed_episode_count"], 3)
        self.assertEqual(stats["wins"], 1)
        self.assertEqual(stats["loss_count"], 1)
        self.assertEqual(stats["zero_count"], 1)
        self.assertAlmostEqual(stats["win_rate"], 1 / 3)
        self.assertAlmostEqual(stats["profit_factor"], 0.5)
        self.assertAlmostEqual(stats["payoff_ratio_return"], 0.5)
        self.assertAlmostEqual(stats["mean_episode_net_return"], -1 / 300)
        self.assertEqual(stats["distinct_entry_dates"], ["20240102", "20240104"])
        self.assertEqual(stats["max_holding_sessions"], 2)

    def test_empty_and_all_positive_episodes_keep_undefined_metrics_explicit(self):
        empty = episode_statistics(empty_trade_fixture())
        self.assertIsNone(empty["win_rate"])
        self.assertIsNone(empty["profit_factor"])

        positive = episode_statistics(trade_fixture((100, 200), ("600000.SH", "600001.SH")))
        self.assertIsNone(positive["profit_factor"])
        self.assertEqual(positive["profit_factor_status"], "NO_OBSERVED_LOSSES")

    def test_duplicate_lot_and_overlapping_same_code_episodes_are_rejected(self):
        duplicate = trade_fixture()
        duplicate.loc[1, "lot_id"] = duplicate.loc[0, "lot_id"]
        with self.assertRaises(ContractError):
            episode_statistics(duplicate)

        overlapping = trade_fixture()
        overlapping.loc[1, "code"] = overlapping.loc[0, "code"]
        overlapping.loc[0, "closed_at"] = "20240105"
        overlapping.loc[1, "entry_date"] = "20240104"
        with self.assertRaises(ContractError):
            episode_statistics(overlapping)


class NavStatisticsTests(unittest.TestCase):
    def test_nav_return_drawdown_and_incomplete_status(self):
        stats = nav_statistics(nav_fixture(), 10000)
        self.assertTrue(stats["nav_complete"])
        self.assertAlmostEqual(stats["net_return"], -0.10)
        self.assertAlmostEqual(stats["max_drawdown"], 0.10)

        incomplete = nav_fixture(statuses=["OK", "INCOMPLETE"])
        incomplete_stats = nav_statistics(incomplete, 10000)
        self.assertFalse(incomplete_stats["nav_complete"])
        self.assertIsNone(incomplete_stats["net_return"])

    def test_cash_only_nav_has_flat_days_and_undefined_sharpe(self):
        stats = nav_statistics(nav_fixture(values=(10000, 10000), positions=(0, 0)), 10000)
        self.assertIsNone(stats["sharpe"])
        self.assertAlmostEqual(stats["flat_day_ratio"], 1.0)


class SelectionSummaryTests(unittest.TestCase):
    def test_exact_twelve_cells_pool_episodes_and_zero_return_windows(self):
        cells = selection_cells(returns=[0.02, 0.02, 0.0, 0.0, -0.01, -0.01])
        summary = selection_summary("S01_CONFIRMED", cells)

        self.assertTrue(summary["qualified"])
        self.assertEqual(len(cells), 12)
        for cost in ("base", "stress"):
            stats = summary["costs"][cost]
            self.assertEqual(stats["closed_episode_count"], 120)
            self.assertEqual(stats["wins"], 84)
            self.assertEqual(stats["loss_count"], 36)
            self.assertEqual(len(stats["distinct_entry_dates"]), 120)
            self.assertEqual(len(stats["distinct_codes"]), 20)
            self.assertEqual(stats["nonnegative_windows"], 4)
            self.assertEqual(stats["positive_windows"], 2)
            self.assertGreater(stats["mean_window_return"], 0)

    def test_selection_requires_six_early_windows_and_two_costs(self):
        cells = selection_cells()
        wrong_split = [dict(cells[0], split="holdout_1"), *cells[1:]]
        missing = cells[:-1]
        duplicate = [*cells, dict(cells[0])]
        for rows in (wrong_split, missing, duplicate):
            with self.assertRaises(ContractError):
                selection_summary("S01_CONFIRMED", rows)

    def test_selection_rejects_pooled_sub65_rate_and_open_positions(self):
        low_rate = selection_cells(episode_count=100, wins=64, losses=36)
        low_rate_summary = selection_summary("S01_CONFIRMED", low_rate)
        self.assertFalse(low_rate_summary["qualified"])
        self.assertIn("base:WIN_RATE_BELOW_65_PERCENT", low_rate_summary["rejections"])
        self.assertIn("stress:WIN_RATE_BELOW_65_PERCENT", low_rate_summary["rejections"])

        open_position = selection_cells()
        open_position[0]["open_position_count"] = 1
        open_summary = selection_summary("S01_CONFIRMED", open_position)
        self.assertFalse(open_summary["qualified"])
        self.assertIn("base:UNRESOLVED_ASSETS", open_summary["rejections"])


class PrimarySelectionTests(unittest.TestCase):
    @staticmethod
    def summary(policy_id, qualified=True, robust=0.0, wilson_lower=0.0, drawdown=0.0):
        return {
            "policy_id": policy_id,
            "qualified": qualified,
            "robust_mean_return": robust,
            "robust_wilson_lower": wilson_lower,
            "worst_drawdown": drawdown,
        }

    def test_choose_primary_ignores_controls_and_uses_frozen_sort_keys(self):
        summaries = [
            self.summary("BASE_ENV", robust=99.0, wilson_lower=99.0, drawdown=0.0),
            self.summary("BASE_DEF", robust=100.0, wilson_lower=100.0, drawdown=0.0),
            self.summary("S01", robust=0.5, wilson_lower=0.60, drawdown=0.10),
            self.summary("S02", robust=0.5, wilson_lower=0.60, drawdown=0.20),
            self.summary("S03", robust=0.5, wilson_lower=0.55, drawdown=0.01),
            self.summary("S04", qualified=False, robust=1000.0, wilson_lower=1000.0, drawdown=0.0),
        ]
        selected = choose_primary(summaries)
        self.assertEqual(selected["primary_policy_id"], "S01")
        self.assertEqual(selected["selected_policy_ids"], ["S01", "S02", "S03"])
        self.assertEqual(selected["qualified_count"], 3)

    def test_choose_primary_returns_none_when_no_s_candidate_qualifies(self):
        selected = choose_primary(
            [
                self.summary("BASE_ENV", qualified=True, robust=10.0),
                self.summary("BASE_DEF", qualified=True, robust=9.0),
                self.summary("S01", qualified=False, robust=100.0),
            ]
        )
        self.assertIsNone(selected["primary_policy_id"])
        self.assertEqual(selected["selected_policy_ids"], [])
        self.assertEqual(selected["verdict"], "NO_HIGH_WIN_CANDIDATE")


class ReviewQualificationTests(unittest.TestCase):
    def test_review_requires_samples_dates_wilson_and_return_above_base(self):
        primary = review_fixture()
        self.assertEqual(review_qualification(primary, {"nav_complete": True, "net_return": 0.05}), [])

        insufficient = copy.deepcopy(primary)
        insufficient["closed_episode_count"] = 29
        insufficient["distinct_entry_dates"] = insufficient["distinct_entry_dates"][:19]
        reasons = review_qualification(insufficient, {"nav_complete": True, "net_return": 0.05})
        self.assertIn("INSUFFICIENT_TRADES", reasons)

        below_baseline = review_qualification(primary, {"nav_complete": True, "net_return": 0.10})
        self.assertIn("NOT_ABOVE_BASE_ENV_RETURN", below_baseline)


if __name__ == "__main__":
    unittest.main()
