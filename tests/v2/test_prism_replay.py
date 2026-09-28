from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from core.backtest.prism_compare_engine import replay_frames
from core.pipeline.prism_compare_config import load_config
from core.strategies.formula_v2 import ReturnBinModel, ReturnBinStats


class PrismReplayTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = load_config()
        self.sessions = pd.bdate_range("2024-01-02", periods=15).strftime("%Y%m%d").tolist()

    def model(self, gross=.03):
        return ReturnBinModel("stock", 5, 2000, 40, gross, (
            ReturnBinStats(0, 100, True, 2000, 40, gross),
        ))

    def frames(self, signals=1):
        result = []
        for index, date in enumerate(self.sessions[:signals + 6]):
            row = dict(
                code="600000.SH", date=date, execution_open=10., valuation_close=10.,
                comparison_close=10., close=10., adj_factor=1., real_bar=True,
                roster_active=True, instrument_type="STOCK", listing_board="MAIN_SH",
                list_date="19991110", delist_date=None, sector_id="BANK",
                is_risk_warning=False, is_suspended=False, no_price_limit=False,
                up_limit=13., down_limit=6., feature_status="OK", prediction_status="OK",
                score1=70., score3=70., score5=70. if index < signals else 50.,
                overextended=False, Q01=.02, Q02=.02, Q02_prior60_median=.02, Q03=1e8,
                market_breadth20=.7, market_context_status="OK", sector_context_status="OK",
                market_state="TREND_EXPANSION", state_multiplier=1., state_reason="",
            )
            result.append((date, pd.DataFrame([row])))
        return result

    def run_replay(self, name, frames, strategy="A0_V2_F0", signals=1, config=None,
                   gross=.03, cost="base", lambda_c=1.):
        output = self.root / name
        result = replay_frames(
            frames, sessions=self.sessions, signal_dates=self.sessions[:signals],
            config=config or self.config, strategy_id=strategy, split="validation",
            cost_scenario=cost, model=self.model(gross), lambda_c=lambda_c,
            output_dir=output, run_id="fixture", scope_flags=(),
        )
        return result, output

    def read(self, output, name):
        return pd.read_csv(output / f"{name}.csv.gz", dtype={"date": str, "trade_date": str})

    def test_disabled_b_has_identical_actions_orders_and_nav_to_a(self):
        config = copy.deepcopy(self.config)
        policy = config["strategies"]["B0_PRISM_A_SHARE_V1"]
        for key in ("enable_market_state", "enable_adaptive_stop", "enable_cooldown"):
            policy[key] = False
        _, a = self.run_replay("a", self.frames())
        _, b = self.run_replay("b", self.frames(), "B0_PRISM_A_SHARE_V1", config=config)
        for name, columns in (
            ("daily_nav", ["date", "nav_cents", "cash_cents", "position_value_cents", "status"]),
            ("decisions", ["date", "code", "account_action", "allocated_quantity", "primary_blocker"]),
            ("orders", ["code", "side", "quantity", "earliest_trade_date", "status"]),
        ):
            pd.testing.assert_frame_equal(self.read(a, name)[columns], self.read(b, name)[columns])
        fills = self.read(a, "fills")
        self.assertEqual(fills.trade_date.tolist(), [self.sessions[1], self.sessions[6]])
        self.assertEqual(fills.side.tolist(), ["BUY", "SELL"])

    def test_entry_close_stop_only_sells_at_next_sellable_open(self):
        frames = self.frames()
        frames[1][1].loc[0, ["close", "valuation_close", "comparison_close"]] = 9.
        result, output = self.run_replay("b", frames, "B0_PRISM_A_SHARE_V1")
        fills = self.read(output, "fills")
        self.assertEqual(fills.trade_date.tolist(), [self.sessions[1], self.sessions[2]])
        self.assertEqual(result["metrics"]["closed_trade_count"], 1)
        closed = self.read(output, "closed_trades")
        self.assertEqual(closed.exit_reason.tolist(), ["PRISM_CLOSE_STOP"])

    def test_two_lots_keep_their_own_actual_entry_expiry(self):
        frames = self.frames(signals=2)
        for index in range(1, len(frames)):
            frame = frames[index][1]
            frame.loc[0, ["close", "valuation_close", "comparison_close"]] = 9.
            if index > 1:
                frame.loc[0, "execution_open"] = 9.
        result, output = self.run_replay("lots", frames, signals=2)
        fills = self.read(output, "fills")
        buys = fills.loc[fills.side.eq("BUY")]
        sells = fills.loc[fills.side.eq("SELL")]
        self.assertEqual(len(buys), 2)
        self.assertEqual(sells.trade_date.tolist(), [self.sessions[6], self.sessions[7]])
        self.assertEqual(sells.quantity.tolist(), buys.quantity.tolist())
        self.assertEqual(result["metrics"]["closed_trade_count"], 2)

    def test_expiry_stop_and_score_reduce_do_not_double_count_or_sell_an_untriggered_lot(self):
        frames = self.frames(signals=2)
        for index in range(1, len(frames)):
            frames[index][1].loc[0, ["close", "valuation_close", "comparison_close"]] = 9.8
            if index > 1:
                frames[index][1].loc[0, "execution_open"] = 9.8
        frames[5][1].loc[0, ["close", "valuation_close", "comparison_close"]] = 9.45
        frames[5][1].loc[0, "score5"] = 42.
        _, output = self.run_replay("dedupe", frames, "B0_PRISM_A_SHARE_V1", signals=2)
        fills = self.read(output, "fills")
        buys, sells = fills.loc[fills.side.eq("BUY")], fills.loc[fills.side.eq("SELL")]
        self.assertEqual(len(buys), 2)
        self.assertEqual(sells.trade_date.tolist(), [self.sessions[6], self.sessions[7]])
        self.assertEqual(sells.quantity.tolist(), buys.quantity.tolist())
        closed = self.read(output, "closed_trades")
        self.assertEqual(closed.iloc[0].exit_reason, "FIXED_5_SESSION_EXPIRY")

    def test_partial_score_reduction_does_not_close_the_lot_or_arm_full_exit_cooldown(self):
        frames = self.frames(signals=4)
        frames[2][1].loc[0, "score5"] = 42.
        frames[3][1].loc[0, ["close", "valuation_close", "comparison_close"]] = 9.8
        frames[4][1].loc[0, "execution_open"] = 9.8
        result, output = self.run_replay("partial", frames, "B0_PRISM_A_SHARE_V1", signals=4)
        fills = self.read(output, "fills")
        self.assertEqual(fills.iloc[1].trade_date, self.sessions[3])
        self.assertEqual(fills.iloc[1].side, "SELL")
        self.assertLess(fills.iloc[1].quantity, fills.iloc[0].quantity)
        self.assertEqual(fills.iloc[2].trade_date, self.sessions[4])
        self.assertEqual(fills.iloc[2].side, "BUY")
        closed = self.read(output, "closed_trades")
        self.assertNotIn(self.sessions[3], closed.closed_at.astype(str).tolist())
        self.assertEqual(result["metrics"]["closed_trade_count"], 2)

    def test_pending_stop_does_not_arm_cooldown_until_real_full_sale(self):
        frames = self.frames(signals=8)
        frames[1][1].loc[0, ["close", "valuation_close", "comparison_close"]] = 9.
        frames[2][1].loc[0, "is_suspended"] = True
        frames[3][1].loc[0, "is_suspended"] = True
        _, output = self.run_replay("cooldown", frames, "B0_PRISM_A_SHARE_V1", signals=8)
        fills = self.read(output, "fills")
        self.assertEqual(fills.iloc[1].trade_date, self.sessions[4])
        self.assertEqual(fills.iloc[1].side, "SELL")
        self.assertEqual(fills.iloc[2].trade_date, self.sessions[7])
        self.assertEqual(fills.iloc[2].side, "BUY")
        decisions = self.read(output, "decisions").set_index("date")
        self.assertIn("PENDING_EXIT_BLOCKS_NEW_RISK", decisions.loc[self.sessions[2], "all_blockers"])
        self.assertIn("COOLDOWN_ACTIVE", decisions.loc[self.sessions[4], "all_blockers"])
        self.assertIn("COOLDOWN_ACTIVE", decisions.loc[self.sessions[5], "all_blockers"])

    def test_missing_mark_is_carried_but_known_delisting_is_unresolved_not_zero(self):
        frames = self.frames()
        for index in range(2, len(frames)):
            frames[index][1].loc[0, ["close", "valuation_close", "comparison_close", "execution_open"]] = np.nan
            frames[index][1].loc[0, "real_bar"] = False
        for index in range(3, len(frames)):
            frames[index][1].loc[0, "delist_date"] = self.sessions[3]
            frames[index][1].loc[0, "roster_active"] = False
        result, output = self.run_replay("missing", frames)
        nav = self.read(output, "daily_nav")
        self.assertGreater(nav.iloc[2].position_value_cents, 0)
        self.assertIn("600000.SH", nav.iloc[2].mark_only_codes)
        self.assertTrue(pd.isna(nav.iloc[3].nav_cents))
        self.assertEqual(result["metrics"]["unresolved_asset_count"], 1)
        remaining = pd.read_csv(output / "remaining_positions.csv")
        self.assertGreater(remaining.iloc[0].quantity, 0)
        self.assertEqual(remaining.iloc[0].valuation_status, "TERMINAL_UNRESOLVED")

    def test_prefilter_rejections_are_not_actual_unfilled_orders_or_shared_accounts(self):
        frames = self.frames()
        frames[0][1]["is_risk_warning"] = pd.Series([None], dtype="boolean")
        result, output = self.run_replay("blocked", frames)
        self.assertEqual(result["metrics"]["rejected_candidate_count"], 1)
        self.assertIsNone(result["metrics"]["unfilled_order_ratio"])
        self.assertEqual(result["metrics"]["final_nav"], 1000000)
        c, control = self.run_replay("control", self.frames(), "C0_V2_EXPOSURE_CONTROL", lambda_c=0.)
        self.assertEqual(c["metrics"]["filled_buy_count"], 0)
        self.assertEqual(c["metrics"]["final_nav"], 1000000)
        a, _ = self.run_replay("independent", self.frames())
        self.assertEqual(a["metrics"]["filled_buy_count"], 1)
        self.assertEqual(json.loads((output / "metrics.json").read_text())["final_nav"], 1000000)
        self.assertNotEqual(output / "account.db", control / "account.db")

    def test_open_fill_ignores_same_session_feature_validity(self):
        frames = self.frames()
        frames[1][1].loc[0, ["close", "valuation_close", "comparison_close", "Q03"]] = np.nan
        frames[1][1].loc[0, "real_bar"] = False
        result, output = self.run_replay("open", frames)
        fills = self.read(output, "fills")
        self.assertEqual(result["metrics"]["filled_buy_count"], 1)
        self.assertEqual(fills.iloc[0].trade_date, self.sessions[1])
        self.assertEqual(fills.iloc[0].price, 10.01)

    def test_native_lowercase_stock_type_is_not_rejected_as_unknown(self):
        frames = self.frames()
        for _, frame in frames:
            frame.loc[0, "instrument_type"] = "stock"
        result, _ = self.run_replay("native-type", frames)
        self.assertEqual(result["metrics"]["filled_buy_count"], 1)

    def test_minimum_commission_and_stress_rerun_quantity_specific_edge(self):
        frames = self.frames()
        frames[0][1].loc[0, "Q03"] = 100000.
        base, output = self.run_replay("fees", frames, gross=.014)
        stress, stressed_output = self.run_replay("stress", frames, gross=.014, cost="stress")
        fill = self.read(output, "fills").iloc[0]
        self.assertEqual(fill.quantity, 100)
        self.assertEqual(fill.price, 10.01)
        self.assertEqual(fill.fee_cents, 501)
        self.assertEqual(base["metrics"]["filled_buy_count"], 1)
        self.assertEqual(stress["metrics"]["filled_buy_count"], 0)
        decisions = self.read(stressed_output, "decisions")
        self.assertIn("QUANTITY_SPECIFIC_NET_EDGE", decisions.iloc[0].all_blockers)


if __name__ == "__main__":
    unittest.main()
