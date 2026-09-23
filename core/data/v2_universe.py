from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import pandas as pd

from core.data.symbols import is_risk_warning_name, normalize_ts_code


@dataclass(frozen=True)
class InstrumentClassification:
    ts_code: str
    exchange: str
    listing_board: str | None
    instrument_type: str
    analysis_eligible: bool
    analysis_reason: str
    trade_eligible: bool
    trade_blockers: tuple[str, ...]


def _compact_date(value: Any) -> str:
    return str(value or "").strip().replace("-", "")


def classify_instrument(row: Mapping[str, Any], as_of: str) -> InstrumentClassification:
    try:
        code = normalize_ts_code(str(row.get("ts_code") or row.get("code") or ""))
    except ValueError:
        return InstrumentClassification("", "UNKNOWN", None, "unknown", False, "INVALID_CODE", False, ("INVALID_CODE",))
    number, suffix = code.split(".")
    exchange = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(suffix, "UNKNOWN")
    asset_type = str(row.get("asset_type") or row.get("instrument_type") or "stock").strip().lower()
    market = str(row.get("market") or "").strip().lower()
    stock_type = asset_type in {"", "stock", "a_share", "equity", "a股"}

    board: str | None = None
    if suffix == "SH" and number.startswith(("600", "601", "603", "605")):
        board = "MAIN_SH"
    elif suffix == "SH" and number.startswith(("688", "689")):
        board = "STAR"
    elif suffix == "SZ" and number.startswith(("000", "001", "002", "003")):
        board = "MAIN_SZ"
    elif suffix == "SZ" and number.startswith(("300", "301")):
        board = "CHINEXT"

    is_b_share = number.startswith("900") or (suffix == "SZ" and number.startswith("200")) or "b share" in market or "b股" in market
    analysis_eligible = stock_type and suffix in {"SH", "SZ"} and board is not None and not is_b_share
    reason = "ELIGIBLE" if analysis_eligible else "OUTSIDE_ANALYSIS_UNIVERSE"

    as_of_date = _compact_date(as_of)
    list_date = _compact_date(row.get("list_date"))
    delist_date = _compact_date(row.get("delist_date"))
    if list_date and list_date > as_of_date:
        analysis_eligible = False
        reason = "NOT_YET_LISTED"
    if delist_date and delist_date < as_of_date:
        analysis_eligible = False
        reason = "DELISTED_BEFORE_AS_OF"

    blockers: list[str] = []
    if not analysis_eligible:
        blockers.append(reason)
    if is_risk_warning_name(str(row.get("name") or "")):
        blockers.append("RISK_WARNING")
    trade_eligible = analysis_eligible and not blockers
    return InstrumentClassification(
        ts_code=code,
        exchange=exchange,
        listing_board=board,
        instrument_type="stock" if stock_type else asset_type or "unknown",
        analysis_eligible=analysis_eligible,
        analysis_reason=reason,
        trade_eligible=trade_eligible,
        trade_blockers=tuple(blockers),
    )


def build_analysis_universe(instruments: pd.DataFrame, as_of: str) -> pd.DataFrame:
    output_columns = [
        "ts_code",
        "name",
        "exchange",
        "listing_board",
        "instrument_type",
        "analysis_status",
        "trade_eligible",
        "trade_blockers",
        "as_of_trade_date",
    ]
    if instruments is None or instruments.empty:
        return pd.DataFrame(columns=output_columns)
    rows: list[dict[str, Any]] = []
    for raw in instruments.to_dict(orient="records"):
        classified = classify_instrument(raw, as_of=as_of)
        if not classified.analysis_eligible:
            continue
        rows.append(
            {
                "ts_code": classified.ts_code,
                "name": str(raw.get("name") or ""),
                "exchange": classified.exchange,
                "listing_board": classified.listing_board,
                "instrument_type": classified.instrument_type,
                "analysis_status": classified.analysis_reason,
                "trade_eligible": classified.trade_eligible,
                "trade_blockers": list(classified.trade_blockers),
                "as_of_trade_date": _compact_date(as_of),
            }
        )
    return pd.DataFrame(rows, columns=output_columns).sort_values("ts_code").drop_duplicates("ts_code").reset_index(drop=True)


def resolve_sector_membership(memberships: pd.DataFrame, as_of: str) -> pd.DataFrame:
    if memberships is None or memberships.empty:
        return pd.DataFrame(columns=list(memberships.columns) if isinstance(memberships, pd.DataFrame) else [])
    target = _compact_date(as_of)
    out = memberships.copy()
    for column in ("valid_from", "valid_to", "observed_at"):
        if column not in out.columns:
            out[column] = None
        out[column] = out[column].map(_compact_date)
    valid_from = out["valid_from"].eq("") | out["valid_from"].le(target)
    valid_to = out["valid_to"].eq("") | out["valid_to"].ge(target)
    current_snapshot = out.get("history_mode", pd.Series("", index=out.index)).astype(str).eq("CURRENT_SNAPSHOT_ONLY")
    observed_after_target = current_snapshot & out["observed_at"].ne("") & out["observed_at"].str.slice(0, 8).gt(target)
    return out.loc[valid_from & valid_to & ~observed_after_target].reset_index(drop=True)
