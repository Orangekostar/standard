from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import pairwise

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class TechnicalContext:
    market: pd.DataFrame
    sectors: pd.DataFrame
    assignments: pd.DataFrame


def _compact_date(value: object) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip().replace("-", "")[:8]


def _normalize_panel(panel: pd.DataFrame, as_of: str) -> pd.DataFrame:
    required = {"code", "date", "adjusted_close", "amount_cny"}
    missing = sorted(required.difference(panel.columns))
    if missing:
        raise ValueError(f"context panel missing columns: {', '.join(missing)}")
    out = panel.copy()
    out["code"] = out["code"].astype(str)
    out["date"] = out["date"].map(_compact_date)
    out = out.loc[out["date"] <= _compact_date(as_of)]
    out["adjusted_close"] = pd.to_numeric(out["adjusted_close"], errors="coerce")
    out["amount_cny"] = pd.to_numeric(out["amount_cny"], errors="coerce")
    if "mark_only" not in out.columns:
        out["mark_only"] = False
    out["mark_only"] = out["mark_only"].fillna(False).astype(bool)
    return out.sort_values(["code", "date"]).drop_duplicates(["code", "date"], keep="last")


def _normalize_universe(universe: pd.DataFrame | Iterable[str] | None, panel: pd.DataFrame) -> pd.DataFrame:
    if universe is None:
        return pd.DataFrame({"code": sorted(panel["code"].unique()), "valid_from": "", "valid_to": ""})
    if isinstance(universe, pd.DataFrame):
        out = universe.copy()
        if "code" not in out.columns and "ts_code" in out.columns:
            out = out.rename(columns={"ts_code": "code"})
        if "code" not in out.columns:
            raise ValueError("universe requires code or ts_code")
        for column in ("valid_from", "valid_to"):
            if column not in out.columns:
                out[column] = ""
            out[column] = out[column].map(_compact_date)
        return out[["code", "valid_from", "valid_to"]].drop_duplicates().sort_values("code").reset_index(drop=True)
    return pd.DataFrame({"code": sorted({str(code) for code in universe}), "valid_from": "", "valid_to": ""})


def _expand_universe(universe: pd.DataFrame, dates: list[str]) -> pd.DataFrame:
    rows: list[dict[str, str]] = []
    for record in universe.to_dict(orient="records"):
        start = _compact_date(record.get("valid_from"))
        end = _compact_date(record.get("valid_to"))
        for date in dates:
            if (not start or start <= date) and (not end or end >= date):
                rows.append({"date": date, "code": str(record["code"])})
    return pd.DataFrame(rows, columns=["date", "code"]).sort_values(["date", "code"]).reset_index(drop=True)


def _expand_memberships(memberships: pd.DataFrame, dates: list[str]) -> pd.DataFrame:
    columns = ["date", "namespace", "sector_id", "code", "history_mode"]
    if memberships is None or memberships.empty:
        return pd.DataFrame(columns=columns)
    rows: list[dict[str, str]] = []
    ordered = memberships.copy()
    for column, default in {
        "namespace": "UNKNOWN",
        "valid_from": "",
        "valid_to": "",
        "observed_at": "",
        "history_mode": "UNKNOWN",
    }.items():
        if column not in ordered.columns:
            ordered[column] = default
    ordered = ordered.sort_values(["namespace", "sector_id", "code", "valid_from"])
    for record in ordered.to_dict(orient="records"):
        start = _compact_date(record.get("valid_from"))
        end = _compact_date(record.get("valid_to"))
        mode = str(record.get("history_mode") or "UNKNOWN")
        observed = _compact_date(record.get("observed_at"))
        if mode in {"CURRENT_SNAPSHOT_ONLY", "OBSERVED_PIT"} and observed:
            start = max(start, observed) if start else observed
        for date in dates:
            if (not start or start <= date) and (not end or end >= date):
                rows.append(
                    {
                        "date": date,
                        "namespace": str(record.get("namespace") or "UNKNOWN"),
                        "sector_id": str(record.get("sector_id") or "UNKNOWN"),
                        "code": str(record["code"]),
                        "history_mode": mode,
                    }
                )
    if not rows:
        return pd.DataFrame(columns=columns)
    return (
        pd.DataFrame(rows, columns=columns)
        .drop_duplicates(["date", "namespace", "sector_id", "code"], keep="last")
        .sort_values(["date", "namespace", "sector_id", "code"])
        .reset_index(drop=True)
    )


def _finalize_context(frame: pd.DataFrame, group_columns: list[str]) -> pd.DataFrame:
    if frame.empty:
        return frame
    pieces = []
    for _, group in frame.groupby(group_columns, sort=True, dropna=False):
        out = group.sort_values("date").copy()
        level = pd.to_numeric(out["index_level"], errors="coerce")
        log_return = np.log(level / level.shift(1))
        count_21 = level.rolling(21, min_periods=1).count()
        count_61 = level.rolling(61, min_periods=1).count()
        out["log_return"] = log_return
        out["sigma20"] = log_return.rolling(20, min_periods=20).std(ddof=0)
        out["log_return20"] = np.log(level / level.shift(20)).where(count_21.eq(21))
        out["log_return60"] = np.log(level / level.shift(60)).where(count_61.eq(61))
        high20 = level.rolling(20, min_periods=20).max()
        low20 = level.rolling(20, min_periods=20).min()
        width = high20 - low20
        out["range_position20"] = ((2.0 * level - high20 - low20) / width).where(width.ne(0), 0.0)
        out["high20prev"] = level.shift(1).rolling(20, min_periods=20).max()
        amount = pd.to_numeric(out["amount_cny"], errors="coerce")
        median_prev = amount.shift(1).rolling(20, min_periods=20).median()
        valid_amount = amount.gt(0) & median_prev.gt(0)
        out["signed_amount_surprise"] = (
            np.sign(pd.to_numeric(out["simple_return"], errors="coerce"))
            * np.tanh(np.log(amount / median_prev))
        ).where(valid_amount)
        pieces.append(out)
    return pd.concat(pieces, ignore_index=True).sort_values([*group_columns, "date"]).reset_index(drop=True)


def _build_context_rows(
    grid: pd.DataFrame,
    membership: pd.DataFrame,
    *,
    group_columns: list[str],
    min_coverage: float,
    min_members: int,
) -> pd.DataFrame:
    joined = membership.merge(grid, on=["date", "code"], how="left", validate="many_to_one")
    rows: list[dict[str, object]] = []
    previous_levels: dict[tuple[object, ...], float] = {}
    grouped = joined.groupby([*group_columns, "date"], sort=True, dropna=False)
    for keys, group in grouped:
        key_tuple = keys if isinstance(keys, tuple) else (keys,)
        group_key = key_tuple[:-1]
        date = str(key_tuple[-1])
        member_count = int(group["code"].nunique())
        returns = pd.to_numeric(group["simple_return"], errors="coerce")
        covered_count = int(returns.notna().sum())
        coverage = covered_count / member_count if member_count else 0.0
        breadth_valid = group["above_ma20"].notna()
        breadth_denominator = int(breadth_valid.sum())
        breadth_numerator = int(group.loc[breadth_valid, "above_ma20"].astype(bool).sum())
        breadth = breadth_numerator / breadth_denominator if breadth_denominator else np.nan
        amount = pd.to_numeric(group["amount_cny"], errors="coerce")
        amount_sum = float(amount.sum(min_count=1)) if amount.notna().any() else np.nan
        is_first = group["previous_close"].notna().sum() == 0
        status = "OK"
        reason_code = ""
        if member_count < min_members:
            status = "UNAVAILABLE"
            reason_code = "INSUFFICIENT_SECTOR_MEMBERS" if group_columns else "EMPTY_UNIVERSE"
        elif is_first:
            status = "WARMUP"
            reason_code = "INSUFFICIENT_HISTORY"
        elif coverage < min_coverage:
            status = "UNAVAILABLE"
            reason_code = "INSUFFICIENT_MEMBER_COVERAGE"
        if status == "UNAVAILABLE":
            breadth = np.nan
        simple_return = float(returns.mean()) if status == "OK" else np.nan
        previous_level = previous_levels.get(group_key, 1.0)
        if status == "WARMUP":
            index_level = previous_level
        elif status == "OK":
            index_level = previous_level * (1.0 + simple_return)
            previous_levels[group_key] = index_level
        else:
            index_level = np.nan
        row: dict[str, object] = {
            "date": date,
            "simple_return": simple_return,
            "index_level": index_level,
            "breadth20": breadth,
            "breadth_numerator": breadth_numerator,
            "breadth_denominator": breadth_denominator,
            "amount_cny": amount_sum,
            "member_count": member_count,
            "covered_count": covered_count,
            "coverage": coverage,
            "status": status,
            "reason_code": reason_code,
        }
        for name, value in zip(group_columns, group_key):
            row[name] = value
        rows.append(row)
    return pd.DataFrame(rows)


def _lag_membership_for_returns(membership: pd.DataFrame, dates: list[str]) -> pd.DataFrame:
    if membership.empty or not dates:
        return membership.copy()
    pieces = [membership.loc[membership["date"] == dates[0]].copy()]
    for previous_date, date in pairwise(dates):
        active = membership.loc[membership["date"] == previous_date].copy()
        active["date"] = date
        pieces.append(active)
    return pd.concat(pieces, ignore_index=True).drop_duplicates().reset_index(drop=True)


def _membership_change_flags(membership: pd.DataFrame, group_columns: list[str]) -> pd.DataFrame:
    columns = [*group_columns, "date", "membership_changed"]
    if membership.empty:
        return pd.DataFrame(columns=columns)
    rows: list[dict[str, object]] = []
    group_arg: str | list[str] = group_columns[0] if len(group_columns) == 1 else group_columns
    for keys, group in membership.groupby(group_arg, sort=True, dropna=False):
        key_tuple = keys if isinstance(keys, tuple) else (keys,)
        previous: frozenset[str] | None = None
        for date, dated in group.groupby("date", sort=True):
            members = frozenset(dated["code"].astype(str))
            row = {"date": str(date), "membership_changed": previous is not None and members != previous}
            row.update(dict(zip(group_columns, key_tuple)))
            rows.append(row)
            previous = members
    return pd.DataFrame(rows, columns=columns)


def build_context(
    panel: pd.DataFrame,
    memberships: pd.DataFrame,
    as_of: str,
    *,
    universe: pd.DataFrame | Iterable[str] | None = None,
    sessions: Iterable[str] | None = None,
    min_context_member_coverage: float = 0.9,
    min_sector_members: int = 5,
) -> TechnicalContext:
    normalized = _normalize_panel(panel, as_of)
    cutoff = _compact_date(as_of)
    dates = (
        sorted({_compact_date(date) for date in sessions if _compact_date(date) <= cutoff})
        if sessions is not None
        else sorted(normalized["date"].unique().tolist())
    )
    if not dates:
        empty = pd.DataFrame()
        return TechnicalContext(empty, empty, empty)
    universe_versions = _normalize_universe(universe, normalized)
    market_membership = _expand_universe(universe_versions, dates)
    codes = sorted(market_membership["code"].unique().tolist())
    grid = pd.MultiIndex.from_product([codes, dates], names=["code", "date"]).to_frame(index=False)
    grid = grid.merge(normalized, on=["code", "date"], how="left", validate="one_to_one")
    grid = grid.sort_values(["code", "date"]).reset_index(drop=True)
    grid["previous_close"] = grid.groupby("code", sort=False)["adjusted_close"].shift(1)
    grid["simple_return"] = grid["adjusted_close"] / grid["previous_close"] - 1.0
    ma20 = grid.groupby("code", sort=False)["adjusted_close"].transform(
        lambda values: values.rolling(20, min_periods=20).mean()
    )
    grid["above_ma20"] = grid["adjusted_close"].ge(ma20).where(ma20.notna() & grid["adjusted_close"].notna())

    market = _build_context_rows(
        grid,
        _lag_membership_for_returns(market_membership, dates),
        group_columns=[],
        min_coverage=float(min_context_member_coverage),
        min_members=1,
    )
    market["context_type"] = "MARKET"
    market["context_id"] = "ALL_A_SHARES"
    market = _finalize_context(market, ["context_id"])
    market_changes = _membership_change_flags(market_membership.assign(context_id="ALL_A_SHARES"), ["context_id"])
    market = market.merge(market_changes, on=["context_id", "date"], how="left", validate="one_to_one")
    market["membership_changed"] = market["membership_changed"].fillna(False).astype(bool)

    assignments = _expand_memberships(memberships, dates)
    sector_membership = _lag_membership_for_returns(
        assignments[["date", "namespace", "sector_id", "code"]], dates
    )
    sectors = _build_context_rows(
        grid,
        sector_membership,
        group_columns=["namespace", "sector_id"],
        min_coverage=float(min_context_member_coverage),
        min_members=max(1, int(min_sector_members)),
    ) if not sector_membership.empty else pd.DataFrame()
    if not sectors.empty:
        sectors["context_type"] = "SECTOR"
        sectors["context_id"] = sectors["namespace"].astype(str) + ":" + sectors["sector_id"].astype(str)
        sectors = _finalize_context(sectors, ["namespace", "sector_id"])
        sector_changes = _membership_change_flags(assignments, ["namespace", "sector_id"])
        sectors = sectors.merge(
            sector_changes,
            on=["namespace", "sector_id", "date"],
            how="left",
            validate="one_to_one",
        )
        sectors["membership_changed"] = sectors["membership_changed"].fillna(False).astype(bool)

    return TechnicalContext(
        market=market.sort_values("date").reset_index(drop=True),
        sectors=sectors.sort_values(["namespace", "sector_id", "date"]).reset_index(drop=True) if not sectors.empty else sectors,
        assignments=assignments,
    )
