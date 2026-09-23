from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from core.factors.registry import build_default_factors
from core.strategies.base import BaseStrategy


class FactorSelectionStrategy(BaseStrategy):
    name = "factor_selection"
    _LEGACY_FACTOR_ALIASES: ClassVar[dict[str, str]] = {
        "alpha_6_turnover_cov": "legacy_causal_alpha6_turnover_cov_v2",
        "alpha_9_reversal": "legacy_causal_alpha9_reversal_v2",
    }

    def __init__(
        self,
        threshold: float = 0.1,
        enabled_factors: Iterable[str] | None = None,
        factor_weights: dict[str, float] | None = None,
    ) -> None:
        self.threshold = threshold
        self.factors = build_default_factors()
        default_names = list(self.factors.keys())
        requested = list(enabled_factors) if enabled_factors is not None else default_names
        self.enabled_factors = [self._LEGACY_FACTOR_ALIASES.get(name, name) for name in requested]
        raw_weights = factor_weights or {name: 1.0 for name in self.enabled_factors}
        self.factor_weights = {
            self._LEGACY_FACTOR_ALIASES.get(name, name): float(weight)
            for name, weight in raw_weights.items()
        }

    @staticmethod
    def _zscore(series: pd.Series) -> pd.Series:
        # Past-only normalization to avoid look-ahead bias:
        # each timestamp uses statistics from historical data up to t-1.
        hist_mean = series.expanding(min_periods=5).mean().shift(1)
        hist_std = series.expanding(min_periods=5).std().shift(1)
        z = (series - hist_mean) / (hist_std + 1e-12)
        z = z.replace([np.inf, -np.inf], np.nan).fillna(0.0)
        return z

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()

        active_names = [name for name in self.enabled_factors if name in self.factors]
        if not active_names:
            active_names = ["momentum_20"]

        score = pd.Series(0.0, index=out.index)
        total_weight = 0.0

        for name in active_names:
            raw = self.factors[name].compute(out).fillna(0.0)
            out[f"factor_{name}"] = raw
            z = self._zscore(raw)
            w = float(self.factor_weights.get(name, 1.0))
            score = score + z * w
            total_weight += abs(w)

        if total_weight > 1e-12:
            score = score / total_weight

        out["factor_score"] = score.fillna(0.0)
        out["signal"] = (out["factor_score"] > self.threshold).astype(int)
        return out

    @staticmethod
    def _normalize_daily_frame(daily_df: pd.DataFrame) -> pd.DataFrame:
        out = daily_df.copy()
        if "trade_date" not in out.columns:
            raise ValueError("daily_df 缺少 trade_date 列")

        out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce")
        out = out.dropna(subset=["trade_date"]).sort_values("trade_date").drop_duplicates(subset=["trade_date"], keep="last")

        for col in ["open", "high", "low", "close", "vol", "amount"]:
            if col not in out.columns:
                out[col] = 0.0
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)
        return out.reset_index(drop=True)

    @staticmethod
    def _extract_minute_row(minute_bar: Mapping[str, Any] | pd.Series) -> dict[str, Any]:
        row = pd.Series(minute_bar)

        time_value = None
        for k in ["trade_time", "datetime", "time", "timestamp", "minute_time"]:
            if k in row and pd.notna(row[k]):
                time_value = row[k]
                break
        if time_value is None and "trade_date" in row and pd.notna(row["trade_date"]):
            time_value = row["trade_date"]
        if time_value is None:
            raise ValueError("minute_bar 缺少时间字段（trade_time/datetime/time/timestamp/trade_date）")

        ts = pd.to_datetime(time_value, errors="coerce")
        if pd.isna(ts):
            raise ValueError("minute_bar 时间字段无法解析")

        def _num(cands: list[str], default: float = 0.0) -> float:
            for c in cands:
                if c in row and pd.notna(row[c]):
                    try:
                        return float(row[c])
                    except (TypeError, ValueError):
                        continue
            return float(default)

        close = _num(["close", "price", "last"])
        open_px = _num(["open"], default=close)
        high = _num(["high"], default=close)
        low = _num(["low"], default=close)

        return {
            "minute_time": ts,
            "trade_date": ts.normalize(),
            "open": open_px,
            "high": high,
            "low": low,
            "close": close,
            "vol": _num(["vol", "volume"], default=0.0),
            "amount": _num(["amount", "turnover"], default=0.0),
            "turnover_rate": _num(["turnover_rate"], default=np.nan),
            "ts_code": str(row.get("ts_code", "") or "").strip(),
        }

    @staticmethod
    def _update_intraday_state(
        state: dict[str, Any] | None,
        minute_row: dict[str, Any],
    ) -> dict[str, Any]:
        if state is None or state.get("trade_date") is None or state.get("trade_date") != minute_row["trade_date"]:
            return {
                "trade_date": minute_row["trade_date"],
                "open": float(minute_row["open"]),
                "high": float(max(minute_row["open"], minute_row["high"], minute_row["close"])),
                "low": float(min(minute_row["open"], minute_row["low"], minute_row["close"])),
                "close": float(minute_row["close"]),
                "vol": float(max(0.0, minute_row["vol"])),
                "amount": float(max(0.0, minute_row["amount"])),
                "turnover_rate": minute_row.get("turnover_rate", np.nan),
                "ts_code": minute_row.get("ts_code", ""),
                "last_minute_time": minute_row["minute_time"],
            }

        if minute_row["minute_time"] <= state.get("last_minute_time"):
            raise ValueError("minute bars must be strictly increasing after deduplication")

        state["high"] = float(max(float(state["high"]), minute_row["high"], minute_row["close"]))
        state["low"] = float(min(float(state["low"]), minute_row["low"], minute_row["close"]))
        state["close"] = float(minute_row["close"])
        state["vol"] = float(max(0.0, state.get("vol", 0.0) + max(0.0, minute_row["vol"])))
        state["amount"] = float(max(0.0, state.get("amount", 0.0) + max(0.0, minute_row["amount"])))
        if pd.notna(minute_row.get("turnover_rate", np.nan)):
            state["turnover_rate"] = float(minute_row["turnover_rate"])
        state["last_minute_time"] = minute_row["minute_time"]
        if minute_row.get("ts_code"):
            state["ts_code"] = minute_row["ts_code"]
        return state

    def generate_intraday_signal(
        self,
        daily_df: pd.DataFrame,
        minute_bar: Mapping[str, Any] | pd.Series,
        state: dict[str, Any] | None = None,
    ) -> tuple[pd.Series, pd.DataFrame, dict[str, Any]]:
        m = self._extract_minute_row(minute_bar)
        daily = self._normalize_daily_frame(daily_df)
        daily = daily.loc[daily["trade_date"] < m["trade_date"]].reset_index(drop=True)
        state = self._update_intraday_state(state, m)

        base_row = daily.iloc[-1].to_dict() if not daily.empty else {}
        row = {c: base_row.get(c, np.nan) for c in daily.columns}
        row["trade_date"] = state["trade_date"]
        row["open"] = state["open"]
        row["high"] = state["high"]
        row["low"] = state["low"]
        row["close"] = state["close"]
        row["vol"] = state["vol"]
        row["amount"] = state["amount"]
        if "turnover_rate" in daily.columns:
            row["turnover_rate"] = state.get("turnover_rate", row.get("turnover_rate", np.nan))
        if "ts_code" in daily.columns and state.get("ts_code"):
            row["ts_code"] = state["ts_code"]

        today = pd.Timestamp(state["trade_date"])
        mask_today = daily["trade_date"] == today
        if mask_today.any():
            daily.loc[mask_today, list(row.keys())] = [row[k] for k in row]
            merged = daily
        else:
            merged = pd.concat([daily, pd.DataFrame([row])], ignore_index=True)

        merged = merged.sort_values("trade_date").reset_index(drop=True)
        signal_df = self.generate_signals(merged)
        latest = signal_df.iloc[-1].copy()
        latest["minute_time"] = state.get("last_minute_time")
        return latest, signal_df, state

    def generate_intraday_signals(
        self,
        daily_df: pd.DataFrame,
        minute_df: pd.DataFrame,
    ) -> pd.DataFrame:
        if minute_df is None or minute_df.empty:
            return pd.DataFrame()

        state: dict[str, Any] | None = None
        rows = []
        minute_sorted = minute_df.copy()
        sort_col = None
        for c in ["trade_time", "datetime", "time", "timestamp", "minute_time", "trade_date"]:
            if c in minute_sorted.columns:
                sort_col = c
                break
        if sort_col:
            minute_sorted[sort_col] = pd.to_datetime(minute_sorted[sort_col], errors="coerce")
            minute_sorted = (
                minute_sorted.dropna(subset=[sort_col])
                .sort_values(sort_col)
                .drop_duplicates(subset=[sort_col], keep="last")
                .reset_index(drop=True)
            )

        for _, bar in minute_sorted.iterrows():
            latest, _, state = self.generate_intraday_signal(daily_df=daily_df, minute_bar=bar, state=state)
            rows.append(latest)

        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows).reset_index(drop=True)
