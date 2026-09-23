from __future__ import annotations

import json
import math
import unittest

from core.strategies.jev_v2 import (
    build_jev_state,
    build_questions,
    pool_probabilities,
    validate_answers,
)

GROUP_NAMES = ("trend", "relative", "structure", "volume_price", "context")
HORIZONS = (1, 3, 5)


def _response(*, probability_sum: tuple[float, float, float] = (0.6, 0.3, 0.1)) -> dict:
    answers = {}
    for group in GROUP_NAMES:
        for horizon in HORIZONS:
            answers[f"{group}_h{horizon}"] = {
                "type": "choice",
                "choice": "up",
                "probabilities": dict(zip(("up", "flat", "down"), probability_sum, strict=True)),
                "confidence": 0.7,
            }
    return {
        "model": "jev-1.13.0",
        "answers": answers,
        "usage": {"input_tokens": 321, "output_tokens": 45},
    }


class JevQuestionAndStateTest(unittest.TestCase):
    def test_builds_fifteen_native_choice_questions_with_explicit_semantics(self) -> None:
        for entity_type in ("stock", "sector"):
            with self.subTest(entity_type=entity_type):
                questions = build_questions(entity_type)
                self.assertEqual(len(questions), 15)
                self.assertEqual(
                    set(questions),
                    {f"{group}_h{horizon}" for group in GROUP_NAMES for horizon in HORIZONS},
                )
                for question_id, question in questions.items():
                    horizon = int(question_id.rsplit("h", 1)[1])
                    self.assertEqual(question["type"], "choice")
                    self.assertEqual(set(question["criteria"]), {"up", "flat", "down"})
                    self.assertIn(f"{horizon}-session", question["instructions"])
                    self.assertIn("next session open", question["instructions"])
                    self.assertIn(f"state.targets.h{horizon}.delta", question["instructions"])

    def test_stock_state_is_anonymous_whitelisted_and_rounded(self) -> None:
        state = build_jev_state(
            "stock",
            "600000.SH",
            {
                "F01": 0.6123456789,
                "F02": -0.35,
                "Q01": 0.024567891,
                "code": "600000.SH",
                "name": "private-name",
                "as_of_trade_date": "20260922",
                "future_return": 0.9,
                "formula_score": 99.0,
                "action": "BUY",
            },
            targets={1: 0.005678912, 3: 0.01, 5: 0.02},
            group_scores={"T": 0.2123456789},
            priors={5: {"up": 0.4, "flat": 0.35, "down": 0.25}},
        )

        serialized = json.dumps(state, ensure_ascii=False, sort_keys=True)
        self.assertEqual(state["entity_type"], "stock")
        self.assertTrue(state["entity_id"].startswith("anonymous_"))
        for forbidden in (
            "600000.SH",
            "private-name",
            "20260922",
            "future_return",
            "formula_score",
            '"action"',
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(state["technical_groups"]["T"]["F01"]["value"], 0.612346)
        self.assertEqual(state["technical_groups"]["T"]["F01"]["bucket"], "strong_positive")
        self.assertEqual(state["technical_groups"]["T"]["F02"]["bucket"], "negative")
        self.assertEqual(state["targets"]["h1"]["delta"], 0.00567891)

    def test_sector_state_uses_independent_compact_feature_groups(self) -> None:
        state = build_jev_state(
            "sector",
            "SW_L1:801010",
            {
                "z1": 0.2,
                "z2": 0.3,
                "z3": -0.1,
                "z5": 0.4,
                "distance_to_20d_high": -0.2,
                "close_range_position_20": 0.7,
                "amount_ratio20": 1.2,
                "sector_breadth20": 0.55,
                "market_breadth20": 0.48,
                "market_trend": 0.1,
            },
            targets={1: 0.002, 3: 0.004, 5: 0.006},
        )

        self.assertEqual(set(state["technical_groups"]), {"T", "R", "S", "V", "C"})
        self.assertIn("z3", state["technical_groups"]["R"])
        self.assertNotIn("F01", json.dumps(state))
        self.assertEqual(state["target_basis"], "FROZEN_MEMBER_EQUAL_WEIGHT_ADJUSTED_OPEN_RETURN")


class JevResponseTest(unittest.TestCase):
    def test_validates_and_preserves_native_diagnostics(self) -> None:
        parsed = validate_answers(_response())

        self.assertEqual(parsed.status, "OK")
        self.assertEqual(parsed.model_returned, "jev-1.13.0")
        self.assertEqual(parsed.usage, {"input_tokens": 321, "output_tokens": 45})
        self.assertEqual(parsed.answers["trend_h5"].choice, "up")
        self.assertEqual(parsed.answers["trend_h5"].confidence, 0.7)

    def test_normalizes_once_only_within_tolerance(self) -> None:
        parsed = validate_answers(_response(probability_sum=(0.6004, 0.3, 0.1)))

        self.assertEqual(parsed.status, "OK")
        self.assertEqual(len(parsed.normalized_question_ids), 15)
        self.assertAlmostEqual(sum(parsed.answers["trend_h5"].probabilities.values()), 1.0)

    def test_probability_sum_outside_tolerance_is_invalid(self) -> None:
        parsed = validate_answers(_response(probability_sum=(0.8, 0.3, 0.1)))
        self.assertEqual(parsed.status, "INVALID_RESPONSE")

    def test_rejects_wrong_classes_and_non_finite_values(self) -> None:
        wrong = _response()
        wrong["answers"]["trend_h5"]["probabilities"] = {"up": 0.6, "flat": 0.3, "other": 0.1}
        invalid = _response()
        invalid["answers"]["trend_h5"]["probabilities"]["up"] = math.nan

        self.assertEqual(validate_answers(wrong).status, "INVALID_RESPONSE")
        self.assertEqual(validate_answers(invalid).status, "INVALID_RESPONSE")

    def test_missing_answer_is_partial_and_cannot_be_filled_by_pool(self) -> None:
        response = _response()
        del response["answers"]["context_h5"]
        parsed = validate_answers(response)
        pooled = pool_probabilities(parsed.answers, "J0_EQUAL_POOL", 5)

        self.assertEqual(parsed.status, "PARTIAL_RESPONSE")
        self.assertEqual(pooled.status, "PARTIAL_RESPONSE")
        self.assertIsNone(pooled.probabilities)
        self.assertEqual(pooled.missing_groups, ("C",))

    def test_j0_and_j1_match_hand_calculation(self) -> None:
        response = _response(probability_sum=(0.0, 0.0, 1.0))
        values = {
            "trend_h5": (0.9, 0.05, 0.05),
            "relative_h5": (0.6, 0.3, 0.1),
            "structure_h5": (0.3, 0.4, 0.3),
            "volume_price_h5": (0.2, 0.3, 0.5),
            "context_h5": (0.1, 0.2, 0.7),
        }
        for question_id, probabilities in values.items():
            response["answers"][question_id]["probabilities"] = dict(
                zip(("up", "flat", "down"), probabilities, strict=True)
            )
        parsed = validate_answers(response)

        j0 = pool_probabilities(parsed.answers, "J0_EQUAL_POOL", 5)
        j1 = pool_probabilities(parsed.answers, "J1_WEIGHTED_POOL", 5)

        self.assertEqual(j0.status, "OK")
        self.assertAlmostEqual(j0.probabilities["up"], 0.42)
        self.assertAlmostEqual(j1.probabilities["up"], 0.495)
        self.assertAlmostEqual(j1.probabilities["flat"], 0.23)
        self.assertAlmostEqual(j1.probabilities["down"], 0.275)


if __name__ == "__main__":
    unittest.main()
