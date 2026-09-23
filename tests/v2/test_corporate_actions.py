from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core.backtest.execution_v2 import (
    FeeSchedule,
    MarketOpen,
    PaperOrder,
    SecurityRule,
    execute_open_orders,
)
from core.backtest.portfolio_v2 import (
    PaperPortfolio,
    apply_corporate_actions,
    mark_portfolio,
)
from core.data.v2_store import V2Store


class CorporateActionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = V2Store(Path(self.tmp.name) / "market.db", data_mode="test")
        self.store.migrate()
        self.portfolio = PaperPortfolio(self.store, "account-a")
        self.portfolio.open_account(method="formula", initial_cash_cents=1_000_000)
        fees = FeeSchedule.research_defaults("20200101", source="fixture-fees")
        rule = SecurityRule(100, Decimal("0.01"), "20200101", None, "fixture-rule")
        execute_open_orders(
            self.store,
            [
                PaperOrder(
                    "buy-a",
                    "run-a",
                    "account-a",
                    "600000.SH",
                    "BUY",
                    100,
                    "20260921",
                    Decimal(10),
                    Decimal("10.20"),
                    "20261008",
                    planned_stop_price=Decimal(9),
                )
            ],
            {
                "600000.SH": MarketOpen(
                    "600000.SH",
                    "20260921",
                    Decimal(10),
                    Decimal(11),
                    Decimal(9),
                    False,
                    False,
                )
            },
            fee_schedule=fees,
            security_rules={"600000.SH": rule},
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cash_and_share_entitlements_are_idempotent_and_not_adj_factor_based(self) -> None:
        event = {
            "event_id": "event-1",
            "code": "600000.SH",
            "record_date": "20260922",
            "pay_date": "20260924",
            "list_date": "20260924",
            "cash_per_share": "0.10",
            "share_ratio": "0.10",
            "split_ratio": None,
            "status": "implemented",
        }

        first = apply_corporate_actions(self.store, "account-a", [event], as_of_date="20260924")
        second = apply_corporate_actions(self.store, "account-a", [event], as_of_date="20260924")

        self.assertEqual(first.applied_events, 1)
        self.assertEqual(second.applied_events, 0)
        self.assertEqual(self.portfolio.cash_cents(), 900_399)
        self.assertEqual(sum(lot.quantity for lot in self.portfolio.lots()), 110)
        ledger = self.portfolio.corporate_action_ledger().iloc[0]
        self.assertEqual(ledger["receivable_cash_cents"], 1000)
        self.assertEqual(ledger["received_cash_cents"], 1000)
        self.assertEqual(ledger["quantity_delta"], 10)
        self.assertEqual(ledger["status"], "SETTLED")

    def test_unresolved_action_and_missing_price_keep_nav_unresolved(self) -> None:
        unresolved = {
            "event_id": "event-unknown",
            "code": "600000.SH",
            "record_date": "20260922",
            "cash_per_share": None,
            "share_ratio": None,
            "split_ratio": None,
            "status": "unknown",
        }
        apply_corporate_actions(self.store, "account-a", [unresolved], as_of_date="20260924")

        action_mark = mark_portfolio(
            self.store,
            "account-a",
            "20260924",
            {"600000.SH": {"price": "10", "mark_only": False}},
        )
        missing_mark = mark_portfolio(self.store, "account-a", "20260924", {})

        self.assertEqual(action_mark.status, "NAV_UNRESOLVED_CORPORATE_ACTION")
        self.assertIsNone(action_mark.nav_cents)
        self.assertEqual(missing_mark.status, "NAV_UNRESOLVED_VALUATION")
        self.assertIsNone(missing_mark.nav_cents)


if __name__ == "__main__":
    unittest.main()
