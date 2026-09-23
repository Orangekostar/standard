from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.strategies.formula_v2 import FACTOR_GROUPS, FORMULA_WEIGHTS
from core.technical_v2.contracts import ContractError

QUESTION_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "configs" / "jev_questions_v1.json"
QUESTION_SCHEMA_VERSION = "standard.jev-questions.v1"
CLASSES = ("up", "flat", "down")
HORIZONS = (1, 3, 5)
GROUP_TO_SLUG = {
    "T": "trend",
    "R": "relative",
    "S": "structure",
    "V": "volume_price",
    "C": "context",
}
RISK_IDS = tuple(f"Q{index:02d}" for index in range(1, 7))
SECTOR_GROUP_FIELDS = {
    "T": ("z1", "z2"),
    "R": ("z3",),
    "S": ("distance_to_20d_high", "close_range_position_20"),
    "V": ("z5", "amount_ratio20"),
    "C": ("sector_breadth20", "market_breadth20", "market_trend"),
}


@dataclass(frozen=True)
class JevAnswer:
    choice: str
    probabilities: dict[str, float]
    confidence: float


@dataclass(frozen=True)
class ValidatedAnswers:
    status: str
    answers: dict[str, JevAnswer]
    model_returned: str | None
    usage: dict[str, int]
    normalized_question_ids: tuple[str, ...]
    missing_question_ids: tuple[str, ...]
    invalid_question_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class PooledProbabilities:
    status: str
    pool_id: str
    horizon: int
    probabilities: dict[str, float] | None
    missing_groups: tuple[str, ...]
    reason_codes: tuple[str, ...]

    @property
    def p_raw_up(self) -> float | None:
        return None if self.probabilities is None else self.probabilities["up"]

    @property
    def p_raw_flat(self) -> float | None:
        return None if self.probabilities is None else self.probabilities["flat"]

    @property
    def p_raw_down(self) -> float | None:
        return None if self.probabilities is None else self.probabilities["down"]


def _load_question_schema(path: Path = QUESTION_SCHEMA_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load Jev question schema: {exc}") from exc
    if payload.get("schema_version") != QUESTION_SCHEMA_VERSION:
        raise ContractError("unsupported Jev question schema version")
    if tuple(payload.get("classes", ())) != CLASSES:
        raise ContractError("Jev question classes must be exactly up/flat/down")
    return payload


def build_questions(entity_type: str) -> dict[str, dict[str, Any]]:
    if entity_type not in {"stock", "sector"}:
        raise ContractError(f"unsupported Jev entity type: {entity_type}")
    schema = _load_question_schema()
    target = schema[f"{entity_type}_target"]
    noun = "stock" if entity_type == "stock" else "sector"
    questions: dict[str, dict[str, Any]] = {}
    for group in schema["groups"]:
        slug = str(group["slug"])
        label = str(group["label"])
        for horizon in schema["horizons"]:
            question_id = f"{slug}_h{int(horizon)}"
            questions[question_id] = {
                "type": "choice",
                "instructions": (
                    f"For the anonymous {noun} described in state, estimate the {int(horizon)}-session "
                    f"{target}, using only the {label} group as the primary evidence. Entry is the next "
                    f"session open; exit is the open {int(horizon)} sessions after entry. The threshold is "
                    f"already provided in state.targets.h{int(horizon)}.delta. Do not calculate prices, "
                    "dates, or arithmetic and do not infer missing facts. Choose among the three mutually "
                    "exclusive classes defined below."
                ),
                "criteria": dict(schema["criteria"]),
            }
    if len(questions) != 15:
        raise ContractError("Jev question schema must produce exactly 15 questions")
    return questions


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _six_significant(value: float) -> float:
    return float(f"{value:.6g}")


def _bucket(value: float) -> str:
    if value < -0.5:
        return "strong_negative"
    if value < -0.2:
        return "negative"
    if value <= 0.2:
        return "neutral"
    if value <= 0.5:
        return "positive"
    return "strong_positive"


def _value_record(value: Any, *, with_bucket: bool = True) -> dict[str, Any]:
    number = _finite(value)
    if number is None:
        return {"value": None, **({"bucket": "missing"} if with_bucket else {})}
    rounded = _six_significant(number)
    return {"value": rounded, **({"bucket": _bucket(rounded)} if with_bucket else {})}


def _anonymous_entity_id(entity_type: str, local_entity_id: str) -> str:
    digest = hashlib.sha256(
        f"{QUESTION_SCHEMA_VERSION}|{entity_type}|{local_entity_id}".encode()
    ).hexdigest()
    return f"anonymous_{digest[:20]}"


def _probability_distribution(value: Any) -> dict[str, float] | None:
    if not isinstance(value, Mapping) or set(value) != set(CLASSES):
        return None
    numbers = {label: _finite(value[label]) for label in CLASSES}
    if any(number is None or not 0.0 <= number <= 1.0 for number in numbers.values()):
        return None
    total = sum(float(number) for number in numbers.values())
    if abs(total - 1.0) > 0.001 or total <= 0.0:
        return None
    return {label: _six_significant(float(numbers[label]) / total) for label in CLASSES}


def _target_map(targets: Mapping[Any, Any], target_basis: str, missing: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for horizon in HORIZONS:
        raw = targets.get(horizon, targets.get(str(horizon), targets.get(f"h{horizon}")))
        if isinstance(raw, Mapping):
            raw = raw.get("delta")
        delta = _finite(raw)
        if delta is None or delta < 0.0:
            missing.append(f"target_h{horizon}_delta")
            continue
        result[f"h{horizon}"] = {
            "delta": _six_significant(delta),
            "basis": target_basis,
            "entry": "next_session_open",
            "exit": f"open_{horizon}_sessions_after_entry",
        }
    return result


def build_jev_state(
    entity_type: str,
    local_entity_id: str,
    features: Mapping[str, Any],
    *,
    targets: Mapping[Any, Any],
    group_scores: Mapping[str, Any] | None = None,
    priors: Mapping[Any, Any] | None = None,
    missing_fields: Sequence[str] = (),
) -> dict[str, Any]:
    if entity_type not in {"stock", "sector"}:
        raise ContractError(f"unsupported Jev entity type: {entity_type}")
    if not isinstance(features, Mapping) or not isinstance(targets, Mapping):
        raise ContractError("Jev state features and targets must be mappings")
    missing: list[str] = []
    groups: dict[str, dict[str, Any]] = {}
    risks: dict[str, dict[str, Any]] = {}
    if entity_type == "stock":
        for group, factor_ids in FACTOR_GROUPS.items():
            group_payload = {factor_id: _value_record(features.get(factor_id)) for factor_id in factor_ids}
            for factor_id in factor_ids:
                if _finite(features.get(factor_id)) is None:
                    missing.append(factor_id)
            if group_scores is not None:
                group_payload["group_score"] = _value_record(group_scores.get(group))
                if _finite(group_scores.get(group)) is None:
                    missing.append(f"group_score_{group}")
            groups[group] = group_payload
        for risk_id in RISK_IDS:
            risks[risk_id] = _value_record(features.get(risk_id), with_bucket=False)
            if _finite(features.get(risk_id)) is None:
                missing.append(risk_id)
        target_basis = "ADJUSTED_OPEN_TO_OPEN_PRICE_RETURN"
    else:
        for group, field_names in SECTOR_GROUP_FIELDS.items():
            groups[group] = {field: _value_record(features.get(field)) for field in field_names}
            for field in field_names:
                if _finite(features.get(field)) is None:
                    missing.append(field)
        target_basis = "FROZEN_MEMBER_EQUAL_WEIGHT_ADJUSTED_OPEN_RETURN"

    class_priors: dict[str, dict[str, float]] = {}
    for horizon in HORIZONS:
        raw_prior = None if priors is None else priors.get(
            horizon, priors.get(str(horizon), priors.get(f"h{horizon}"))
        )
        if raw_prior is None:
            continue
        distribution = _probability_distribution(raw_prior)
        if distribution is None:
            missing.append(f"prior_h{horizon}")
        else:
            class_priors[f"h{horizon}"] = distribution

    allowed_missing = set(RISK_IDS)
    allowed_missing.update(factor_id for ids in FACTOR_GROUPS.values() for factor_id in ids)
    allowed_missing.update(field for fields in SECTOR_GROUP_FIELDS.values() for field in fields)
    allowed_missing.update(f"group_score_{group}" for group in FACTOR_GROUPS)
    allowed_missing.update(f"target_h{horizon}_delta" for horizon in HORIZONS)
    allowed_missing.update(f"prior_h{horizon}" for horizon in HORIZONS)
    missing.extend(str(field) for field in missing_fields if str(field) in allowed_missing)

    state = {
        "schema_version": QUESTION_SCHEMA_VERSION,
        "entity_type": entity_type,
        "entity_id": _anonymous_entity_id(entity_type, str(local_entity_id)),
        "target_basis": target_basis,
        "group_definitions": dict(GROUP_TO_SLUG),
        "technical_groups": groups,
        "risk_metrics": risks,
        "targets": _target_map(targets, target_basis, missing),
        "class_priors": class_priors,
        "missing_fields": sorted(set(missing)),
    }
    return state


def validate_answers(
    response: Mapping[str, Any],
    expected_question_ids: Sequence[str] | None = None,
) -> ValidatedAnswers:
    expected = tuple(expected_question_ids or build_questions("stock").keys())
    expected_set = set(expected)
    if not isinstance(response, Mapping):
        return ValidatedAnswers(
            "INVALID_RESPONSE", {}, None, {}, (), expected, (), ("RESPONSE_NOT_OBJECT",)
        )
    model = response.get("model")
    model_returned = model if isinstance(model, str) and model else None
    usage_raw = response.get("usage")
    usage: dict[str, int] = {}
    usage_valid = isinstance(usage_raw, Mapping)
    if usage_valid:
        for key in ("input_tokens", "output_tokens"):
            value = usage_raw.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                usage_valid = False
                break
            usage[key] = value
    answers_raw = response.get("answers")
    if not isinstance(answers_raw, Mapping):
        return ValidatedAnswers(
            "INVALID_RESPONSE",
            {},
            model_returned,
            usage,
            (),
            expected,
            (),
            ("ANSWERS_NOT_OBJECT",),
        )

    parsed: dict[str, JevAnswer] = {}
    invalid: list[str] = []
    normalized: list[str] = []
    for question_id in expected:
        answer = answers_raw.get(question_id)
        if answer is None:
            continue
        if not isinstance(answer, Mapping) or answer.get("type") != "choice":
            invalid.append(question_id)
            continue
        choice = answer.get("choice")
        confidence = _finite(answer.get("confidence"))
        probabilities = answer.get("probabilities")
        if (
            choice not in CLASSES
            or confidence is None
            or not 0.0 <= confidence <= 1.0
            or not isinstance(probabilities, Mapping)
            or set(probabilities) != set(CLASSES)
        ):
            invalid.append(question_id)
            continue
        values = {label: _finite(probabilities[label]) for label in CLASSES}
        if any(value is None or not 0.0 <= value <= 1.0 for value in values.values()):
            invalid.append(question_id)
            continue
        total = sum(float(value) for value in values.values())
        if total <= 0.0 or abs(total - 1.0) > 0.001:
            invalid.append(question_id)
            continue
        if total != 1.0:
            normalized.append(question_id)
        parsed[question_id] = JevAnswer(
            choice=str(choice),
            probabilities={label: float(values[label]) / total for label in CLASSES},
            confidence=float(confidence),
        )

    extra = sorted(set(answers_raw) - expected_set)
    invalid.extend(extra)
    missing = tuple(question_id for question_id in expected if question_id not in answers_raw)
    reasons: list[str] = []
    if model_returned is None:
        reasons.append("MODEL_MISSING")
    if not usage_valid:
        reasons.append("USAGE_INVALID")
    if invalid:
        reasons.append("ANSWER_SCHEMA_INVALID")
    if missing:
        reasons.append("ANSWER_MISSING")
    if normalized:
        reasons.append("PROBABILITIES_NORMALIZED")
    if model_returned is None or not usage_valid or invalid:
        status = "INVALID_RESPONSE"
    elif missing:
        status = "PARTIAL_RESPONSE"
    else:
        status = "OK"
    return ValidatedAnswers(
        status=status,
        answers=parsed,
        model_returned=model_returned,
        usage=usage,
        normalized_question_ids=tuple(normalized),
        missing_question_ids=missing,
        invalid_question_ids=tuple(sorted(set(invalid))),
        reason_codes=tuple(reasons),
    )


def pool_probabilities(
    answers: Mapping[str, JevAnswer | Mapping[str, Any]],
    pool_id: str,
    horizon: int,
) -> PooledProbabilities:
    if int(horizon) not in HORIZONS:
        raise ContractError(f"unsupported Jev horizon: {horizon}")
    if pool_id not in {"J0_EQUAL_POOL", "J1_WEIGHTED_POOL"}:
        raise ContractError(f"unsupported Jev pool: {pool_id}")
    distributions: dict[str, Mapping[str, Any]] = {}
    missing_groups: list[str] = []
    for group, slug in GROUP_TO_SLUG.items():
        answer = answers.get(f"{slug}_h{int(horizon)}")
        probabilities = answer.probabilities if isinstance(answer, JevAnswer) else None
        if isinstance(answer, Mapping):
            raw = answer.get("probabilities")
            probabilities = raw if isinstance(raw, Mapping) else None
        distribution = _probability_distribution(probabilities)
        if distribution is None:
            missing_groups.append(group)
        else:
            distributions[group] = distribution
    if missing_groups:
        return PooledProbabilities(
            status="PARTIAL_RESPONSE",
            pool_id=pool_id,
            horizon=int(horizon),
            probabilities=None,
            missing_groups=tuple(missing_groups),
            reason_codes=("MISSING_OR_INVALID_GROUP_RESPONSE",),
        )
    if pool_id == "J0_EQUAL_POOL":
        weights = {group: 0.2 for group in GROUP_TO_SLUG}
    else:
        weights = FORMULA_WEIGHTS["F0_BALANCED"][int(horizon)]
    pooled = {
        label: sum(weights[group] * float(distributions[group][label]) for group in GROUP_TO_SLUG)
        for label in CLASSES
    }
    total = sum(pooled.values())
    normalized = {label: pooled[label] / total for label in CLASSES}
    return PooledProbabilities(
        status="OK",
        pool_id=pool_id,
        horizon=int(horizon),
        probabilities=normalized,
        missing_groups=(),
        reason_codes=(),
    )
