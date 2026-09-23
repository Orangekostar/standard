from __future__ import annotations

import unittest

import pandas as pd

from core.backtest.metrics_v2 import calculate_portfolio_metrics


class PortfolioMetricsTest(unittest.TestCase):
    def test_daily_nav_metrics_keep_win_rate_separate_from_positive_days(self) -> None:
        nav = pd.DataFrame(
            {
                "date": ["20260921", "20260922", "20260923"],
                "nav_cents": [100_000, 101_000, 102_000],
                "cash_cents": [100_000, 51_000, 52_000],
                "status": ["OK", "OK", "OK"],
                "sector_exposure": [{}, {"S1": 0.5}, {"S1": 0.49}],
            }
        )
        fills = pd.DataFrame(
            [
                {"quantity": 100, "price": "5", "side": "BUY"},
                {"quantity": 100, "price": "5.1", "side": "SELL"},
            ]
        )
        orders = pd.DataFrame([{"status": "FILLED"}, {"status": "EXPIRED_NO_OPEN"}])
        closed = pd.DataFrame(
            [
                {"status": "CLOSED", "realized_pnl_cents": 100},
                {"status": "CLOSED", "realized_pnl_cents": -50},
            ]
        )

        metrics = calculate_portfolio_metrics(nav, fills=fills, orders=orders, closed_trades=closed)

        self.assertEqual(metrics.status, "OK")
        self.assertAlmostEqual(metrics.net_return, 0.02)
        self.assertEqual(metrics.positive_day_ratio, 1.0)
        self.assertEqual(metrics.closed_trade_win_rate, 0.5)
        self.assertEqual(metrics.fill_count, 2)
        self.assertEqual(metrics.unfilled_order_ratio, 0.5)
        self.assertAlmostEqual(metrics.max_sector_exposure, 0.5)
        self.assertIsNotNone(metrics.annualized_return)
        self.assertIsNotNone(metrics.annualized_volatility)

    def test_short_or_unresolved_nav_does_not_fake_annual_metrics(self) -> None:
        short = pd.DataFrame(
            [{"date": "20260921", "nav_cents": 100_000, "cash_cents": 100_000, "status": "OK"}]
        )
        unresolved = pd.DataFrame(
            [
                {"date": "20260921", "nav_cents": 100_000, "cash_cents": 100_000, "status": "OK"},
                {"date": "20260922", "nav_cents": None, "cash_cents": 100_000, "status": "NAV_UNRESOLVED_VALUATION"},
            ]
        )

        short_metrics = calculate_portfolio_metrics(short)
        unresolved_metrics = calculate_portfolio_metrics(unresolved)

        self.assertIsNone(short_metrics.annualized_return)
        self.assertIsNone(short_metrics.sharpe)
        self.assertEqual(unresolved_metrics.status, "NAV_INCOMPLETE")
        self.assertIsNone(unresolved_metrics.net_return)
        self.assertEqual(unresolved_metrics.unresolved_nav_days, 1)

    def test_classification_metrics_accept_contract_uppercase_labels(self) -> None:
        nav = pd.DataFrame(
            [
                {"date": "20260921", "nav_cents": 100_000, "cash_cents": 100_000, "status": "OK"},
                {"date": "20260922", "nav_cents": 101_000, "cash_cents": 101_000, "status": "OK"},
            ]
        )
        predictions = pd.DataFrame(
            [
                {
                    "as_of_trade_date": "20260921",
                    "target_class": "UP",
                    "forecast_class": "UP",
                    "signal_strength": 0.8,
                    "realized_return": 0.02,
                },
                {
                    "as_of_trade_date": "20260921",
                    "target_class": "DOWN",
                    "forecast_class": "FLAT",
                    "signal_strength": -0.2,
                    "realized_return": -0.01,
                },
            ]
        )

        metrics = calculate_portfolio_metrics(nav, predictions=predictions)

        self.assertEqual(metrics.classification_accuracy, 0.5)
        self.assertAlmostEqual(metrics.mean_rank_ic, 1.0)


if __name__ == "__main__":
    unittest.main()
