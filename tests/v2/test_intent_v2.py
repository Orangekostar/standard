from __future__ import annotations

import unittest

from core.strategies.intent_v2 import derive_research_intent


def formula_rows(score5: float, score3: float, *, net_edge: float | None = 0.01, overextended: bool = False):
    return [
        {"method": "formula", "horizon": 3, "formula_score": score3, "prediction_status": "OK"},
        {
            "method": "formula",
            "horizon": 5,
            "formula_score": score5,
            "prediction_status": "OK",
            "expected_net_edge": net_edge,
            "overextended": overextended,
        },
    ]


class IntentV2Test(unittest.TestCase):
    def test_non_primary_horizon_is_diagnostic_only(self) -> None:
        result = derive_research_intent(formula_rows(70, 60), None, horizon=3)

        self.assertEqual(result.research_intent, "DIAGNOSTIC_ONLY")
        self.assertIsNone(result.account_action)
        self.assertIsNone(result.order_quantity)

    def test_unheld_formula_buy_requires_h3_filter_and_not_overextended(self) -> None:
        buy = derive_research_intent(formula_rows(70, 60), None, horizon=5)
        weak_h3 = derive_research_intent(formula_rows(70, 50), None, horizon=5)
        extended = derive_research_intent(formula_rows(70, 60, overextended=True), None, horizon=5)

        self.assertEqual(buy.research_intent, "BUY_WATCH")
        self.assertEqual(weak_h3.research_intent, "WAIT")
        self.assertEqual(extended.research_intent, "WAIT")
        self.assertIn("OVEREXTENDED", extended.reason_codes)

    def test_low_known_net_edge_downgrades_buy_but_unknown_remains_conditional(self) -> None:
        low = derive_research_intent(formula_rows(70, 60, net_edge=0.001), None, horizon=5)
        unknown = derive_research_intent(formula_rows(70, 60, net_edge=None), None, horizon=5)

        self.assertEqual(low.research_intent, "WAIT")
        self.assertIn("NET_EDGE_BELOW_MINIMUM", low.reason_codes)
        self.assertEqual(unknown.research_intent, "BUY_WATCH")
        self.assertIn("NET_EDGE_UNAVAILABLE_CONDITIONAL", unknown.reason_codes)

    def test_held_formula_matrix_and_unsellable_block(self) -> None:
        state = {"current_quantity": 500, "sellable_quantity": 0, "account_available": True}

        add = derive_research_intent(formula_rows(70, 60), state, horizon=5)
        hold = derive_research_intent(formula_rows(55, 55), state, horizon=5)
        reduce = derive_research_intent(formula_rows(42, 55), state, horizon=5)
        sell = derive_research_intent(formula_rows(40, 55), state, horizon=5)

        self.assertEqual(add.research_intent, "ADD_WATCH")
        self.assertEqual(add.account_action, "ADD")
        self.assertEqual(hold.research_intent, "HOLD")
        self.assertEqual(reduce.account_action, "BLOCKED")
        self.assertEqual(sell.account_action, "BLOCKED")
        self.assertIn("NO_SELLABLE_QUANTITY", sell.action_blockers)

    def test_jev_prefers_calibrated_probabilities_and_marks_raw_as_unvalidated(self) -> None:
        calibrated_rows = [
            {
                "method": "jev",
                "horizon": 5,
                "prediction_status": "OK",
                "p_raw_up": 0.2,
                "p_raw_flat": 0.2,
                "p_raw_down": 0.6,
                "p_cal_up": 0.6,
                "p_cal_flat": 0.2,
                "p_cal_down": 0.2,
                "expected_net_edge": None,
            }
        ]
        raw_rows = [
            {
                "method": "jev",
                "horizon": 5,
                "prediction_status": "OK",
                "p_raw_up": 0.6,
                "p_raw_flat": 0.2,
                "p_raw_down": 0.2,
                "p_cal_up": None,
                "p_cal_flat": None,
                "p_cal_down": None,
                "expected_net_edge": None,
            }
        ]

        calibrated = derive_research_intent(calibrated_rows, None, horizon=5)
        raw = derive_research_intent(raw_rows, None, horizon=5)

        self.assertEqual(calibrated.research_intent, "BUY_WATCH")
        self.assertNotIn("UNVALIDATED_PROBABILITY", calibrated.reason_codes)
        self.assertEqual(raw.research_intent, "BUY_WATCH")
        self.assertIn("UNVALIDATED_PROBABILITY", raw.reason_codes)


if __name__ == "__main__":
    unittest.main()
