from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import numpy as np
import pandas as pd

from core.data.symbols import classify_listing_board, normalize_ts_code


@dataclass(frozen=True)
class ProviderResult:
    status: str
    frame: pd.DataFrame
    data_source_mode: str
    code: str = ""
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def normalize_daily_frame(
    frame: pd.DataFrame,
    *,
    source: str = "TUSHARE_DAILY",
    source_version: str = "tushare-daily-v1",
    retrieved_at: str | None = None,
    data_source_mode: str = "real",
) -> pd.DataFrame:
    columns = [
        "code",
        "date",
        "source_version",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "pct_chg",
        "volume_raw",
        "volume_raw_unit",
        "amount_raw",
        "amount_raw_unit",
        "volume_shares",
        "amount_cny",
        "source",
        "retrieved_at",
        "completeness",
        "data_source_mode",
    ]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    out = frame.copy()
    if "ts_code" not in out.columns or "trade_date" not in out.columns:
        raise ValueError("daily frame requires ts_code and trade_date")
    out["code"] = out["ts_code"].astype(str).map(normalize_ts_code)
    out["date"] = out["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True).str.replace("-", "", regex=False)
    for name in ("open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"):
        if name not in out.columns:
            out[name] = np.nan
        out[name] = pd.to_numeric(out[name], errors="coerce")
    out["volume_raw"] = out["vol"]
    out["volume_raw_unit"] = "lot"
    out["amount_raw"] = out["amount"]
    out["amount_raw_unit"] = "thousand_cny"
    out["volume_shares"] = out["volume_raw"] * 100.0
    out["amount_cny"] = out["amount_raw"] * 1000.0
    out["source"] = str(source)
    out["source_version"] = str(source_version)
    out["retrieved_at"] = str(retrieved_at or pd.Timestamp.now(tz="UTC").isoformat())
    out["completeness"] = "COMPLETE"
    out["data_source_mode"] = str(data_source_mode)
    out = out.sort_values(["code", "date"]).drop_duplicates(["code", "date", "source_version"], keep="last")
    return out[columns].reset_index(drop=True)


class RealV2Provider:
    data_source_mode = "real"

    def __init__(self, token: str, transport: Any | None = None) -> None:
        self.token = str(token or "").strip()
        self._transport = transport

    def _api(self) -> Any | None:
        if not self.token:
            return None
        if self._transport is not None:
            return self._transport
        try:
            import tushare as ts

            self._transport = ts.pro_api(self.token)
        except Exception:  # noqa: BLE001 - provider clients expose heterogeneous exceptions
            return None
        return self._transport

    def _unavailable(self) -> ProviderResult:
        return ProviderResult(
            status="UNAVAILABLE_CREDENTIALS",
            frame=pd.DataFrame(),
            data_source_mode=self.data_source_mode,
            code="TUSHARE_TOKEN_MISSING",
            message="TUSHARE_TOKEN is not configured",
        )

    def daily(self, trade_date: str) -> ProviderResult:
        api = self._api()
        if api is None:
            return self._unavailable()
        try:
            raw = api.daily(trade_date=str(trade_date).replace("-", ""))
        except Exception as exc:  # noqa: BLE001 - normalize provider failures at the boundary
            return ProviderResult("ERROR", pd.DataFrame(), "real", "PROVIDER_ERROR", str(exc))
        if raw is None or raw.empty:
            return ProviderResult("NO_DATA", pd.DataFrame(), "real", "EMPTY_RESPONSE", "daily returned no rows")
        normalized = normalize_daily_frame(raw)
        if len(raw) >= 6000:
            return ProviderResult(
                "PARTIAL",
                normalized,
                "real",
                "TRUNCATION_SUSPECTED",
                "daily response reached the documented 6000-row limit",
                {"rows": len(raw), "limit": 6000},
            )
        return ProviderResult("OK", normalized, "real", details={"rows": len(raw)})

    def instruments(self, as_of: str | None = None) -> ProviderResult:
        api = self._api()
        if api is None:
            return self._unavailable()
        try:
            frame = api.stock_basic(
                exchange="",
                list_status="L",
                fields="ts_code,symbol,name,area,industry,market,list_date,delist_date,list_status",
            )
        except Exception as exc:  # noqa: BLE001 - normalize provider failures at the boundary
            return ProviderResult("ERROR", pd.DataFrame(), "real", "PROVIDER_ERROR", str(exc))
        raw = frame.copy() if isinstance(frame, pd.DataFrame) else pd.DataFrame()
        if raw.empty:
            return ProviderResult("NO_DATA", pd.DataFrame(), "real", "EMPTY_RESPONSE")
        observed_at = pd.Timestamp.now(tz="UTC").isoformat()
        valid_from = str(as_of or pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y%m%d")).replace("-", "")
        codes = raw["ts_code"].astype(str).map(normalize_ts_code)
        frame = pd.DataFrame(
            {
                "code": codes,
                "instrument_type": "stock",
                "exchange": codes.str.rsplit(".", n=1).str[-1].map({"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}),
                "listing_board": codes.map(classify_listing_board),
                "name": _optional_series(raw, "name"),
                "industry": _optional_series(raw, "industry"),
                "list_date": _optional_series(raw, "list_date"),
                "delist_date": _optional_series(raw, "delist_date"),
                "listing_status": _optional_series(raw, "list_status", "L"),
                "valid_from": valid_from,
                "valid_to": None,
                "source": "TUSHARE_STOCK_BASIC",
                "source_version": "tushare-stock-basic-v1",
                "observed_at": observed_at,
            }
        )
        frame = frame.drop_duplicates(["code", "valid_from", "source_version"], keep="last").reset_index(drop=True)
        return ProviderResult("OK", frame, "real", details={"rows": len(frame)})

    def calendar(self, exchange: str, start_date: str, end_date: str) -> ProviderResult:
        api = self._api()
        if api is None:
            return self._unavailable()
        try:
            raw = api.trade_cal(
                exchange=str(exchange),
                start_date=str(start_date).replace("-", ""),
                end_date=str(end_date).replace("-", ""),
            )
        except Exception as exc:  # noqa: BLE001 - normalize provider failures at the boundary
            return ProviderResult("ERROR", pd.DataFrame(), "real", "PROVIDER_ERROR", str(exc))
        if raw is None or raw.empty:
            return ProviderResult("NO_DATA", pd.DataFrame(), "real", "EMPTY_RESPONSE", "trade_cal returned no rows")
        dates = raw["cal_date"].astype(str)
        open_dates = sorted(dates.loc[pd.to_numeric(raw["is_open"], errors="coerce").fillna(0).eq(1)].unique())
        next_sessions = []
        for date in dates:
            next_sessions.append(next((candidate for candidate in open_dates if candidate > date), None))
        out = pd.DataFrame(
            {
                "exchange": raw.get("exchange", str(exchange)),
                "date": dates,
                "is_open": pd.to_numeric(raw["is_open"], errors="coerce").fillna(0).astype(int),
                "previous_session": raw.get("pretrade_date"),
                "next_session": next_sessions,
                "source": "TUSHARE_TRADE_CAL",
                "source_version": "tushare-trade-cal-v1",
                "retrieved_at": pd.Timestamp.now(tz="UTC").isoformat(),
            }
        )
        return ProviderResult("OK", out, "real")

    def adjustments(self, trade_date: str) -> ProviderResult:
        result = self._simple_date_endpoint("adj_factor", trade_date, "TUSHARE_ADJ_FACTOR")
        if result.frame.empty:
            return result
        raw = result.frame
        frame = pd.DataFrame(
            {
                "code": raw["ts_code"].astype(str).map(normalize_ts_code),
                "date": raw["trade_date"].astype(str),
                "source_version": "tushare-adj-factor-v1",
                "adj_factor": pd.to_numeric(raw.get("adj_factor"), errors="coerce"),
                "source": "TUSHARE_ADJ_FACTOR",
                "retrieved_at": raw["retrieved_at"],
            }
        )
        return ProviderResult(result.status, frame, "real", result.code, result.message, result.details)

    def limits(self, trade_date: str) -> ProviderResult:
        result = self._simple_date_endpoint("stk_limit", trade_date, "TUSHARE_STK_LIMIT")
        raw = result.frame
        if not raw.empty:
            frame = pd.DataFrame(
                {
                    "code": raw["ts_code"].astype(str).map(normalize_ts_code),
                    "date": raw["trade_date"].astype(str),
                    "source_version": "tushare-stk-limit-v1",
                    "is_suspended": None,
                    "is_risk_warning": None,
                    "up_limit": pd.to_numeric(raw.get("up_limit"), errors="coerce"),
                    "down_limit": pd.to_numeric(raw.get("down_limit"), errors="coerce"),
                    "no_price_limit": None,
                    "rule_version": "TUSHARE_STK_LIMIT",
                    "source": "TUSHARE_STK_LIMIT",
                    "retrieved_at": raw["retrieved_at"],
                }
            )
            result = ProviderResult(result.status, frame, "real", result.code, result.message, result.details)
        if len(raw) >= 5800:
            return ProviderResult(
                "PARTIAL",
                result.frame,
                "real",
                "TRUNCATION_SUSPECTED",
                "stk_limit response reached the documented 5800-row limit",
                {"rows": len(result.frame), "limit": 5800},
            )
        return result

    def sector_memberships(self, l1_codes: list[str]) -> ProviderResult:
        api = self._api()
        if api is None:
            return self._unavailable()
        observed_at = pd.Timestamp.now(tz="UTC").isoformat()
        frames: list[pd.DataFrame] = []
        failures: list[dict[str, str]] = []
        limit_hits: list[dict[str, str]] = []
        for l1_code in sorted({str(value).strip() for value in l1_codes if str(value).strip()}):
            for is_new in ("Y", "N"):
                try:
                    raw = api.index_member_all(l1_code=l1_code, is_new=is_new)
                except Exception as exc:  # noqa: BLE001 - continue independent provider partitions
                    failures.append({"l1_code": l1_code, "is_new": is_new, "error": str(exc)})
                    continue
                if raw is None or raw.empty:
                    continue
                if len(raw) >= 2000:
                    limit_hits.append({"l1_code": l1_code, "is_new": is_new})
                work = raw.copy()
                work["namespace"] = "SW_L1"
                work["sector_id"] = _optional_series(work, "l1_code", l1_code).fillna(l1_code).astype(str)
                work["code"] = work["ts_code"].astype(str).map(normalize_ts_code)
                work["valid_from"] = _optional_series(work, "in_date", "").fillna("").astype(str)
                work["valid_to"] = _optional_series(work, "out_date")
                work["observed_at"] = observed_at
                work["history_mode"] = "RECONSTRUCTED_PIT"
                work["source"] = "TUSHARE_INDEX_MEMBER_ALL"
                work["source_version"] = "tushare-index-member-all-v1"
                frames.append(work)
        if not frames:
            code = "PROVIDER_ERROR" if failures else "EMPTY_RESPONSE"
            status = "ERROR" if failures else "NO_DATA"
            return ProviderResult(status, pd.DataFrame(), "real", code, "index_member_all returned no usable rows", {"failures": failures})
        columns = [
            "namespace",
            "sector_id",
            "code",
            "valid_from",
            "valid_to",
            "observed_at",
            "history_mode",
            "source",
            "source_version",
        ]
        combined = pd.concat(frames, ignore_index=True, sort=False)[columns]
        combined = combined.drop_duplicates(["namespace", "sector_id", "code", "valid_from", "source_version"], keep="last")
        partial = bool(failures or limit_hits)
        return ProviderResult(
            "PARTIAL" if partial else "OK",
            combined.reset_index(drop=True),
            "real",
            "TRUNCATION_OR_FETCH_FAILURE" if partial else "",
            "one or more membership partitions were incomplete" if partial else "",
            {"failures": failures, "limit_hits": limit_hits},
        )

    def sector_classifications(self) -> ProviderResult:
        api = self._api()
        if api is None:
            return self._unavailable()
        try:
            raw = api.index_classify(level="L1", src="SW2021")
        except Exception as exc:  # noqa: BLE001 - normalize provider failures at the boundary
            return ProviderResult("ERROR", pd.DataFrame(), "real", "PROVIDER_ERROR", str(exc))
        if raw is None or raw.empty:
            return ProviderResult(
                "NO_DATA",
                pd.DataFrame(),
                "real",
                "EMPTY_RESPONSE",
                "index_classify returned no SW2021 L1 rows",
            )
        if "index_code" not in raw.columns:
            return ProviderResult(
                "ERROR",
                pd.DataFrame(),
                "real",
                "SCHEMA_MISMATCH",
                "index_classify response lacks index_code",
            )
        frame = pd.DataFrame(
            {
                "namespace": "SW_L1",
                "sector_id": raw["index_code"].astype(str),
                "sector_name": _optional_series(raw, "industry_name", "").fillna("").astype(str),
                "level": _optional_series(raw, "level", "L1").fillna("L1").astype(str),
                "source": "TUSHARE_INDEX_CLASSIFY",
                "source_version": "tushare-index-classify-sw2021-v1",
                "retrieved_at": pd.Timestamp.now(tz="UTC").isoformat(),
            }
        ).drop_duplicates("sector_id", keep="last")
        status = "PARTIAL" if len(raw) >= 1000 else "OK"
        return ProviderResult(
            status,
            frame.reset_index(drop=True),
            "real",
            "TRUNCATION_SUSPECTED" if status == "PARTIAL" else "",
            details={"rows": len(frame), "level": "L1", "src": "SW2021"},
        )

    def suspensions(self, trade_date: str) -> ProviderResult:
        api = self._api()
        if api is None:
            return self._unavailable()
        compact_date = str(trade_date).replace("-", "")
        retrieved_at = pd.Timestamp.now(tz="UTC").isoformat()
        frames: list[pd.DataFrame] = []
        failures: list[dict[str, str]] = []
        limit_hits: list[str] = []
        for suspend_type in ("S", "R"):
            try:
                raw = api.suspend_d(trade_date=compact_date, suspend_type=suspend_type)
            except Exception as exc:  # noqa: BLE001 - continue independent provider partitions
                failures.append({"suspend_type": suspend_type, "error": str(exc)})
                continue
            if raw is None or raw.empty:
                continue
            if len(raw) >= 5000:
                limit_hits.append(suspend_type)
            work = raw.copy()
            work["code"] = work["ts_code"].astype(str).map(normalize_ts_code)
            work["date"] = work["trade_date"].astype(str)
            work["source_version"] = "tushare-suspend-d-v1"
            work["is_suspended"] = _optional_series(work, "suspend_type", suspend_type).astype(str).eq("S")
            work["is_risk_warning"] = None
            work["up_limit"] = None
            work["down_limit"] = None
            work["no_price_limit"] = None
            work["rule_version"] = "TUSHARE_SUSPEND_D"
            work["source"] = "TUSHARE_SUSPEND_D"
            work["retrieved_at"] = retrieved_at
            frames.append(work)
        if not frames:
            status = "ERROR" if failures else "NO_DATA"
            return ProviderResult(status, pd.DataFrame(), "real", "PROVIDER_ERROR" if failures else "EMPTY_RESPONSE", details={"failures": failures})
        columns = [
            "code",
            "date",
            "source_version",
            "is_suspended",
            "is_risk_warning",
            "up_limit",
            "down_limit",
            "no_price_limit",
            "rule_version",
            "source",
            "retrieved_at",
        ]
        combined = pd.concat(frames, ignore_index=True, sort=False)[columns]
        combined = combined.sort_values(["code", "date", "is_suspended"]).drop_duplicates(["code", "date", "source_version"], keep="last")
        partial = bool(failures or limit_hits)
        return ProviderResult(
            "PARTIAL" if partial else "OK",
            combined.reset_index(drop=True),
            "real",
            "TRUNCATION_OR_FETCH_FAILURE" if partial else "",
            details={"failures": failures, "limit_hits": limit_hits},
        )

    def corporate_actions(self, ts_codes: list[str]) -> ProviderResult:
        api = self._api()
        if api is None:
            return self._unavailable()
        retrieved_at = pd.Timestamp.now(tz="UTC").isoformat()
        rows: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        limit_hits: list[str] = []
        for code in sorted({normalize_ts_code(value) for value in ts_codes}):
            try:
                raw = api.dividend(ts_code=code)
            except Exception as exc:  # noqa: BLE001 - continue independent provider partitions
                failures.append({"ts_code": code, "error": str(exc)})
                continue
            if raw is None or raw.empty:
                continue
            if len(raw) >= 2000:
                limit_hits.append(code)
            for record in raw.to_dict(orient="records"):
                record_code = normalize_ts_code(str(record.get("ts_code") or code))
                identity = "|".join(
                    str(record.get(key) or "")
                    for key in ("ann_date", "record_date", "ex_date", "pay_date", "div_listdate", "div_proc")
                )
                event_id = hashlib.sha256(f"{record_code}|{identity}".encode()).hexdigest()
                share_ratio = _decimal_text(record.get("stk_div"))
                cash_per_share = _decimal_text(record.get("cash_div_tax"))
                event_type = "DIVIDEND_AND_SHARE" if _positive_decimal(share_ratio) and _positive_decimal(cash_per_share) else ("SHARE_ACTION" if _positive_decimal(share_ratio) else "CASH_DIVIDEND")
                rows.append(
                    {
                        "event_id": event_id,
                        "code": record_code,
                        "event_type": event_type,
                        "record_date": _nullable_text(record.get("record_date")),
                        "ex_date": _nullable_text(record.get("ex_date")),
                        "pay_date": _nullable_text(record.get("pay_date")),
                        "list_date": _nullable_text(record.get("div_listdate")),
                        "cash_per_share": cash_per_share,
                        "share_ratio": share_ratio,
                        "split_ratio": None,
                        "status": str(record.get("div_proc") or "UNKNOWN"),
                        "source": "TUSHARE_DIVIDEND",
                        "source_version": "tushare-dividend-v1",
                        "retrieved_at": retrieved_at,
                    }
                )
        columns = [
            "event_id",
            "code",
            "event_type",
            "record_date",
            "ex_date",
            "pay_date",
            "list_date",
            "cash_per_share",
            "share_ratio",
            "split_ratio",
            "status",
            "source",
            "source_version",
            "retrieved_at",
        ]
        if not rows:
            status = "ERROR" if failures else "NO_DATA"
            return ProviderResult(status, pd.DataFrame(columns=columns), "real", "PROVIDER_ERROR" if failures else "EMPTY_RESPONSE", details={"failures": failures})
        frame = pd.DataFrame(rows, columns=columns).drop_duplicates(["event_id", "source_version"], keep="last")
        partial = bool(failures or limit_hits)
        return ProviderResult(
            "PARTIAL" if partial else "OK",
            frame.reset_index(drop=True),
            "real",
            "TRUNCATION_OR_FETCH_FAILURE" if partial else "",
            details={"failures": failures, "limit_hits": limit_hits},
        )

    def historical_instruments(self, trade_date: str) -> ProviderResult:
        api = self._api()
        if api is None:
            return self._unavailable()
        compact_date = str(trade_date).replace("-", "")
        fields = "trade_date,ts_code,name,industry,list_date"
        try:
            raw = api.bak_basic(trade_date=compact_date, fields=fields)
        except Exception as exc:  # noqa: BLE001 - normalize provider failures at the boundary
            return ProviderResult("ERROR", pd.DataFrame(), "real", "PROVIDER_ERROR", str(exc))
        if raw is None or raw.empty:
            return ProviderResult("NO_DATA", pd.DataFrame(), "real", "EMPTY_RESPONSE")
        frame = pd.DataFrame(
            {
                "as_of_trade_date": raw["trade_date"].astype(str),
                "code": raw["ts_code"].astype(str).map(normalize_ts_code),
                "name": raw.get("name", ""),
                "industry_snapshot": raw.get("industry", ""),
                "list_date": raw.get("list_date", ""),
                "source": "TUSHARE_BAK_BASIC",
                "source_version": "tushare-bak-basic-v1",
                "observed_at": pd.Timestamp.now(tz="UTC").isoformat(),
            }
        )
        if len(raw) >= 7000:
            return ProviderResult("PARTIAL", frame, "real", "TRUNCATION_SUSPECTED", details={"limit": 7000})
        return ProviderResult("OK", frame, "real")

    def _simple_date_endpoint(self, method: str, trade_date: str, source: str) -> ProviderResult:
        api = self._api()
        if api is None:
            return self._unavailable()
        try:
            frame = getattr(api, method)(trade_date=str(trade_date).replace("-", ""))
        except Exception as exc:  # noqa: BLE001 - normalize provider failures at the boundary
            return ProviderResult("ERROR", pd.DataFrame(), "real", "PROVIDER_ERROR", str(exc))
        out = frame.copy() if isinstance(frame, pd.DataFrame) else pd.DataFrame()
        if not out.empty:
            out["source"] = source
            out["source_version"] = f"{source.lower()}-v1"
            out["retrieved_at"] = pd.Timestamp.now(tz="UTC").isoformat()
        return ProviderResult("OK" if not out.empty else "NO_DATA", out, "real")


class DemoV2Provider:
    data_source_mode = "demo"

    def __init__(self, seed: int = 20260923) -> None:
        self.seed = int(seed)

    def daily(self, trade_date: str) -> ProviderResult:
        compact_date = str(trade_date).replace("-", "")
        digest = hashlib.sha256(f"{self.seed}|{compact_date}".encode()).digest()
        rng = np.random.default_rng(int.from_bytes(digest[:8], "big"))
        rows: list[dict[str, Any]] = []
        for index, code in enumerate(
            ("000001.SZ", "000002.SZ", "300001.SZ", "600000.SH", "601398.SH", "688001.SH")
        ):
            close = 10.0 + index * 2.0 + float(rng.normal(0.0, 0.2))
            rows.append(
                {
                    "ts_code": code,
                    "trade_date": compact_date,
                    "open": close * 0.995,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "pre_close": close / 1.002,
                    "pct_chg": 0.2,
                    "vol": 100_000.0 + index * 1_000.0,
                    "amount": (100_000.0 + index * 1_000.0) * close / 10.0,
                }
            )
        frame = normalize_daily_frame(
            pd.DataFrame(rows),
            source="SYNTHETIC_FIXTURE",
            source_version=f"demo-seed-{self.seed}",
            retrieved_at=f"{compact_date[:4]}-{compact_date[4:6]}-{compact_date[6:]}T16:10:00+08:00",
            data_source_mode="demo",
        )
        return ProviderResult("OK", frame, "demo", details={"seed": self.seed, "rows": len(frame)})


def _nullable_text(value: Any) -> str | None:
    if value is None or value is pd.NA or (isinstance(value, float) and np.isnan(value)):
        return None
    text = str(value).strip()
    return text or None


def _optional_series(frame: pd.DataFrame, column: str, default: Any = None) -> pd.Series:
    if column in frame.columns:
        return frame[column]
    return pd.Series(default, index=frame.index)


def _decimal_text(value: Any) -> str | None:
    text = _nullable_text(value)
    if text is None:
        return None
    try:
        return format(Decimal(text), "f")
    except InvalidOperation:
        return None


def _positive_decimal(value: str | None) -> bool:
    return value is not None and Decimal(value) > 0
