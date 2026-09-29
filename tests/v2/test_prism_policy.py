from __future__ import annotations

import unittest
import copy
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from core.pipeline.prism_compare_config import load_config, reference_cost, write_json
from core.technical_v2.contracts import ContractError
from core.strategies.prism_a_share import adaptive_distance, entry_allowed, market_regimes, update_stop


class PrismPolicyTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def context(self):
        # Alternating positive log returns keep both positive median volatility and ER=1.
        returns = np.tile([.001, .003], 55)
        return pd.DataFrame(dict(date=[f"{i:08d}" for i in range(110)],
                                 log_return=returns, log_return20=.04,
                                 breadth20=.6, status="OK"))

    def test_market_uses_full_prior_window_and_future_append_is_causal(self):
        frame = self.context()
        states = market_regimes(frame, self.config)
        self.assertEqual(states.iloc[78].market_state, "UNKNOWN")
        self.assertEqual(states.iloc[79].market_state, "TREND_EXPANSION")
        self.assertAlmostEqual(states.iloc[79].ER20, 1)
        self.assertEqual(states.iloc[79].state_multiplier, 1)
        changed = pd.concat([frame, frame.tail(1).assign(date="99999999", log_return=-.5)], ignore_index=True)
        pd.testing.assert_frame_equal(states, market_regimes(changed, self.config).iloc[:110].reset_index(drop=True))

    def test_market_unknown_precedes_stress_and_range_does_not_force_exits(self):
        frame = self.context()
        frame.loc[109, "breadth20"] = .2
        states = market_regimes(frame, self.config)
        self.assertEqual(states.iloc[-1].market_state, "STRESS")
        self.assertEqual(states.iloc[-1].state_multiplier, 0)
        frame.loc[109, "log_return"] = np.nan
        self.assertEqual(market_regimes(frame, self.config).iloc[-1].market_state, "UNKNOWN")
        frame = self.context()
        frame.loc[109, "log_return20"] = -.01
        self.assertEqual(market_regimes(frame, self.config).iloc[-1].state_multiplier, .5)

    def test_adaptive_distance_bounds_fallback_and_missing(self):
        self.assertEqual(adaptive_distance(.04, .1, .001, self.config)[0], .12)
        self.assertEqual(adaptive_distance(.001, .001, .1, self.config)[0], .03)
        self.assertEqual(adaptive_distance(0, .001, .1, self.config)[0], .03)
        distance, reasons = adaptive_distance(.04, .1, None, self.config)
        self.assertEqual(distance, .1)
        self.assertIn("ADAPTIVE_WIDTH_FALLBACK", reasons)
        self.assertIsNone(adaptive_distance(None, .1, .001, self.config)[0])

    def test_stop_tightens_only_and_missing_price_is_not_a_cross(self):
        self.assertEqual(update_stop(95, 110, 100, .12), (98, 110, False))
        self.assertEqual(update_stop(98, 110, 100, .12), (98, 110, False))
        self.assertEqual(update_stop(98, 110, None, .12), (98, 110, False))
        self.assertEqual(update_stop(98, 110, 97, None), (98, 110, True))

    def test_two_full_sessions_after_actual_flat_exit(self):
        self.assertTrue(entry_allowed(10, None, 2))
        self.assertFalse(entry_allowed(11, 10, 2))
        self.assertFalse(entry_allowed(12, 10, 2))
        self.assertTrue(entry_allowed(13, 10, 2))

    def test_fee_reference_comes_from_config_and_stress_is_not_terminal_subtraction(self):
        self.assertAlmostEqual(reference_cost(self.config, "base"), .00312)
        self.assertAlmostEqual(reference_cost(self.config, "stress"), .00512)
        self.config["costs"]["slippage_cases"]["stress"] = .003
        self.assertAlmostEqual(reference_cost(self.config, "stress"), .00712)

    def test_unimplemented_switches_and_non_f0_baselines_cannot_be_silently_accepted(self):
        cases = (("shared_portfolio", "one_day_new_buy_validity", False),
                 ("strategies.A0_V2_F0", "formula", "F1_TREND"),
                 ("strategies.A0_V2_F0", "price_triggered_exit", True),
                 ("evaluation", "risk_free_rate", .02),
                 ("split", "test_account_continues_across_reporting_blocks", False),
                 ("data", "mode", "demo"),
                 ("strategies.B0_PRISM_A_SHARE_V1", "formula", "F1_TREND"),
                 ("strategies.B0_PRISM_A_SHARE_V1.adaptive_stop", "tighten_only", False),
                 ("strategies.C0_V2_EXPOSURE_CONTROL", "scaled_limits", []),
                 ("strategies.C0_V2_EXPOSURE_CONTROL", "refit_lambda_in_test", True),
                 ("strategies.A0_V2_F0", "primary_horizon", 3),
                 ("costs", "slippage_accounting", "terminal_subtraction"),
                 ("evaluation", "bootstrap_method", "independent_trades"),
                 ("split", "account_reset_between_validation_and_test", False),
                 ("delivery", "upload_raw_vendor_db", True),
                 ("strategies.B0_PRISM_A_SHARE_V1", "max_planned_holding_sessions", 8),
                 ("split", "final_tail_sessions", 3),
                 ("split", "test_reporting_blocks", [60, 66]))
        with tempfile.TemporaryDirectory() as directory:
            for group, field, value in cases:
                with self.subTest(group=group, field=field):
                    changed = copy.deepcopy(self.config)
                    section = changed
                    for part in group.split("."):
                        section = section[part]
                    section[field] = value
                    path = Path(directory) / "changed.json"
                    write_json(path, changed)
                    with self.assertRaises(ContractError):
                        load_config(path)


if __name__ == "__main__":
    unittest.main()
