from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def market_regimes(context: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    out = context.sort_values("date").copy().reset_index(drop=True)
    policy = config["strategies"]["B0_PRISM_A_SHARE_V1"]["market_state"]
    returns = pd.to_numeric(out["log_return"], errors="coerce")
    window = policy["efficiency_sessions"]
    sum_returns = returns.rolling(window, min_periods=window).sum()
    sum_abs = returns.abs().rolling(window, min_periods=window).sum()
    out["ER20"] = (sum_returns.abs() / sum_abs).where(sum_abs.ne(0), policy["zero_er_denominator"])
    out["breadth_delta5"] = out["breadth20"] - out["breadth20"].shift(policy["breadth_delta_sessions"])
    volatility_window = policy["volatility_sessions"]
    out["market_sigma20"] = returns.rolling(volatility_window, min_periods=volatility_window).std(ddof=0)
    reference_window = policy["prior_vol_median_sessions"]
    reference = out["market_sigma20"].shift(1).rolling(reference_window, min_periods=reference_window).median()
    out["market_vol_reference"] = reference
    out["market_vol_ratio"] = out["market_sigma20"] / reference.where(reference.gt(0))
    required = ["ER20", "breadth20", "breadth_delta5", "market_vol_ratio", "log_return20"]
    known = np.isfinite(out[required].astype(float)).all(axis=1) & out["status"].eq("OK")
    stress = (out["breadth20"].lt(policy["stress_breadth_below"])
              | (out["breadth_delta5"].le(policy["stress_breadth_delta_at_most"])
                 & out["market_vol_ratio"].ge(policy["stress_vol_ratio_at_least"])))
    trend = (out["log_return20"].gt(policy["trend_log_return20_above"])
             & out["ER20"].ge(policy["trend_er_at_least"])
             & out["breadth_delta5"].ge(policy["trend_breadth_delta_at_least"]))
    out["market_state"] = np.select([~known, stress, trend],
                                   ["UNKNOWN", "STRESS", "TREND_EXPANSION"], default="RANGE")
    out["state_multiplier"] = out["market_state"].map(policy["risk_multiplier"]).astype(float)
    out["state_reason"] = np.where(out["market_state"].eq("UNKNOWN"), "MARKET_STATE_FALLBACK_TO_A", "")
    return out


def adaptive_distance(q01: Any, sigma: Any, reference: Any, config: dict[str, Any]) -> tuple[float | None, tuple[str, ...]]:
    atr = finite(q01)
    if atr is None or atr < 0:
        return None, ("ATR_UNAVAILABLE",)
    policy = config["strategies"]["B0_PRISM_A_SHARE_V1"]["adaptive_stop"]
    volatility, denominator = finite(sigma), finite(reference)
    reasons: tuple[str, ...] = ()
    if volatility is None or volatility < 0 or denominator is None or denominator <= 0:
        k = policy["missing_reference_k"]
        reasons = ("ADAPTIVE_WIDTH_FALLBACK",)
    else:
        low, high = policy["vol_ratio_clip"]
        k = policy["base_atr_multiple"] * min(high, max(low, volatility / denominator))
    low, high = policy["distance_fraction_clip"]
    return min(high, max(low, k * atr)), reasons


def update_stop(old_stop: float, highest_close: float, close: Any,
                distance: Any) -> tuple[float, float, bool]:
    current, width = finite(close), finite(distance)
    if current is None or current <= 0:
        return old_stop, highest_close, False
    highest_close = max(highest_close, current)
    stop = max(old_stop, highest_close - width * current) if width is not None else old_stop
    return stop, highest_close, current <= stop


def entry_allowed(entry_index: int, last_flat_exit_index: int | None, cooldown: int) -> bool:
    return last_flat_exit_index is None or entry_index - last_flat_exit_index > cooldown
