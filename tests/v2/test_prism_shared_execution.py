from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal

from core.backtest.execution_v2 import execute_open_orders
from core.backtest.portfolio_v2 import apply_corporate_actions, mark_portfolio
from tests.v2 import test_execution_v2 as fixtures


class SharedExecutionTest(unittest.TestCase):
    setUp = fixtures.ExecutionTest.setUp
    tearDown = fixtures.ExecutionTest.tearDown

    def execute(self, orders, date="20260924", price="10", rule=None):
        quote = replace(self.open_market["600000.SH"], trade_date=date, raw_open=Decimal(price))
        return execute_open_orders(
            self.store, orders, {"600000.SH": quote},
            fee_schedule=self.fees, security_rules={"600000.SH": rule or self.rule},
        ).results

    def test_late_buy_cannot_fill_on_a_later_session(self):
        result = self.execute([self.buy_order], "20260925")[0]
        self.assertEqual(result.status, "EXPIRED_VALIDITY")
        self.assertEqual(self.portfolio.cash_cents(), 100_000_000)
        self.assertEqual(self.store.count_fills(), 0)

    def test_board_minimum_and_increment_never_round_up(self):
        rule = replace(self.rule, minimum_quantity=200, quantity_increment=1, maximum_quantity=100_000)
        self.assertEqual(rule.floor_buy_quantity(199), 0)
        self.assertEqual(rule.floor_buy_quantity(201.9), 201)
        self.assertEqual(rule.floor_sell_quantity(150, 300), 0)
        self.assertEqual(rule.floor_sell_quantity(150, 150), 150)
        bad = replace(self.buy_order, quantity=199)
        self.assertEqual(self.execute([bad], rule=rule)[0].filled_quantity, 0)
        self.assertEqual(self.portfolio.cash_cents(), 100_000_000)

    def test_same_code_exit_blocks_new_entry_even_if_exit_is_pending(self):
        self.execute([self.buy_order])
        sell = replace(self.buy_order, order_id="sell", side="SELL", price_ceiling_floor=None)
        buy = replace(self.buy_order, order_id="rebuy")
        results = self.execute([buy, sell])
        self.assertEqual([r.status for r in results], ["PENDING_EXIT_T1", "EXPIRED_PENDING_EXIT"])
        self.assertEqual(self.store.count_fills(), 1)

    def test_lot_selected_exit_does_not_sell_older_untriggered_lot(self):
        self.execute([self.buy_order])
        second = replace(self.buy_order, order_id="second", earliest_trade_date="20260925")
        self.execute([second], "20260925")
        lot = next(lot for lot in self.portfolio.lots() if lot.entry_date == "20260925")
        sell = replace(
            second, order_id="selected-sell", side="SELL", earliest_trade_date="20260928",
            price_ceiling_floor=None, lot_quantities=((lot.lot_id, 100),),
        )
        self.assertEqual(self.execute([sell], "20260928")[0].filled_quantity, 100)
        self.assertEqual([(x.entry_date, x.quantity) for x in self.portfolio.lots()],
                         [("20260924", 100), ("20260925", 0)])
        cash = self.portfolio.cash_cents()
        self.assertEqual(self.execute([sell], "20260928")[0].status, "ALREADY_FILLED")
        self.assertEqual(self.portfolio.cash_cents(), cash)

    def test_slipped_price_cannot_cross_actual_exchange_limit(self):
        quote = replace(self.open_market["600000.SH"], up_limit=Decimal("10.005"))
        result = execute_open_orders(
            self.store, [self.buy_order], {"600000.SH": quote},
            fee_schedule=self.fees, security_rules={"600000.SH": self.rule},
        ).results[0]
        self.assertEqual(result.status, "EXPIRED_SLIPPED_LIMIT")
        self.assertEqual(self.store.count_fills(), 0)

    def test_confirmed_pending_cash_dividend_is_in_nav_once(self):
        self.execute([self.buy_order])
        event = dict(event_id="div", code="600000.SH", record_date="20260924",
                     ex_date="20260925", pay_date="20260928", cash_per_share="1")
        apply_corporate_actions(self.store, "formula-paper", [event], as_of_date="20260924")
        mark = mark_portfolio(self.store, "formula-paper", "20260924", {"600000.SH": 10})
        self.assertEqual(mark.nav_cents, 100_009_399)
        self.assertEqual(mark.receivable_cents, 10_000)
        apply_corporate_actions(self.store, "formula-paper", [event], as_of_date="20260928")
        settled = mark_portfolio(self.store, "formula-paper", "20260928", {"600000.SH": 10})
        self.assertEqual(settled.nav_cents, 100_009_399)
        self.assertEqual(settled.receivable_cents, 0)


if __name__ == "__main__":
    unittest.main()
