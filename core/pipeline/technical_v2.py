from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from core.background.snapshot_store import (
    ArtifactMismatch,
    write_immutable_json,
    write_publication_manifest,
    write_run_manifest,
)
from core.technical_v2.contracts import ContractError, json_safe, sha256_json

FIXED_SPLIT_COUNTS = {
    "train": 252,
    "calibration": 63,
    "validation": 63,
    "test": 126,
}
FIXED_MATURE_DATES = sum(FIXED_SPLIT_COUNTS.values())
WARMUP_SESSIONS = 120
TARGET_DEFINITION_VERSION = "adjusted-open-t1-to-t1-plus-h.v1"
PREDICTION_METHODS = ("formula", "jev")
PREDICTION_HORIZONS = (1, 3, 5)


@dataclass(frozen=True)
class FixedSplitPlan:
    status: str
    assignments: pd.DataFrame
    warmup_dates: dict[str, tuple[str, ...]]
    block_starts: dict[str, str]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class PipelineRunResult:
    run_id: str
    publication_id: str
    manifest_hash: str
    manifest_path: Path
    coverage_rows: int
    valid_prediction_rows: int
    route_statuses: dict[str, str]


def _compact_date(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip().replace("-", "")[:8]


def _sessions(calendar: pd.DataFrame | Sequence[Any]) -> list[str]:
    if isinstance(calendar, pd.DataFrame):
        if "date" not in calendar.columns:
            raise ContractError("calendar requires date")
        frame = calendar
        if "is_open" in frame.columns:
            frame = frame.loc[pd.to_numeric(frame["is_open"], errors="coerce").eq(1)]
        values = frame["date"]
    else:
        values = calendar
    return sorted({_compact_date(value) for value in values if _compact_date(value)})


def _direction(realized_return: float, delta: float) -> str:
    if realized_return > delta and not math.isclose(realized_return, delta, abs_tol=1e-12):
        return "up"
    if realized_return < -delta and not math.isclose(realized_return, -delta, abs_tol=1e-12):
        return "down"
    return "flat"


def build_labels(
    prices: pd.DataFrame,
    calendar: pd.DataFrame | Sequence[Any],
    horizons: Iterable[int] = (1, 3, 5),
) -> pd.DataFrame:
    required = {"code", "date", "adjusted_open"}
    missing = sorted(required.difference(prices.columns))
    if missing:
        raise ContractError(f"label prices missing columns: {', '.join(missing)}")
    horizon_values = tuple(int(value) for value in horizons)
    if not horizon_values or any(value not in {1, 3, 5} for value in horizon_values):
        raise ContractError("label horizons must be a non-empty subset of 1, 3, 5")
    sessions = _sessions(calendar)
    session_index = {value: index for index, value in enumerate(sessions)}
    frame = prices.copy()
    frame["code"] = frame["code"].astype(str)
    frame["date"] = frame["date"].map(_compact_date)
    if frame.duplicated(["code", "date"]).any():
        raise ContractError("label prices must be unique by code/date")
    frame["adjusted_open"] = pd.to_numeric(frame["adjusted_open"], errors="coerce")
    if "mark_only" not in frame.columns:
        frame["mark_only"] = False
    frame["mark_only"] = frame["mark_only"].fillna(False).astype(bool)
    volatility_column = "sigma20" if "sigma20" in frame.columns else "Q02" if "Q02" in frame.columns else None
    by_code = {
        code: group.set_index("date", drop=False).sort_index()
        for code, group in frame.groupby("code", sort=True)
    }
    rows: list[dict[str, Any]] = []
    for record in frame.sort_values(["code", "date"]).to_dict(orient="records"):
        code = str(record["code"])
        signal_date = str(record["date"])
        signal_index = session_index.get(signal_date)
        entity_type = str(record.get("entity_type") or "stock")
        delta_floor = 0.002 if entity_type == "sector" else 0.005
        sigma = None
        if volatility_column is not None:
            candidate = pd.to_numeric(pd.Series([record.get(volatility_column)]), errors="coerce").iloc[0]
            if pd.notna(candidate) and np.isfinite(candidate) and float(candidate) >= 0:
                sigma = float(candidate)
        for horizon in horizon_values:
            entry_date = None
            exit_date = None
            label_status = "OK"
            reasons: list[str] = []
            if signal_index is None:
                label_status = "SIGNAL_DATE_NOT_IN_CALENDAR"
                reasons.append(label_status)
            else:
                entry_position = signal_index + 1
                exit_position = signal_index + 1 + horizon
                if entry_position < len(sessions):
                    entry_date = sessions[entry_position]
                if exit_position < len(sessions):
                    exit_date = sessions[exit_position]
                if entry_date is None or exit_date is None:
                    label_status = "LABEL_NOT_MATURED"
                    reasons.append(label_status)
            delta = None if sigma is None else max(delta_floor, 0.25 * sigma * math.sqrt(horizon))
            if delta is None:
                reasons.append("TARGET_THRESHOLD_UNAVAILABLE")
                if label_status == "OK":
                    label_status = "TARGET_THRESHOLD_UNAVAILABLE"
            entry_open = np.nan
            exit_open = np.nan
            realized_return = np.nan
            target_class = "unknown"
            code_prices = by_code[code]
            if label_status in {"OK", "TARGET_THRESHOLD_UNAVAILABLE"}:
                entry = code_prices.loc[entry_date] if entry_date in code_prices.index else None
                exit_row = code_prices.loc[exit_date] if exit_date in code_prices.index else None
                if (
                    entry is None
                    or exit_row is None
                    or bool(entry.get("mark_only", False))
                    or bool(exit_row.get("mark_only", False))
                ):
                    label_status = "UNOBSERVABLE_LABEL"
                    reasons.append(label_status)
                else:
                    entry_open = float(entry["adjusted_open"])
                    exit_open = float(exit_row["adjusted_open"])
                    if (
                        not np.isfinite(entry_open)
                        or not np.isfinite(exit_open)
                        or entry_open <= 0
                        or exit_open <= 0
                    ):
                        label_status = "UNOBSERVABLE_LABEL"
                        reasons.append(label_status)
                    else:
                        realized_return = exit_open / entry_open - 1.0
                        if delta is not None:
                            target_class = _direction(realized_return, delta)
                            label_status = "OK"
            rows.append(
                {
                    "entity_type": entity_type,
                    "entity_id": code,
                    "code": code,
                    "as_of_trade_date": signal_date,
                    "horizon": horizon,
                    "entry_date": entry_date,
                    "exit_date": exit_date,
                    "label_end_date": exit_date,
                    "entry_adjusted_open": entry_open,
                    "exit_adjusted_open": exit_open,
                    "sigma20_at_signal": sigma,
                    "delta": delta,
                    "realized_return": realized_return,
                    "target_class": target_class,
                    "label_status": label_status,
                    "reason_codes": tuple(dict.fromkeys(reasons)),
                    "target_definition_version": TARGET_DEFINITION_VERSION,
                }
            )
    return pd.DataFrame(rows)


def build_sector_labels(
    stock_labels: pd.DataFrame,
    assignments: pd.DataFrame,
    sector_context: pd.DataFrame,
) -> pd.DataFrame:
    label_required = {"code", "as_of_trade_date", "horizon", "realized_return", "label_status"}
    assignment_required = {"date", "namespace", "sector_id", "code"}
    context_required = {"date", "namespace", "sector_id", "sigma20"}
    for frame, required, name in (
        (stock_labels, label_required, "stock labels"),
        (assignments, assignment_required, "sector assignments"),
        (sector_context, context_required, "sector context"),
    ):
        if missing := sorted(required.difference(frame.columns)):
            raise ContractError(f"{name} missing columns: {', '.join(missing)}")
    labels = stock_labels.copy()
    labels["as_of_trade_date"] = labels["as_of_trade_date"].map(_compact_date)
    if labels.duplicated(["code", "as_of_trade_date", "horizon"]).any():
        raise ContractError("stock labels must be unique by code/date/horizon")
    members = assignments.copy()
    members["date"] = members["date"].map(_compact_date)
    members["code"] = members["code"].astype(str)
    members = members.drop_duplicates(["date", "namespace", "sector_id", "code"])
    context = sector_context.copy()
    context["date"] = context["date"].map(_compact_date)
    context = context.drop_duplicates(["date", "namespace", "sector_id"], keep="last")
    context_lookup = context.set_index(["date", "namespace", "sector_id"])
    horizons = sorted(pd.to_numeric(labels["horizon"], errors="coerce").dropna().astype(int).unique())
    rows: list[dict[str, Any]] = []
    grouped = members.groupby(["date", "namespace", "sector_id"], sort=True)
    for (date_value, namespace, sector_id), group in grouped:
        expected_codes = sorted(group["code"].unique())
        for horizon in horizons:
            observed = labels.loc[
                labels["as_of_trade_date"].eq(date_value)
                & labels["horizon"].eq(horizon)
                & labels["code"].astype(str).isin(expected_codes)
            ]
            observed_returns = pd.to_numeric(observed["realized_return"], errors="coerce")
            valid = observed.loc[np.isfinite(observed_returns)]
            observed_codes = set(observed["code"].astype(str))
            valid_codes = set(valid["code"].astype(str))
            label_status = "OK"
            reasons: list[str] = []
            realized_return = np.nan
            target_class = "unknown"
            delta = np.nan
            sigma = np.nan
            context_key = (date_value, namespace, sector_id)
            if context_key in context_lookup.index:
                sigma = pd.to_numeric(
                    pd.Series([context_lookup.loc[context_key]["sigma20"]]), errors="coerce"
                ).iloc[0]
            if observed_codes != set(expected_codes) or valid_codes != set(expected_codes):
                label_status = "INCOMPLETE_MEMBER_COVERAGE"
                reasons.append(label_status)
            elif pd.isna(sigma) or not np.isfinite(float(sigma)) or float(sigma) < 0:
                label_status = "TARGET_THRESHOLD_UNAVAILABLE"
                reasons.append(label_status)
            else:
                delta = max(0.002, 0.25 * float(sigma) * math.sqrt(horizon))
                realized_return = float(pd.to_numeric(valid["realized_return"]).mean())
                target_class = _direction(realized_return, delta)
            entry_dates = sorted(value for value in observed.get("entry_date", pd.Series(dtype=str)).dropna().unique())
            exit_dates = sorted(value for value in observed.get("exit_date", pd.Series(dtype=str)).dropna().unique())
            rows.append(
                {
                    "entity_type": "sector",
                    "entity_id": f"{namespace}:{sector_id}",
                    "namespace": namespace,
                    "sector_id": sector_id,
                    "as_of_trade_date": date_value,
                    "horizon": horizon,
                    "entry_date": entry_dates[0] if len(entry_dates) == 1 else None,
                    "exit_date": exit_dates[0] if len(exit_dates) == 1 else None,
                    "label_end_date": exit_dates[0] if len(exit_dates) == 1 else None,
                    "member_count": len(expected_codes),
                    "observed_member_count": len(valid_codes),
                    "member_coverage": len(valid_codes) / len(expected_codes) if expected_codes else 0.0,
                    "sigma20_at_signal": None if pd.isna(sigma) else float(sigma),
                    "delta": None if pd.isna(delta) else float(delta),
                    "realized_return": realized_return,
                    "target_class": target_class,
                    "label_status": label_status,
                    "reason_codes": tuple(reasons),
                    "target_definition_version": "frozen-member-equal-weight-open-return.v1",
                }
            )
    return pd.DataFrame(rows)


def build_fixed_split(
    dates: Sequence[Any],
    *,
    all_sessions: Sequence[Any] | None = None,
) -> FixedSplitPlan:
    mature = sorted({_compact_date(value) for value in dates if _compact_date(value)})
    empty = pd.DataFrame(columns=["signal_date", "split", "reporting_block", "evaluation_order"])
    if len(mature) < FIXED_MATURE_DATES:
        return FixedSplitPlan(
            "INSUFFICIENT_HISTORY",
            empty,
            {},
            {},
            ("REQUIRES_504_MATURE_SIGNAL_DATES",),
        )
    selected = mature[-FIXED_MATURE_DATES:]
    boundaries = {}
    offset = 0
    rows: list[dict[str, Any]] = []
    for split, count in FIXED_SPLIT_COUNTS.items():
        block = selected[offset : offset + count]
        boundaries[split] = block[0]
        for index, signal_date in enumerate(block):
            reporting_block = None
            if split == "test":
                reporting_block = "test_1" if index < 63 else "test_2"
            rows.append(
                {
                    "signal_date": signal_date,
                    "split": split,
                    "reporting_block": reporting_block,
                    "evaluation_order": offset + index,
                }
            )
        offset += count
    sessions = _sessions(all_sessions if all_sessions is not None else mature)
    session_index = {value: index for index, value in enumerate(sessions)}
    warmups: dict[str, tuple[str, ...]] = {}
    insufficient_warmup: list[str] = []
    for split, start in boundaries.items():
        position = session_index.get(start, 0)
        warmup = tuple(sessions[max(0, position - WARMUP_SESSIONS) : position])
        warmups[split] = warmup
        if len(warmup) < WARMUP_SESSIONS:
            insufficient_warmup.append(split)
    status = "OK" if not insufficient_warmup else "INSUFFICIENT_WARMUP"
    reasons = () if not insufficient_warmup else ("REQUIRES_120_SESSION_WARMUP",)
    return FixedSplitPlan(status, pd.DataFrame(rows), warmups, boundaries, reasons)


def purge_cross_boundary(rows: pd.DataFrame, next_start: Any) -> pd.DataFrame:
    if "label_end_date" not in rows.columns:
        raise ContractError("purge rows require label_end_date")
    boundary = _compact_date(next_start)
    if not boundary:
        raise ContractError("purge boundary is required")
    label_end = rows["label_end_date"].map(_compact_date)
    return rows.loc[label_end.ne("") & label_end.lt(boundary)].copy().reset_index(drop=True)


def _active_rows(frame: pd.DataFrame, as_of: str) -> pd.DataFrame:
    out = frame.copy()
    start_column = "valid_from" if "valid_from" in out.columns else "list_date" if "list_date" in out.columns else None
    end_column = "valid_to" if "valid_to" in out.columns else "delist_date" if "delist_date" in out.columns else None
    starts = out[start_column].map(_compact_date) if start_column else pd.Series("", index=out.index)
    ends = out[end_column].map(_compact_date) if end_column else pd.Series("", index=out.index)
    return out.loc[(starts.eq("") | starts.le(as_of)) & (ends.eq("") | ends.ge(as_of))].copy()


def select_cohort(
    universe: pd.DataFrame,
    memberships: pd.DataFrame | None,
    *,
    entity_type: str,
    as_of: Any,
    seed: int = 20260923,
    max_size: int = 32,
) -> pd.DataFrame:
    cutoff = _compact_date(as_of)
    if entity_type not in {"stock", "sector"}:
        raise ContractError(f"unsupported cohort entity type: {entity_type}")
    if not cutoff or max_size <= 0:
        raise ContractError("cohort as_of and max_size must be valid")

    def stable_hash(identifier: str) -> str:
        return hashlib.sha256(f"seed={int(seed)}|{identifier}".encode()).hexdigest()

    if entity_type == "sector":
        identifier_column = "sector_id" if "sector_id" in universe.columns else "entity_id"
        if identifier_column not in universe.columns:
            raise ContractError("sector cohort universe requires sector_id")
        active = _active_rows(universe, cutoff)
        identifiers = sorted(set(active[identifier_column].astype(str)), key=lambda value: (stable_hash(value), value))
        rows = [
            {
                "entity_type": "sector",
                "sector_id": identifier,
                "entity_id": identifier,
                "cohort_hash": stable_hash(identifier),
                "selection_rank": rank,
                "frozen_as_of": cutoff,
            }
            for rank, identifier in enumerate(identifiers[:max_size], start=1)
        ]
        return pd.DataFrame(rows)

    code_column = "code" if "code" in universe.columns else "ts_code" if "ts_code" in universe.columns else None
    if code_column is None:
        raise ContractError("stock cohort universe requires code or ts_code")
    active_universe = _active_rows(universe, cutoff)
    codes = sorted(set(active_universe[code_column].astype(str)))
    sector_by_code: dict[str, str] = {}
    if memberships is not None and not memberships.empty:
        if not {"code", "sector_id"}.issubset(memberships.columns):
            raise ContractError("stock cohort memberships require code and sector_id")
        active_memberships = _active_rows(memberships, cutoff)
        if "namespace" in active_memberships.columns and active_memberships["namespace"].eq("SW_L1").any():
            active_memberships = active_memberships.loc[active_memberships["namespace"].eq("SW_L1")]
        ordered = active_memberships.assign(code=active_memberships["code"].astype(str)).sort_values(
            ["code", "sector_id"]
        )
        sector_by_code = (
            ordered.drop_duplicates("code", keep="first").set_index("code")["sector_id"].astype(str).to_dict()
        )
    groups: dict[str, list[str]] = {}
    for code in codes:
        groups.setdefault(sector_by_code.get(code, "UNKNOWN"), []).append(code)
    for values in groups.values():
        values.sort(key=lambda value: (stable_hash(value), value))
    sector_order = sorted(sector for sector in groups if sector != "UNKNOWN")
    if "UNKNOWN" in groups:
        sector_order.append("UNKNOWN")
    selected: list[tuple[str, str]] = []
    round_index = 0
    while len(selected) < max_size:
        added = False
        for sector in sector_order:
            values = groups[sector]
            if round_index < len(values):
                selected.append((values[round_index], sector))
                added = True
                if len(selected) == max_size:
                    break
        if not added:
            break
        round_index += 1
    return pd.DataFrame(
        [
            {
                "entity_type": "stock",
                "code": code,
                "entity_id": code,
                "sector_id": sector,
                "cohort_hash": stable_hash(code),
                "selection_rank": rank,
                "frozen_as_of": cutoff,
            }
            for rank, (code, sector) in enumerate(selected, start=1)
        ]
    )


def _prediction_entity_column(universe: pd.DataFrame) -> str:
    for column in ("entity_id", "code", "ts_code"):
        if column in universe.columns:
            return column
    raise ContractError("prediction universe requires entity_id, code, or ts_code")


def build_prediction_contract(
    universe: pd.DataFrame,
    predictions: pd.DataFrame | None,
    *,
    as_of_trade_date: Any,
    entity_type: str = "stock",
    methods: Sequence[str] = PREDICTION_METHODS,
    horizons: Sequence[int] = PREDICTION_HORIZONS,
    missing_status_by_method: dict[str, str] | None = None,
) -> pd.DataFrame:
    as_of = _compact_date(as_of_trade_date)
    if not as_of:
        raise ContractError("prediction contract requires as_of_trade_date")
    method_values = tuple(str(value) for value in methods)
    horizon_values = tuple(int(value) for value in horizons)
    if not method_values or len(set(method_values)) != len(method_values):
        raise ContractError("prediction methods must be unique and non-empty")
    if not horizon_values or len(set(horizon_values)) != len(horizon_values):
        raise ContractError("prediction horizons must be unique and non-empty")
    entity_column = _prediction_entity_column(universe)
    members = universe.copy()
    members["entity_id"] = members[entity_column].astype(str)
    if members["entity_id"].eq("").any() or members["entity_id"].duplicated().any():
        raise ContractError("prediction universe entity IDs must be unique and non-empty")
    reserved_columns = {
        "entity_type",
        "entity_id",
        "as_of_trade_date",
        "horizon",
        "method",
        "prediction_status",
        "reason_codes",
    }
    metadata_columns = [
        column
        for column in members.columns
        if column != entity_column and column not in reserved_columns
    ]
    members = members[[entity_column, *metadata_columns]].rename(columns={entity_column: "entity_id"})
    members = members.loc[:, ~members.columns.duplicated()].sort_values("entity_id").reset_index(drop=True)
    base_rows = []
    for member in members.to_dict(orient="records"):
        for method in method_values:
            for horizon in horizon_values:
                base_rows.append(
                    {
                        **member,
                        "entity_type": str(entity_type),
                        "as_of_trade_date": as_of,
                        "horizon": horizon,
                        "method": method,
                    }
                )
    base = pd.DataFrame(
        base_rows,
        columns=[
            "entity_id",
            *metadata_columns,
            "entity_type",
            "as_of_trade_date",
            "horizon",
            "method",
        ],
    )
    supplied = predictions.copy() if predictions is not None else pd.DataFrame()
    keys = ["entity_type", "entity_id", "as_of_trade_date", "horizon", "method"]
    if not supplied.empty:
        required = {*keys, "prediction_status"}
        if missing := sorted(required.difference(supplied.columns)):
            raise ContractError(f"prediction rows missing columns: {', '.join(missing)}")
        supplied["entity_type"] = supplied["entity_type"].astype(str)
        supplied["entity_id"] = supplied["entity_id"].astype(str)
        supplied["as_of_trade_date"] = supplied["as_of_trade_date"].map(_compact_date)
        supplied["horizon"] = pd.to_numeric(supplied["horizon"], errors="coerce")
        supplied["method"] = supplied["method"].astype(str)
        if not supplied["entity_type"].eq(str(entity_type)).all():
            raise ArtifactMismatch("prediction entity_type does not match publication contract")
        if not supplied["as_of_trade_date"].eq(as_of).all():
            raise ArtifactMismatch("prediction as_of_trade_date does not match publication")
        if not supplied["method"].isin(method_values).all():
            raise ArtifactMismatch("prediction method does not match publication contract")
        if not supplied["horizon"].isin(horizon_values).all():
            raise ArtifactMismatch("prediction horizon does not match publication contract")
        if not supplied["entity_id"].isin(set(members["entity_id"])).all():
            raise ArtifactMismatch("prediction entity is outside the frozen universe")
        if supplied.duplicated(keys).any():
            raise ArtifactMismatch("prediction rows contain duplicate publication keys")
        payload_columns = [
            column
            for column in supplied.columns
            if column not in keys and column not in base.columns
        ]
        base = base.merge(supplied[keys + payload_columns], on=keys, how="left", validate="one_to_one")
    if "prediction_status" not in base.columns:
        base["prediction_status"] = None
    missing_status = {method: "NOT_EVALUATED" for method in method_values}
    missing_status.update({str(key): str(value) for key, value in (missing_status_by_method or {}).items()})
    missing_mask = base["prediction_status"].isna() | base["prediction_status"].astype(str).eq("")
    base.loc[missing_mask, "prediction_status"] = base.loc[missing_mask, "method"].map(missing_status)
    if "reason_codes" not in base.columns:
        base["reason_codes"] = None
    base.loc[missing_mask, "reason_codes"] = base.loc[missing_mask, "prediction_status"].map(
        lambda status: (str(status),)
    )
    method_order = {method: index for index, method in enumerate(method_values)}
    base["_method_order"] = base["method"].map(method_order)
    return (
        base.sort_values(["entity_id", "_method_order", "horizon"])
        .drop(columns="_method_order")
        .reset_index(drop=True)
    )


def _strict_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in frame.to_dict(orient="records"):
        clean: dict[str, Any] = {}
        for key, value in row.items():
            try:
                missing = bool(pd.isna(value))
            except (TypeError, ValueError):
                missing = False
            clean[str(key)] = None if missing else json_safe(value)
        records.append(clean)
    return records


def _route_status(rows: pd.DataFrame, empty_status: str) -> str:
    if rows.empty:
        return empty_status
    statuses = rows["prediction_status"].astype(str)
    if statuses.eq("OK").all():
        return "OK"
    if statuses.eq("OK").any():
        return "PARTIAL"
    unique = sorted(set(statuses))
    return unique[0] if len(unique) == 1 else "UNAVAILABLE"


class TechnicalV2Pipeline:
    def __init__(self, artifact_root: str | Path) -> None:
        self.artifact_root = Path(artifact_root)

    @staticmethod
    def _validate_binding(
        rows: pd.DataFrame | None,
        *,
        method: str,
        as_of_trade_date: str,
    ) -> pd.DataFrame:
        if rows is None:
            return pd.DataFrame()
        frame = rows.copy()
        if frame.empty:
            return frame
        required = {
            "entity_type",
            "entity_id",
            "as_of_trade_date",
            "horizon",
            "method",
            "prediction_status",
        }
        if missing := sorted(required.difference(frame.columns)):
            raise ContractError(f"{method} prediction rows missing columns: {', '.join(missing)}")
        if not frame["method"].astype(str).eq(method).all():
            raise ArtifactMismatch(f"{method} route contains another method")
        if not frame["as_of_trade_date"].map(_compact_date).eq(as_of_trade_date).all():
            raise ArtifactMismatch(f"{method} route belongs to another as-of date")
        keys = ["entity_type", "entity_id", "as_of_trade_date", "horizon", "method"]
        if frame.duplicated(keys).any():
            raise ArtifactMismatch(f"{method} route contains duplicate prediction keys")
        return frame

    def _ensure_run_manifest(self, manifest: dict[str, Any]) -> tuple[Path, str, dict[str, Any]]:
        path = self.artifact_root / str(manifest["run_id"]) / "run_manifest.json"
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ArtifactMismatch(f"cannot read existing run manifest: {path}") from exc
            for key in (
                "run_id",
                "as_of_trade_date",
                "information_cutoff",
                "mode",
                "data_source_mode",
                "data_hash",
                "config_hash",
                "code_hash",
            ):
                if existing.get(key) != manifest.get(key):
                    raise ArtifactMismatch(f"run manifest binding changed: {key}")
            return path, sha256_json(existing), existing
        path, manifest_hash = write_run_manifest(self.artifact_root, manifest)
        return path, manifest_hash, manifest

    def _write_prediction_rows(self, run_id: str, method: str, rows: pd.DataFrame) -> str | None:
        if rows.empty:
            return None
        row_hashes: list[str] = []
        for row in _strict_records(rows):
            key = {
                name: row.get(name)
                for name in ("entity_type", "entity_id", "as_of_trade_date", "horizon", "method")
            }
            filename = f"{sha256_json(key)}.json"
            path = self.artifact_root / run_id / "predictions" / method / filename
            row_hashes.append(write_immutable_json(path, row))
        return sha256_json(sorted(row_hashes))

    def run(
        self,
        *,
        run_id: str,
        as_of_trade_date: Any,
        information_cutoff: str,
        mode: str,
        data_source_mode: str,
        data_hash: str,
        config_hash: str,
        code_hash: str,
        universe: pd.DataFrame,
        sector_universe: pd.DataFrame | None = None,
        formula_predictions: pd.DataFrame,
        jev_predictions: pd.DataFrame | None = None,
    ) -> PipelineRunResult:
        run_id = str(run_id or "").strip()
        as_of = _compact_date(as_of_trade_date)
        hashes = {"data_hash": data_hash, "config_hash": config_hash, "code_hash": code_hash}
        if not run_id or not as_of or not all(str(value or "").strip() for value in hashes.values()):
            raise ContractError("pipeline run requires run_id, as_of, and data/config/code hashes")
        formula = self._validate_binding(
            formula_predictions,
            method="formula",
            as_of_trade_date=as_of,
        )
        jev = self._validate_binding(
            jev_predictions,
            method="jev",
            as_of_trade_date=as_of,
        )
        run_manifest = {
            "schema_version": "technical-v2-run.v1",
            "run_id": run_id,
            "as_of_trade_date": as_of,
            "information_cutoff": str(information_cutoff),
            "mode": str(mode),
            "data_source_mode": str(data_source_mode),
            **hashes,
            "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        }
        _, run_manifest_hash, recorded_run = self._ensure_run_manifest(run_manifest)
        formula_hash = self._write_prediction_rows(run_id, "formula", formula)
        jev_hash = self._write_prediction_rows(run_id, "jev", jev)
        provided_frames = [frame for frame in (formula, jev) if not frame.empty]
        provided = (
            pd.concat(provided_frames, ignore_index=True)
            if provided_frames
            else pd.DataFrame()
        )
        jev_missing_status = "PENDING" if jev.empty else "NOT_EVALUATED"
        coverage_frames = [
            build_prediction_contract(
                universe,
                provided.loc[provided["entity_type"].astype(str).eq("stock")] if not provided.empty else provided,
                as_of_trade_date=as_of,
                entity_type="stock",
                missing_status_by_method={"formula": "NOT_EVALUATED", "jev": jev_missing_status},
            )
        ]
        sectors = sector_universe if sector_universe is not None else pd.DataFrame(columns=["entity_id"])
        sector_supplied = (
            provided.loc[provided["entity_type"].astype(str).eq("sector")]
            if not provided.empty
            else provided
        )
        if not sectors.empty:
            coverage_frames.append(
                build_prediction_contract(
                    sectors,
                    sector_supplied,
                    as_of_trade_date=as_of,
                    entity_type="sector",
                    missing_status_by_method={"formula": "NOT_EVALUATED", "jev": jev_missing_status},
                )
            )
        elif not sector_supplied.empty:
            raise ArtifactMismatch("sector predictions require a frozen sector universe")
        coverage = pd.concat(coverage_frames, ignore_index=True, sort=False)
        coverage = coverage.sort_values(
            ["entity_type", "entity_id", "method", "horizon"]
        ).reset_index(drop=True)
        coverage_records = _strict_records(coverage)
        coverage_hash = sha256_json(coverage_records)
        coverage_path = self.artifact_root / run_id / "coverage" / f"{coverage_hash}.json"
        write_immutable_json(
            coverage_path,
            {
                "schema_version": "technical-v2-coverage.v1",
                "run_id": run_id,
                "as_of_trade_date": as_of,
                "rows": coverage_records,
            },
        )
        formula_coverage = coverage.loc[coverage["method"].eq("formula")]
        jev_coverage = coverage.loc[coverage["method"].eq("jev")]
        routes = {
            "formula": {
                "status": _route_status(formula_coverage, "NOT_EVALUATED"),
                "recorded_rows": len(formula),
                "artifact_hash": formula_hash,
            },
            "jev": {
                "status": _route_status(jev_coverage, "PENDING"),
                "recorded_rows": len(jev),
                "artifact_hash": jev_hash,
            },
        }
        valid_rows = int(coverage["prediction_status"].astype(str).eq("OK").sum())
        publication = write_publication_manifest(
            self.artifact_root,
            {
                "schema_version": "technical-v2-publication.v1",
                "run_id": run_id,
                "as_of_trade_date": as_of,
                "information_cutoff": recorded_run["information_cutoff"],
                "mode": recorded_run["mode"],
                "data_source_mode": recorded_run["data_source_mode"],
                **hashes,
                "run_manifest_hash": run_manifest_hash,
                "coverage_hash": coverage_hash,
                "coverage_rows": len(coverage),
                "valid_prediction_rows": valid_rows,
                "routes": routes,
            },
        )
        publication_id = str(publication["publication_id"])
        return PipelineRunResult(
            run_id=run_id,
            publication_id=publication_id,
            manifest_hash=str(publication["manifest_hash"]),
            manifest_path=self.artifact_root / "publications" / f"{publication_id}.json",
            coverage_rows=int(publication["coverage_rows"]),
            valid_prediction_rows=int(publication["valid_prediction_rows"]),
            route_statuses={
                method: str(values["status"])
                for method, values in publication["routes"].items()
            },
        )
