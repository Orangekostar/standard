from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from core.technical_v2.contracts import (
    ContractError,
    canonical_json,
    json_safe,
    sha256_json,
)

CLASSES = ("up", "flat", "down")
TEMPERATURE_GRID = (0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0)
FORMULA_ORDER = {"F0_BALANCED": 0, "F1_TREND": 1, "F2_STRUCTURE": 2}
JEV_ORDER = {"J0_EQUAL_POOL": 0, "J1_WEIGHTED_POOL": 1}


@dataclass(frozen=True)
class ProbabilityMetrics:
    log_loss: float
    brier_score: float
    records: int
    dates: int


@dataclass(frozen=True)
class TemperatureFit:
    status: str
    entity_type: str
    horizon: int
    pool_id: str
    temperature: float | None
    records: int
    dates: int
    class_counts: dict[str, int]
    calibration_log_loss: float | None
    calibration_id: str | None
    model_id: str | None
    data_version: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ClassReturnModel:
    status: str
    entity_type: str
    horizon: int
    class_means: dict[str, float | None]
    class_records: dict[str, int]
    class_dates: dict[str, int]
    reason_codes: tuple[str, ...]

    def estimate(
        self,
        probabilities: Mapping[str, float],
        estimated_round_trip_cost: float,
    ) -> tuple[float | None, float | None]:
        if self.status != "OK" or any(self.class_means[label] is None for label in CLASSES):
            return None, None
        values = _validated_probability_array(probabilities)
        expected = sum(values[index] * float(self.class_means[label]) for index, label in enumerate(CLASSES))
        return expected, expected - float(estimated_round_trip_cost)


@dataclass(frozen=True)
class FormulaSelection:
    status: str
    selected_config_id: str
    eligible_config_ids: tuple[str, ...]
    candidate_statuses: dict[str, tuple[str, ...]]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class JevPoolResult:
    pool_id: str
    status: str
    selected_variant: str | None
    identity_metrics: ProbabilityMetrics | None
    calibrated_metrics: ProbabilityMetrics | None
    temperature: float | None
    coverage: float
    model_id: str | None
    data_version: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class JevSelection:
    status: str
    selected_pool_id: str | None
    selected_variant: str | None
    pool_results: tuple[JevPoolResult, ...]
    reason_codes: tuple[str, ...]


def _require_split(rows: pd.DataFrame, expected: str) -> None:
    if "split" not in rows.columns:
        raise ContractError("selection rows require an explicit split")
    values = set(rows["split"].dropna().astype(str))
    if values != {expected}:
        raise ContractError(f"operation accepts only {expected} rows, got {sorted(values)}")


def _validated_probability_array(value: Mapping[str, Any] | Sequence[Any]) -> np.ndarray:
    if isinstance(value, Mapping):
        if set(value) != set(CLASSES):
            raise ContractError("probabilities must contain exactly up/flat/down")
        raw = [value[label] for label in CLASSES]
    else:
        raw = list(value)
        if len(raw) != 3:
            raise ContractError("probabilities must contain three values")
    probabilities = np.asarray(raw, dtype=float)
    if not np.isfinite(probabilities).all() or (probabilities < 0).any() or (probabilities > 1).any():
        raise ContractError("probabilities must be finite and in [0, 1]")
    total = float(probabilities.sum())
    if total <= 0 or abs(total - 1.0) > 0.001:
        raise ContractError("probabilities must sum to one within tolerance")
    return probabilities / total


def _probability_matrix(rows: pd.DataFrame, prefix: str) -> np.ndarray:
    columns = [f"{prefix}_{label}" for label in CLASSES]
    if missing := sorted(set(columns).difference(rows.columns)):
        raise ContractError(f"probability rows missing columns: {', '.join(missing)}")
    matrix = rows[columns].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(matrix).all() or (matrix < 0).any() or (matrix > 1).any():
        raise ContractError("probability matrix contains invalid values")
    totals = matrix.sum(axis=1)
    if (totals <= 0).any() or (np.abs(totals - 1.0) > 0.001).any():
        raise ContractError("probability rows must sum to one within tolerance")
    return matrix / totals[:, None]


def probability_metrics(rows: pd.DataFrame, *, prefix: str = "p_raw") -> ProbabilityMetrics:
    required = {"as_of_trade_date", "target_class"}
    if missing := sorted(required.difference(rows.columns)):
        raise ContractError(f"probability metrics missing columns: {', '.join(missing)}")
    if rows.empty:
        raise ContractError("probability metrics require rows")
    labels = rows["target_class"].astype(str)
    if not labels.isin(CLASSES).all():
        raise ContractError("probability metrics require observed up/flat/down labels")
    matrix = _probability_matrix(rows, prefix)
    label_indices = labels.map({label: index for index, label in enumerate(CLASSES)}).to_numpy(dtype=int)
    true_probabilities = matrix[np.arange(len(rows)), label_indices]
    one_hot = np.eye(3, dtype=float)[label_indices]
    scored = pd.DataFrame(
        {
            "date": rows["as_of_trade_date"].map(str).to_numpy(),
            "log_loss": -np.log(np.clip(true_probabilities, 1e-8, 1.0)),
            "brier": np.square(matrix - one_hot).sum(axis=1),
        }
    )
    date_scores = scored.groupby("date", sort=True)[["log_loss", "brier"]].mean()
    return ProbabilityMetrics(
        log_loss=float(date_scores["log_loss"].mean()),
        brier_score=float(date_scores["brier"].mean()),
        records=len(rows),
        dates=len(date_scores),
    )


def _temperature_matrix(matrix: np.ndarray, temperature: float) -> np.ndarray:
    if not math.isfinite(temperature) or temperature <= 0:
        raise ContractError("temperature must be finite and positive")
    logits = np.log(np.clip(matrix, 1e-8, 1.0)) / float(temperature)
    logits -= logits.max(axis=1, keepdims=True)
    scaled = np.exp(logits)
    return scaled / scaled.sum(axis=1, keepdims=True)


def apply_temperature(rows: pd.DataFrame, temperature: float) -> pd.DataFrame:
    matrix = _temperature_matrix(_probability_matrix(rows, "p_raw"), temperature)
    out = rows.copy()
    for index, label in enumerate(CLASSES):
        out[f"p_cal_{label}"] = matrix[:, index]
    return out


def fit_temperature(
    rows: pd.DataFrame,
    *,
    entity_type: str,
    horizon: int,
    pool_id: str,
    temperature_grid: Sequence[float] = TEMPERATURE_GRID,
    min_records: int = 1000,
    min_dates: int = 30,
    min_records_per_class: int = 30,
) -> TemperatureFit:
    _require_split(rows, "calibration")
    required = {
        "entity_type",
        "horizon",
        "pool_id",
        "target_class",
        "as_of_trade_date",
        "model_id",
        "data_version",
    }
    if missing := sorted(required.difference(rows.columns)):
        raise ContractError(f"temperature rows missing columns: {', '.join(missing)}")
    selected = rows.loc[
        rows["entity_type"].astype(str).eq(entity_type)
        & pd.to_numeric(rows["horizon"], errors="coerce").eq(int(horizon))
        & rows["pool_id"].astype(str).eq(pool_id)
    ].copy()
    model_ids = sorted(set(selected["model_id"].dropna().astype(str)))
    data_versions = sorted(set(selected["data_version"].dropna().astype(str)))
    model_id = model_ids[0] if len(model_ids) == 1 else None
    data_version = data_versions[0] if len(data_versions) == 1 else None
    class_counts = {label: int(selected["target_class"].astype(str).eq(label).sum()) for label in CLASSES}
    dates = int(selected["as_of_trade_date"].astype(str).nunique())
    if selected.empty:
        return TemperatureFit(
            "INSUFFICIENT_CALIBRATION_SUPPORT",
            entity_type,
            int(horizon),
            pool_id,
            None,
            0,
            0,
            class_counts,
            None,
            None,
            None,
            None,
            ("CALIBRATION_SUPPORT_BELOW_MINIMUM",),
        )
    if len(model_ids) != 1 or len(data_versions) != 1:
        return TemperatureFit(
            "VERSION_MISMATCH",
            entity_type,
            int(horizon),
            pool_id,
            None,
            len(selected),
            dates,
            class_counts,
            None,
            None,
            model_id,
            data_version,
            ("MODEL_OR_DATA_VERSION_MISMATCH",),
        )
    if (
        len(selected) < min_records
        or dates < min_dates
        or any(class_counts[label] < min_records_per_class for label in CLASSES)
    ):
        return TemperatureFit(
            "INSUFFICIENT_CALIBRATION_SUPPORT",
            entity_type,
            int(horizon),
            pool_id,
            None,
            len(selected),
            dates,
            class_counts,
            None,
            None,
            model_id,
            data_version,
            ("CALIBRATION_SUPPORT_BELOW_MINIMUM",),
        )
    scores: list[tuple[float, float]] = []
    try:
        for temperature in temperature_grid:
            transformed = apply_temperature(selected, float(temperature))
            metrics = probability_metrics(transformed, prefix="p_cal")
            scores.append((float(temperature), metrics.log_loss))
    except ContractError as exc:
        return TemperatureFit(
            "INVALID_PROBABILITIES",
            entity_type,
            int(horizon),
            pool_id,
            None,
            len(selected),
            dates,
            class_counts,
            None,
            None,
            model_id,
            data_version,
            (str(exc),),
        )
    minimum = min(score for _, score in scores)
    tied = [temperature for temperature, score in scores if math.isclose(score, minimum, abs_tol=1e-12)]
    chosen = min(tied, key=lambda value: (abs(value - 1.0), value))
    calibration_payload = {
        "schema_version": "standard.jev-temperature.v1",
        "entity_type": entity_type,
        "horizon": int(horizon),
        "pool_id": pool_id,
        "temperature": chosen,
        "records": len(selected),
        "dates": dates,
        "class_counts": class_counts,
        "model_id": model_id,
        "data_version": data_version,
    }
    return TemperatureFit(
        "OK",
        entity_type,
        int(horizon),
        pool_id,
        chosen,
        len(selected),
        dates,
        class_counts,
        minimum,
        sha256_json(calibration_payload),
        model_id,
        data_version,
        (),
    )


def fit_class_return_means(
    rows: pd.DataFrame,
    *,
    entity_type: str,
    horizon: int,
    min_records_per_class: int = 100,
    min_dates_per_class: int = 20,
) -> ClassReturnModel:
    _require_split(rows, "train")
    required = {
        "entity_type",
        "horizon",
        "target_class",
        "as_of_trade_date",
        "realized_return",
    }
    if missing := sorted(required.difference(rows.columns)):
        raise ContractError(f"class-return rows missing columns: {', '.join(missing)}")
    selected = rows.loc[
        rows["entity_type"].astype(str).eq(entity_type)
        & pd.to_numeric(rows["horizon"], errors="coerce").eq(int(horizon))
    ].copy()
    selected["realized_return"] = pd.to_numeric(selected["realized_return"], errors="coerce")
    selected = selected.loc[np.isfinite(selected["realized_return"])]
    class_means: dict[str, float | None] = {}
    class_records: dict[str, int] = {}
    class_dates: dict[str, int] = {}
    reasons: list[str] = []
    for label in CLASSES:
        group = selected.loc[selected["target_class"].astype(str).eq(label)]
        class_records[label] = len(group)
        date_means = group.groupby(group["as_of_trade_date"].astype(str))["realized_return"].mean()
        class_dates[label] = len(date_means)
        if len(group) < min_records_per_class or len(date_means) < min_dates_per_class:
            class_means[label] = None
            reasons.append(f"CLASS_{label.upper()}_SUPPORT_BELOW_MINIMUM")
        else:
            class_means[label] = float(date_means.mean())
    return ClassReturnModel(
        "OK" if not reasons else "INSUFFICIENT_CLASS_SUPPORT",
        entity_type,
        int(horizon),
        class_means,
        class_records,
        class_dates,
        tuple(reasons),
    )


def select_formula_candidate(
    candidates: pd.DataFrame,
    *,
    min_closed_trades: int = 30,
    min_trade_dates: int = 20,
    max_drawdown_cap: float = 0.20,
    sharpe_tie_tolerance: float = 0.05,
) -> FormulaSelection:
    _require_split(candidates, "validation")
    required = {
        "config_id",
        "net_sharpe",
        "turnover",
        "closed_trades",
        "trade_dates",
        "max_drawdown",
        "unresolved",
    }
    if missing := sorted(required.difference(candidates.columns)):
        raise ContractError(f"formula selection missing columns: {', '.join(missing)}")
    if candidates["config_id"].duplicated().any():
        raise ContractError("formula selection requires one validation row per config")
    if not set(candidates["config_id"]).issubset(FORMULA_ORDER):
        raise ContractError("formula selection received an unknown config")
    statuses: dict[str, tuple[str, ...]] = {}
    eligible_rows: list[dict[str, Any]] = []
    for row in candidates.to_dict(orient="records"):
        config_id = str(row["config_id"])
        reasons: list[str] = []
        numeric: dict[str, float] = {}
        for column in ("net_sharpe", "turnover", "closed_trades", "trade_dates", "max_drawdown"):
            try:
                numeric[column] = float(row[column])
            except (TypeError, ValueError):
                numeric[column] = math.nan
        if not all(math.isfinite(value) for value in numeric.values()):
            reasons.append("INVALID_METRIC")
        unresolved = row["unresolved"]
        if not isinstance(unresolved, (bool, np.bool_)):
            reasons.append("INVALID_UNRESOLVED_FLAG")
        elif bool(unresolved):
            reasons.append("LEDGER_UNRESOLVED")
        if numeric["closed_trades"] < min_closed_trades:
            reasons.append("INSUFFICIENT_CLOSED_TRADES")
        if numeric["trade_dates"] < min_trade_dates:
            reasons.append("INSUFFICIENT_TRADE_DATES")
        if numeric["max_drawdown"] > max_drawdown_cap:
            reasons.append("MAX_DRAWDOWN_EXCEEDED")
        statuses[config_id] = tuple(reasons)
        if not reasons:
            eligible_rows.append({"config_id": config_id, **numeric})
    if not eligible_rows:
        return FormulaSelection(
            "NO_ELIGIBLE_CANDIDATE",
            "F0_BALANCED",
            (),
            statuses,
            ("F0_UNVALIDATED_REFERENCE",),
        )
    best_sharpe = max(row["net_sharpe"] for row in eligible_rows)
    tied = [row for row in eligible_rows if best_sharpe - row["net_sharpe"] <= sharpe_tie_tolerance]
    chosen = min(tied, key=lambda row: (row["turnover"], FORMULA_ORDER[row["config_id"]]))
    return FormulaSelection(
        "SELECTED",
        chosen["config_id"],
        tuple(row["config_id"] for row in sorted(eligible_rows, key=lambda row: FORMULA_ORDER[row["config_id"]])),
        statuses,
        (),
    )


def select_jev_pool(
    validation_rows: pd.DataFrame,
    fits: Mapping[str, TemperatureFit],
    *,
    logloss_tie_tolerance: float = 0.001,
) -> JevSelection:
    _require_split(validation_rows, "validation")
    required = {
        "pool_id",
        "as_of_trade_date",
        "target_class",
        "entity_type",
        "horizon",
        "response_status",
        "model_id",
        "data_version",
    }
    if missing := sorted(required.difference(validation_rows.columns)):
        raise ContractError(f"Jev selection missing columns: {', '.join(missing)}")
    unknown = set(validation_rows["pool_id"].astype(str)) - set(JEV_ORDER)
    if unknown:
        raise ContractError(f"Jev selection received unknown pools: {sorted(unknown)}")
    entity_types = sorted(set(validation_rows["entity_type"].dropna().astype(str)))
    horizons = sorted(set(pd.to_numeric(validation_rows["horizon"], errors="coerce").dropna().astype(int)))
    if len(entity_types) != 1 or len(horizons) != 1:
        raise ContractError("Jev selection must be isolated to one entity type and horizon")
    entity_type = entity_types[0]
    horizon = horizons[0]
    results: list[JevPoolResult] = []
    for pool_id in sorted(set(validation_rows["pool_id"].astype(str)), key=JEV_ORDER.get):
        pool_all = validation_rows.loc[validation_rows["pool_id"].astype(str).eq(pool_id)].copy()
        pool = pool_all.loc[pool_all["response_status"].astype(str).eq("OK")].copy()
        coverage = len(pool) / len(pool_all) if len(pool_all) else 0.0
        reasons: list[str] = []
        model_id = None
        data_version = None
        for column, reason in (("model_id", "MODEL_VERSION_MISMATCH"), ("data_version", "DATA_VERSION_MISMATCH")):
            values = sorted(set(pool[column].dropna().astype(str)))
            if len(values) != 1:
                reasons.append(reason)
            elif column == "model_id":
                model_id = values[0]
            else:
                data_version = values[0]
        if pool.empty:
            reasons.append("NO_VALID_RESPONSES")
        if reasons:
            results.append(
                JevPoolResult(pool_id, "INELIGIBLE", None, None, None, None, coverage, model_id, data_version, tuple(reasons))
            )
            continue
        try:
            identity = probability_metrics(pool, prefix="p_raw")
        except ContractError as exc:
            results.append(
                JevPoolResult(pool_id, "INELIGIBLE", None, None, None, None, coverage, model_id, data_version, (str(exc),))
            )
            continue
        selected_variant = "identity"
        calibrated_metrics = None
        temperature = None
        fit = fits.get(pool_id)
        if fit is not None and fit.status == "OK" and fit.temperature is not None:
            if (
                fit.pool_id != pool_id
                or fit.entity_type != entity_type
                or fit.horizon != horizon
                or fit.model_id != model_id
                or fit.data_version != data_version
            ):
                raise ContractError("temperature fit contract does not match selection rows")
            calibrated = apply_temperature(pool, fit.temperature)
            calibrated_metrics = probability_metrics(calibrated, prefix="p_cal")
            temperature = fit.temperature
            if calibrated_metrics.log_loss < identity.log_loss - 1e-12:
                selected_variant = "calibrated"
            else:
                reasons.append("CALIBRATION_NO_GAIN")
        else:
            reasons.append("CALIBRATION_UNAVAILABLE")
        results.append(
            JevPoolResult(
                pool_id,
                "OK",
                selected_variant,
                identity,
                calibrated_metrics,
                temperature,
                coverage,
                model_id,
                data_version,
                tuple(reasons),
            )
        )
    eligible = [result for result in results if result.status == "OK"]
    if not eligible:
        return JevSelection("NO_ELIGIBLE_POOL", None, None, tuple(results), ("NO_ELIGIBLE_POOL",))
    if len({(result.model_id, result.data_version) for result in eligible}) != 1:
        return JevSelection(
            "NO_ELIGIBLE_POOL",
            None,
            None,
            tuple(results),
            ("POOL_MODEL_OR_DATA_VERSION_MISMATCH",),
        )

    def chosen_metrics(result: JevPoolResult) -> ProbabilityMetrics:
        if result.selected_variant == "calibrated":
            if result.calibrated_metrics is None:
                raise ContractError("selected calibrated pool has no calibrated metrics")
            return result.calibrated_metrics
        if result.identity_metrics is None:
            raise ContractError("selected identity pool has no identity metrics")
        return result.identity_metrics

    best_log_loss = min(chosen_metrics(result).log_loss for result in eligible)
    log_tied = [
        result
        for result in eligible
        if chosen_metrics(result).log_loss - best_log_loss <= logloss_tie_tolerance
    ]
    best_brier = min(chosen_metrics(result).brier_score for result in log_tied)
    brier_tied = [
        result
        for result in log_tied
        if math.isclose(chosen_metrics(result).brier_score, best_brier, abs_tol=1e-12)
    ]
    chosen = min(brier_tied, key=lambda result: JEV_ORDER[result.pool_id])
    return JevSelection("SELECTED", chosen.pool_id, chosen.selected_variant, tuple(results), ())


def write_selection_artifact(
    path: str | Path,
    formula_selection: FormulaSelection,
    jev_selection: JevSelection,
    *,
    metadata: Mapping[str, Any],
) -> str:
    selection = {
        "schema_version": "standard.technical-v2.selection.v1",
        "formula": asdict(formula_selection),
        "jev": asdict(jev_selection),
        "metadata": dict(metadata),
        "final_test_opened": False,
    }
    digest = sha256_json(selection)
    payload = {
        **selection,
        "selection_sha256": digest,
        "hash_scope": "document excluding selection_sha256 and hash_scope",
    }
    destination = Path(path)
    if destination.exists():
        try:
            existing = json.loads(destination.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ContractError("existing selection artifact is invalid JSON") from exc
        if existing != json_safe(payload):
            raise ContractError("selection artifact is immutable and already contains different content")
        return digest
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return digest
