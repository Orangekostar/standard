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
    allocate_orders,
    execute_open_orders,
)
from core.backtest.portfolio_v2 import PaperPortfolio
from core.data.v2_store import V2Store


class ExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = V2Store(Path(self.tmp.name) / "market.db", data_mode="test")
        self.store.migrate()
        self.portfolio = PaperPortfolio(self.store, "formula-paper")
        self.portfolio.open_account(method="formula", initial_cash_cents=100_000_000)
        self.fees = FeeSchedule(
            effective_from="20200101",
            effective_to=None,
            commission_rate=Decimal("0.0003"),
            minimum_commission_cny=Decimal(5),
            sell_addon_rate=Decimal("0.0005"),
            other_rate_each_side=Decimal("0.00001"),
            slippage_each_side=Decimal("0.001"),
            source="research-assumption-v1",
        )
        self.rule = SecurityRule(
            lot_size=100,
            tick_size=Decimal("0.01"),
            effective_from="20200101",
            effective_to=None,
            source="fixture-rule-v1",
        )
        self.buy_order = PaperOrder(
            order_id="order-buy-1",
            run_id="run-1",
            account_id="formula-paper",
            code="600000.SH",
            side="BUY",
            quantity=100,
            earliest_trade_date="20260924",
            reference_price=Decimal(10),
            price_ceiling_floor=Decimal("10.20"),
            planned_exit_date="20261009",
        )
        self.open_market = {
            "600000.SH": MarketOpen(
                code="600000.SH",
                trade_date="20260924",
                raw_open=Decimal(10),
                up_limit=Decimal(11),
                down_limit=Decimal(9),
                no_price_limit=False,
                is_suspended=False,
            )
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_same_order_run_twice_creates_one_fill_and_one_cash_effect(self) -> None:
        first = execute_open_orders(
            self.store,
            [self.buy_order],
            self.open_market,
            fee_schedule=self.fees,
            security_rules={"600000.SH": self.rule},
        )
        second = execute_open_orders(
            self.store,
            [self.buy_order],
            self.open_market,
            fee_schedule=self.fees,
            security_rules={"600000.SH": self.rule},
        )

        self.assertEqual(self.store.count_fills(self.buy_order.order_id), 1)
        self.assertEqual(first.results[0].status, "FILLED")
        self.assertEqual(second.results[0].status, "ALREADY_FILLED")
        self.assertEqual(first.results[0].fill_price, Decimal("10.01"))
        self.assertEqual(first.results[0].fee_cents, 501)
        self.assertEqual(self.portfolio.cash_cents(), 99_899_399)

    def test_same_day_buy_is_not_sellable_and_next_session_is(self) -> None:
        execute_open_orders(
            self.store,
            [self.buy_order],
            self.open_market,
            fee_schedule=self.fees,
            security_rules={"600000.SH": self.rule},
        )
        lot = self.portfolio.lots()[0]

        self.assertEqual(lot.sellable_quantity("20260924"), 0)
        self.assertEqual(lot.sellable_quantity("20260925"), 100)

    def test_sell_uses_fifo_cost_and_exact_fee_components(self) -> None:
        execute_open_orders(
            self.store,
            [self.buy_order],
            self.open_market,
            fee_schedule=self.fees,
            security_rules={"600000.SH": self.rule},
        )
        sell = PaperOrder(
            order_id="order-sell-1",
            run_id="run-2",
            account_id="formula-paper",
            code="600000.SH",
            side="SELL",
            quantity=100,
            earliest_trade_date="20260925",
            reference_price=Decimal(10),
            price_ceiling_floor=None,
        )
        result = execute_open_orders(
            self.store,
            [sell],
            {
                "600000.SH": MarketOpen(
                    code="600000.SH",
                    trade_date="20260925",
                    raw_open=Decimal(11),
                    up_limit=Decimal(12),
                    down_limit=Decimal(10),
                    no_price_limit=False,
                    is_suspended=False,
                )
            },
            fee_schedule=self.fees,
            security_rules={"600000.SH": self.rule},
        )

        self.assertEqual(result.results[0].fill_price, Decimal("10.98"))
        self.assertEqual(result.results[0].fee_cents, 556)
        self.assertEqual(self.portfolio.cash_cents(), 100_008_643)
        self.assertEqual(self.portfolio.lots()[0].status, "CLOSED")
        self.assertEqual(self.portfolio.closed_trades().iloc[0]["realized_pnl_cents"], 8643)

    def test_price_limit_and_missing_rule_metadata_never_assume_a_fill(self) -> None:
        at_limit = dict(self.open_market)
        at_limit["600000.SH"] = MarketOpen(
            code="600000.SH",
            trade_date="20260924",
            raw_open=Decimal(11),
            up_limit=Decimal(11),
            down_limit=Decimal(9),
            no_price_limit=False,
            is_suspended=False,
        )
        blocked = execute_open_orders(
            self.store,
            [self.buy_order],
            at_limit,
            fee_schedule=self.fees,
            security_rules={"600000.SH": self.rule},
        )
        unknown = PaperOrder(**{**self.buy_order.__dict__, "order_id": "order-buy-unknown"})
        unknown_result = execute_open_orders(
            self.store,
            [unknown],
            self.open_market,
            fee_schedule=self.fees,
            security_rules={},
        )

        self.assertEqual(blocked.results[0].status, "EXPIRED_LIMIT_UP")
        self.assertEqual(unknown_result.results[0].status, "RULE_METADATA_MISSING")
        self.assertEqual(self.store.count_fills(), 0)

    def test_limit_down_exit_remains_pending_and_can_fill_next_session(self) -> None:
        execute_open_orders(
            self.store,
            [self.buy_order],
            self.open_market,
            fee_schedule=self.fees,
            security_rules={"600000.SH": self.rule},
        )
        sell = PaperOrder(
            "order-sell-pending",
            "run-2",
            "formula-paper",
            "600000.SH",
            "SELL",
            100,
            "20260925",
            Decimal(10),
            None,
        )
        blocked = execute_open_orders(
            self.store,
            [sell],
            {
                "600000.SH": MarketOpen(
                    "600000.SH", "20260925", Decimal(9), Decimal(11), Decimal(9), False, False
                )
            },
            fee_schedule=self.fees,
            security_rules={"600000.SH": self.rule},
        )
        retried = execute_open_orders(
            self.store,
            [sell],
            {
                "600000.SH": MarketOpen(
                    "600000.SH", "20260928", Decimal("9.5"), Decimal("10.4"), Decimal("8.6"), False, False
                )
            },
            fee_schedule=self.fees,
            security_rules={"600000.SH": self.rule},
        )

        self.assertEqual(blocked.results[0].status, "PENDING_EXIT_LIMIT_DOWN")
        self.assertEqual(retried.results[0].status, "FILLED")
        self.assertEqual(self.store.count_fills(sell.order_id), 1)

    def test_buy_quantity_only_shrinks_to_affordable_board_lots(self) -> None:
        small = PaperPortfolio(self.store, "small-paper")
        small.open_account(method="formula", initial_cash_cents=102_000)
        oversized = PaperOrder(
            **{
                **self.buy_order.__dict__,
                "order_id": "order-buy-cash-limited",
                "account_id": "small-paper",
                "quantity": 100_000,
            }
        )

        result = execute_open_orders(
            self.store,
            [oversized],
            self.open_market,
            fee_schedule=self.fees,
            security_rules={"600000.SH": self.rule},
        )

        self.assertEqual(result.results[0].status, "PARTIAL_FILLED")
        self.assertEqual(result.results[0].filled_quantity, 100)
        self.assertGreaterEqual(small.cash_cents(), 0)

    def test_allocation_obeys_breadth_stock_risk_adv_and_lot_caps(self) -> None:
        candidate = {
            "code": "600000.SH",
            "horizon": 5,
            "account_action": "BUY",
            "prediction_status": "OK",
            "trade_eligible": True,
            "expected_net_edge": 0.01,
            "formula_score": 80.0,
            "reference_price": 10.0,
            "atr_pct14": 0.02,
            "adv20_cny": 50_000_000,
            "sector_id": "S1",
            "planned_exit_date": "20261009",
        }
        allocated = allocate_orders(
            [candidate],
            run_id="run-a",
            account_id="formula-paper",
            as_of_trade_date="20260923",
            earliest_trade_date="20260924",
            nav_cents=100_000_000,
            cash_cents=100_000_000,
            market_breadth20=0.5,
            holdings=[],
            security_rules={"600000.SH": self.rule},
        )
        blocked = allocate_orders(
            [candidate],
            run_id="run-b",
            account_id="formula-paper",
            as_of_trade_date="20260923",
            earliest_trade_date="20260924",
            nav_cents=100_000_000,
            cash_cents=100_000_000,
            market_breadth20=0.2,
            holdings=[],
            security_rules={"600000.SH": self.rule},
        )

        self.assertEqual(allocated.orders[0].quantity, 10_000)
        self.assertEqual(allocated.orders[0].price_ceiling_floor, Decimal("10.10"))
        self.assertEqual(blocked.orders, ())
        self.assertIn("MARKET_EXPOSURE_CAP_ZERO", blocked.status_rows[0]["reason_codes"])

    def test_invalid_candidate_numeric_value_is_blocked_not_raised(self) -> None:
        invalid = {
            "code": "600000.SH",
            "horizon": 5,
            "account_action": "BUY",
            "prediction_status": "OK",
            "trade_eligible": True,
            "expected_net_edge": "not-a-number",
            "formula_score": None,
            "reference_price": 10,
            "atr_pct14": 0.02,
            "adv20_cny": 50_000_000,
            "sector_id": "S1",
        }

        result = allocate_orders(
            [invalid],
            run_id="run-invalid",
            account_id="formula-paper",
            as_of_trade_date="20260923",
            earliest_trade_date="20260924",
            nav_cents=100_000_000,
            cash_cents=100_000_000,
            market_breadth20=0.5,
            holdings=[],
            security_rules={"600000.SH": self.rule},
        )

        self.assertEqual(result.orders, ())
        self.assertIn("NET_EDGE_BELOW_MINIMUM", result.status_rows[0]["reason_codes"])


if __name__ == "__main__":
    unittest.main()
