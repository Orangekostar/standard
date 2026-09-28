from __future__ import annotations

import tempfile
import unittest
import json
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
from core.technical_v2.contracts import canonical_json


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

    def share_event(self, ratio="0.10"):
        return dict(event_id="shares", code="600000.SH", record_date="20260922",
                    ex_date="20260923", list_date="20260925", share_ratio=ratio,
                    split_ratio=None, cash_per_share=None, pay_date=None, status="implemented")

    def sell(self, order_id, date, quantity):
        return execute_open_orders(self.store,
            [PaperOrder(order_id, "run-a", "account-a", "600000.SH", "SELL", quantity, date, Decimal("9.09"), None)],
            {"600000.SH": MarketOpen("600000.SH", date, Decimal("9.09"), Decimal(11), Decimal(8), False, False)},
            fee_schedule=FeeSchedule.research_defaults("20200101", source="fixture-fees"),
            security_rules={"600000.SH": SecurityRule(100, Decimal(".01"), "20200101", None, "fixture-rule")})

    def test_pending_listed_share_entitlement_is_valued_without_a_listing_day_nav_jump(self):
        event = self.share_event()
        apply_corporate_actions(self.store, "account-a", [event], as_of_date="20260922")
        apply_corporate_actions(self.store, "account-a", [event], as_of_date="20260923")
        pending = mark_portfolio(self.store, "account-a", "20260923", {"600000.SH": Decimal("9.09")})
        self.assertEqual(pending.position_value_cents, 90900)
        self.assertEqual(pending.receivable_cents, 9090)
        apply_corporate_actions(self.store, "account-a", [event], as_of_date="20260925")
        listed = mark_portfolio(self.store, "account-a", "20260925", {"600000.SH": Decimal("9.09")})
        self.assertEqual(listed.receivable_cents, 0)
        self.assertEqual(listed.nav_cents, pending.nav_cents)

    def test_bonus_lot_inherits_expiry_and_comparison_reference_and_is_one_economic_trade(self):
        parent = self.portfolio.lots()[0]
        metadata = {**parent.metadata, "comparison_stop": 9.3, "highest_comparison_close": 10.5,
                    "entry_regime": "RANGE", "entry_sector_id": "BANK"}
        with self.store._write_connection() as conn:
            conn.execute("UPDATE paper_lots SET metadata_json=? WHERE lot_id=?", (canonical_json(metadata), parent.lot_id))
        event = self.share_event()
        apply_corporate_actions(self.store, "account-a", [event], as_of_date="20260922")
        self.sell("parent-exit", "20260923", 100)
        self.assertTrue(self.portfolio.closed_trades().empty)
        apply_corporate_actions(self.store, "account-a", [event], as_of_date="20260925")
        child = next(lot for lot in self.portfolio.lots() if lot.status == "OPEN")
        self.assertEqual(child.quantity, 10)
        self.assertEqual(child.planned_exit_date, "20261008")
        self.assertEqual(child.metadata["economic_lot_id"], parent.lot_id)
        self.assertEqual(child.metadata["comparison_stop"], 9.3)
        self.assertEqual(child.metadata["highest_comparison_close"], 10.5)
        self.sell("bonus-exit", "20260926", 10)
        closed = self.portfolio.closed_trades()
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed.iloc[0].lot_id, parent.lot_id)
        self.assertEqual(closed.iloc[0].realized_pnl_cents, -1772)

    def test_first_action_processing_after_parent_sale_uses_record_date_quantity(self):
        self.sell("early-sale", "20260923", 100)
        result = apply_corporate_actions(self.store, "account-a", [self.share_event()], as_of_date="20260925")
        self.assertEqual(result.applied_events, 1)
        self.assertEqual(sum(lot.quantity for lot in self.portfolio.lots()), 10)
        self.assertEqual(self.portfolio.corporate_action_ledger().iloc[0].quantity_delta, 10)

    def test_zero_fractional_share_entitlement_does_not_remain_pending_forever(self):
        apply_corporate_actions(self.store, "account-a", [self.share_event("0.001")], as_of_date="20260925")
        self.assertEqual(self.portfolio.corporate_action_ledger().iloc[0].status, "SETTLED")
        self.assertEqual(sum(lot.quantity for lot in self.portfolio.lots()), 100)


if __name__ == "__main__":
    unittest.main()
