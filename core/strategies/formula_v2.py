from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from math import isfinite, sqrt
from typing import Any

import numpy as np
import pandas as pd

from core.factors.technical_v2 import DIRECTIONAL_FACTOR_IDS
from core.technical_v2.contracts import ContractError

FACTOR_GROUPS = {
    "T": ("F01", "F02", "F03"),
    "R": ("F04", "F05"),
    "S": ("F06", "F07", "F08"),
    "V": ("F09", "F10", "F11"),
    "C": ("F12", "F13", "F14", "F15"),
}
BASE_WEIGHTS = {
    1: {"T": 0.20, "R": 0.15, "S": 0.25, "V": 0.25, "C": 0.15},
    3: {"T": 0.25, "R": 0.20, "S": 0.20, "V": 0.20, "C": 0.15},
    5: {"T": 0.30, "R": 0.20, "S": 0.20, "V": 0.15, "C": 0.15},
}
FORMULA_VARIANTS = {
    "F0_BALANCED": {},
    "F1_TREND": {"T": 0.05, "V": -0.05},
    "F2_STRUCTURE": {"S": 0.05, "T": -0.05},
}
FORMULA_CONFIG_IDS = tuple(FORMULA_VARIANTS)
FORMULA_WEIGHTS = {
    config_id: {
        horizon: {
            group: base[group] + FORMULA_VARIANTS[config_id].get(group, 0.0)
            for group in FACTOR_GROUPS
        }
        for horizon, base in BASE_WEIGHTS.items()
    }
    for config_id in FORMULA_CONFIG_IDS
}
SECTOR_WEIGHTS = {
    1: (0.20, 0.10, 0.20, 0.30, 0.20),
    3: (0.25, 0.15, 0.20, 0.25, 0.15),
    5: (0.30, 0.20, 0.20, 0.20, 0.10),
}
SCORE_BIN_EDGES = (0.0, 20.0, 35.0, 50.0, 65.0, 80.0, 100.0)


@dataclass(frozen=True)
class FormulaScore:
    status: str
    horizon: int
    config_id: str
    factor_values: dict[str, float | None]
    group_scores: dict[str, float | None]
    formula_score: float | None
    forecast_class: str
    observed_trend: str
    missing_factor_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    p_raw_up: None = None
    p_raw_flat: None = None
    p_raw_down: None = None
    p_cal_up: None = None
    p_cal_flat: None = None
    p_cal_down: None = None


@dataclass(frozen=True)
class SectorFormulaScore:
    status: str
    horizon: int
    components: dict[str, float | None]
    formula_score: float | None
    forecast_class: str
    observed_trend: str
    sector_intent: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ReturnBinStats:
    lower: float
    upper: float
    include_upper: bool
    records: int
    dates: int
    mean_return: float | None


@dataclass(frozen=True)
class ReturnBinModel:
    entity_type: str
    horizon: int
    global_records: int
    global_dates: int
    global_mean: float | None
    bins: tuple[ReturnBinStats, ...]


@dataclass(frozen=True)
class FormulaReturnEstimate:
    status: str
    expected_gross_return: float | None
    estimated_round_trip_cost: float
    expected_net_edge: float | None
    return_estimate_basis: str
    bin_lower: float | None
    bin_upper: float | None
    bin_records: int
    bin_dates: int


def _number(value: Any, *, bounded: bool = False) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(result) or (bounded and not -1.0 <= result <= 1.0):
        return None
    return result


def _classify_score(score: float | None) -> str:
    if score is None:
        return "unknown"
    if score >= 60.0:
        return "up"
    if score <= 40.0:
        return "down"
    return "flat"


def _observed_trend(values: list[float | None]) -> str:
    if any(value is None for value in values):
        return "UNKNOWN"
    mean = float(np.mean(values))
    if mean >= 0.2:
        return "RISING"
    if mean <= -0.2:
        return "FALLING"
    return "SIDEWAYS"


def score_stock(features: Mapping[str, Any], horizon: int, config_id: str) -> FormulaScore:
    if int(horizon) not in BASE_WEIGHTS:
        raise ContractError(f"unsupported formula horizon: {horizon}")
    if config_id not in FORMULA_WEIGHTS:
        raise ContractError(f"unsupported formula config: {config_id}")
    values = {factor_id: _number(features.get(factor_id), bounded=True) for factor_id in DIRECTIONAL_FACTOR_IDS}
    missing = tuple(factor_id for factor_id, value in values.items() if value is None)
    groups: dict[str, float | None] = {}
    reasons: list[str] = []
    for group, factor_ids in FACTOR_GROUPS.items():
        available = [values[factor_id] for factor_id in factor_ids if values[factor_id] is not None]
        groups[group] = float(np.mean(available)) if available else None
        if not available:
            reasons.append(f"GROUP_{group}_UNAVAILABLE")
    complete = len(missing) <= 3 and all(value is not None for value in groups.values())
    if complete:
        weights = FORMULA_WEIGHTS[config_id][int(horizon)]
        strength = sum(weights[group] * float(groups[group]) for group in FACTOR_GROUPS)
        formula_score = float(np.clip(50.0 + 50.0 * strength, 0.0, 100.0))
        status = "OK"
    else:
        formula_score = None
        status = "INSUFFICIENT_FEATURES"
        reasons.insert(0, "INSUFFICIENT_FEATURES")
    trend = _observed_trend([values[factor_id] for factor_id in FACTOR_GROUPS["T"]])
    return FormulaScore(
        status=status,
        horizon=int(horizon),
        config_id=config_id,
        factor_values=values,
        group_scores=groups,
        formula_score=formula_score,
        forecast_class=_classify_score(formula_score),
        observed_trend=trend,
        missing_factor_ids=missing,
        reason_codes=tuple(reasons),
    )


def _sector_components(context: Mapping[str, Any]) -> dict[str, float | None]:
    direct = {key: _number(context.get(key), bounded=True) for key in ("z1", "z2", "z3", "z4", "z5")}
    if any(value is not None for value in direct.values()):
        return direct
    sigma = _number(context.get("sigma20"))
    log20 = _number(context.get("log_return20"))
    log60 = _number(context.get("log_return60"))
    market20 = _number(context.get("market_log_return20"))
    breadth = _number(context.get("breadth20"))
    amount_surprise = _number(context.get("signed_amount_surprise"), bounded=True)
    scale = max(sigma, 0.005) if sigma is not None else None
    return {
        "z1": float(np.tanh(log20 / (scale * sqrt(20.0)))) if log20 is not None and scale else None,
        "z2": float(np.tanh(log60 / (scale * sqrt(60.0)))) if log60 is not None and scale else None,
        "z3": float(np.tanh((log20 - market20) / (scale * sqrt(20.0))))
        if log20 is not None and market20 is not None and scale
        else None,
        "z4": 2.0 * breadth - 1.0 if breadth is not None and 0.0 <= breadth <= 1.0 else None,
        "z5": amount_surprise,
    }


def score_sector(context: Mapping[str, Any], horizon: int) -> SectorFormulaScore:
    if int(horizon) not in SECTOR_WEIGHTS:
        raise ContractError(f"unsupported sector horizon: {horizon}")
    components = _sector_components(context)
    complete = all(value is not None for value in components.values())
    if complete:
        strength = sum(
            weight * float(components[f"z{index}"])
            for index, weight in enumerate(SECTOR_WEIGHTS[int(horizon)], start=1)
        )
        formula_score = float(np.clip(50.0 + 50.0 * strength, 0.0, 100.0))
        status = "OK"
        reasons: tuple[str, ...] = ()
    else:
        formula_score = None
        status = "CONTEXT_UNAVAILABLE"
        reasons = ("SECTOR_CONTEXT_UNAVAILABLE",)
    forecast = _classify_score(formula_score)
    if int(horizon) != 5:
        intent = "DIAGNOSTIC_ONLY"
    elif forecast == "up":
        intent = "INCREASE_EXPOSURE"
    elif forecast == "flat":
        intent = "MAINTAIN"
    elif forecast == "down":
        intent = "REDUCE_EXPOSURE"
    else:
        intent = "OBSERVE"
    return SectorFormulaScore(
        status=status,
        horizon=int(horizon),
        components=components,
        formula_score=formula_score,
        forecast_class=forecast,
        observed_trend=_observed_trend([components["z1"], components["z2"]]),
        sector_intent=intent,
        reason_codes=reasons,
    )


def fit_return_bins(training_rows: pd.DataFrame, *, entity_type: str, horizon: int) -> ReturnBinModel:
    required = {
        "entity_type",
        "horizon",
        "as_of_trade_date",
        "formula_score",
        "realized_return",
    }
    missing = sorted(required.difference(training_rows.columns))
    if missing:
        raise ContractError(f"return-bin rows missing columns: {', '.join(missing)}")
    frame = training_rows.copy()
    if "split" in frame.columns and not frame["split"].astype(str).eq("train").all():
        raise ContractError("return bins may only fit training rows")
    if not frame["entity_type"].astype(str).eq(str(entity_type)).all():
        raise ContractError("return bins cannot mix entity types")
    if not pd.to_numeric(frame["horizon"], errors="coerce").eq(int(horizon)).all():
        raise ContractError("return bins cannot mix horizons")
    frame["formula_score"] = pd.to_numeric(frame["formula_score"], errors="coerce")
    frame["realized_return"] = pd.to_numeric(frame["realized_return"], errors="coerce")
    frame = frame.loc[
        frame["formula_score"].between(0.0, 100.0)
        & np.isfinite(frame["realized_return"])
    ].copy()
    global_records = len(frame)
    global_dates = int(frame["as_of_trade_date"].astype(str).nunique())
    global_mean = float(frame["realized_return"].mean()) if global_records else None
    bins = []
    for index, (lower, upper) in enumerate(pairwise(SCORE_BIN_EDGES)):
        include_upper = index == len(SCORE_BIN_EDGES) - 2
        mask = frame["formula_score"].ge(lower) & (
            frame["formula_score"].le(upper) if include_upper else frame["formula_score"].lt(upper)
        )
        subset = frame.loc[mask]
        bins.append(
            ReturnBinStats(
                lower=lower,
                upper=upper,
                include_upper=include_upper,
                records=len(subset),
                dates=int(subset["as_of_trade_date"].astype(str).nunique()),
                mean_return=float(subset["realized_return"].mean()) if not subset.empty else None,
            )
        )
    return ReturnBinModel(str(entity_type), int(horizon), global_records, global_dates, global_mean, tuple(bins))


def estimate_formula_return(
    model: ReturnBinModel,
    *,
    score: float,
    estimated_round_trip_cost: float,
) -> FormulaReturnEstimate:
    numeric_score = _number(score)
    cost = _number(estimated_round_trip_cost)
    if numeric_score is None or not 0.0 <= numeric_score <= 100.0:
        raise ContractError("formula score must be finite and within [0, 100]")
    if cost is None or cost < 0:
        raise ContractError("estimated round-trip cost must be finite and non-negative")
    selected = next(
        item
        for item in model.bins
        if numeric_score >= item.lower and (numeric_score <= item.upper if item.include_upper else numeric_score < item.upper)
    )
    supported_global = (
        model.global_mean is not None
        and model.global_records >= 1000
        and model.global_dates >= 30
    )
    if not supported_global:
        gross = None
        status = "INSUFFICIENT_GLOBAL_SUPPORT"
    elif selected.mean_return is not None and selected.records >= 100 and selected.dates >= 20:
        gross = (selected.records * selected.mean_return + 100.0 * model.global_mean) / (selected.records + 100.0)
        status = "OK"
    else:
        gross = model.global_mean
        status = "POOLED_LOW_SUPPORT"
    return FormulaReturnEstimate(
        status=status,
        expected_gross_return=gross,
        estimated_round_trip_cost=cost,
        expected_net_edge=gross - cost if gross is not None else None,
        return_estimate_basis="ADJUSTED_PRICE_PROXY",
        bin_lower=selected.lower,
        bin_upper=selected.upper,
        bin_records=selected.records,
        bin_dates=selected.dates,
    )
