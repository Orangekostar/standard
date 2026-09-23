from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from math import isfinite
from typing import Any


@dataclass(frozen=True)
class IntentDecision:
    horizon: int
    method: str
    research_intent: str
    account_action: str | None
    action_blockers: tuple[str, ...]
    reason_codes: tuple[str, ...]
    order_quantity: None = None


def _mapping(row: Any) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    if is_dataclass(row):
        return asdict(row)
    raise TypeError("prediction rows must be mappings or dataclass instances")


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _dedupe(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def derive_research_intent(
    rows: Sequence[Mapping[str, Any] | Any],
    holding_state: Mapping[str, Any] | None,
    *,
    horizon: int,
) -> IntentDecision:
    normalized = [_mapping(row) for row in rows]
    method = str(next((row.get("method") for row in normalized if row.get("method")), "unknown")).lower()
    if int(horizon) != 5:
        return IntentDecision(int(horizon), method, "DIAGNOSTIC_ONLY", None, (), ())

    by_horizon = {int(row.get("horizon")): row for row in normalized if str(row.get("horizon", "")).isdigit()}
    primary = by_horizon.get(5)
    current_quantity = int((holding_state or {}).get("current_quantity", 0) or 0)
    held = current_quantity > 0
    reasons: list[str] = []
    blockers: list[str] = []
    if primary is None or str(primary.get("prediction_status", "OK")) != "OK":
        reasons.append("PREDICTION_UNAVAILABLE")
        intent = "HOLD" if held else "WAIT"
    elif method == "formula":
        score5 = _finite(primary.get("formula_score"))
        score3 = _finite(by_horizon.get(3, {}).get("formula_score"))
        overextended = bool(primary.get("overextended", False))
        if held:
            if score5 is None:
                intent = "HOLD"
                reasons.append("FORMULA_SCORE_UNAVAILABLE")
            elif score5 >= 65.0:
                intent = "ADD_WATCH"
            elif score5 >= 45.0:
                intent = "HOLD"
            elif score5 > 40.0:
                intent = "REDUCE_WATCH"
            else:
                intent = "SELL_WATCH"
        elif score5 is None:
            intent = "WAIT"
            reasons.append("FORMULA_SCORE_UNAVAILABLE")
        elif score5 <= 40.0:
            intent = "AVOID"
        elif score5 >= 65.0 and score3 is not None and score3 >= 55.0 and not overextended:
            intent = "BUY_WATCH"
        else:
            intent = "WAIT"
            if score5 >= 65.0 and (score3 is None or score3 < 55.0):
                reasons.append("H3_FILTER_NOT_MET")
            if overextended:
                reasons.append("OVEREXTENDED")
    elif method == "jev":
        calibrated = all(
            _finite(primary.get(f"p_cal_{label}")) is not None
            for label in ("up", "flat", "down")
        )
        prefix = "p_cal" if calibrated else "p_raw"
        p_up = _finite(primary.get(f"{prefix}_up"))
        p_down = _finite(primary.get(f"{prefix}_down"))
        if not calibrated:
            reasons.append("UNVALIDATED_PROBABILITY")
        if p_up is None or p_down is None:
            intent = "HOLD" if held else "WAIT"
            reasons.append("PROBABILITY_UNAVAILABLE")
        elif held:
            if p_down >= 0.55:
                intent = "SELL_WATCH"
            elif p_down >= 0.35:
                intent = "REDUCE_WATCH"
            elif p_up >= 0.55 and p_down <= 0.25:
                intent = "ADD_WATCH"
            else:
                intent = "HOLD"
        elif p_up >= 0.55 and p_down <= 0.25:
            intent = "BUY_WATCH"
        elif p_down >= 0.55:
            intent = "AVOID"
        else:
            intent = "WAIT"
    else:
        intent = "HOLD" if held else "WAIT"
        reasons.append("METHOD_UNSUPPORTED")

    if intent == "BUY_WATCH":
        net_edge = _finite(primary.get("expected_net_edge")) if primary is not None else None
        if net_edge is not None and net_edge <= 0.001:
            intent = "WAIT"
            reasons.append("NET_EDGE_BELOW_MINIMUM")
        elif net_edge is None:
            reasons.append("NET_EDGE_UNAVAILABLE_CONDITIONAL")

    account_action: str | None = None
    if holding_state is not None:
        action_map = {
            "BUY_WATCH": "BUY",
            "ADD_WATCH": "ADD",
            "HOLD": "HOLD",
            "REDUCE_WATCH": "REDUCE",
            "SELL_WATCH": "SELL",
            "WAIT": "WAIT",
            "AVOID": "WAIT",
        }
        account_action = action_map[intent]
        if account_action in {"REDUCE", "SELL"}:
            sellable = int(holding_state.get("sellable_quantity", 0) or 0)
            if sellable <= 0:
                account_action = "BLOCKED"
                blockers.append("NO_SELLABLE_QUANTITY")
        if not bool(holding_state.get("account_available", True)):
            account_action = None
            blockers.append("ACCOUNT_UNAVAILABLE")

    return IntentDecision(
        horizon=5,
        method=method,
        research_intent=intent,
        account_action=account_action,
        action_blockers=_dedupe(blockers),
        reason_codes=_dedupe(reasons),
    )
