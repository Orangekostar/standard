from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from core.technical_v2.contracts import ContractError

FIXED_SPLIT_COUNTS = {
    "train": 252,
    "calibration": 63,
    "validation": 63,
    "test": 126,
}
FIXED_MATURE_DATES = sum(FIXED_SPLIT_COUNTS.values())
WARMUP_SESSIONS = 120
TARGET_DEFINITION_VERSION = "adjusted-open-t1-to-t1-plus-h.v1"


@dataclass(frozen=True)
class FixedSplitPlan:
    status: str
    assignments: pd.DataFrame
    warmup_dates: dict[str, tuple[str, ...]]
    block_starts: dict[str, str]
    reason_codes: tuple[str, ...]


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
