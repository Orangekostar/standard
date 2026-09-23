from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.analysis.sector_v2 import TechnicalContext

DIRECTIONAL_FACTOR_IDS = tuple(f"F{index:02d}" for index in range(1, 16))
RISK_INDICATOR_IDS = tuple(f"Q{index:02d}" for index in range(1, 7))


@dataclass(frozen=True)
class FactorMetadata:
    factor_id: str
    name: str
    group: str
    kind: str
    lookback: int
    unit: str
    inputs: tuple[str, ...] = ()
    formula_version: str = "technical-v2-fixed-v1"


TECHNICAL_V2_REGISTRY = {
    "F01": FactorMetadata("F01", "mom20_riskadj", "T", "directional", 20, "unitless", ("adjusted_close",)),
    "F02": FactorMetadata("F02", "mom60_riskadj", "T", "directional", 60, "unitless", ("adjusted_close",)),
    "F03": FactorMetadata("F03", "ma_structure", "T", "directional", 60, "unitless", ("adjusted_close", "adjusted_high", "adjusted_low")),
    "F04": FactorMetadata("F04", "relative_sector20", "R", "directional", 20, "unitless", ("adjusted_close", "sector_index")),
    "F05": FactorMetadata("F05", "relative_market20", "R", "directional", 20, "unitless", ("adjusted_close", "market_index")),
    "F06": FactorMetadata("F06", "breakout_distance20", "S", "directional", 20, "unitless", ("adjusted_close", "adjusted_high")),
    "F07": FactorMetadata("F07", "range_position20", "S", "directional", 20, "unitless", ("adjusted_close", "adjusted_high", "adjusted_low")),
    "F08": FactorMetadata("F08", "close_pressure5", "S", "directional", 5, "unitless", ("adjusted_close", "adjusted_high", "adjusted_low")),
    "F09": FactorMetadata("F09", "signed_amount_surprise", "V", "directional", 20, "unitless", ("adjusted_close", "amount_cny")),
    "F10": FactorMetadata("F10", "amount_close_pressure20", "V", "directional", 20, "unitless", ("adjusted_close", "adjusted_high", "adjusted_low", "amount_cny")),
    "F11": FactorMetadata("F11", "signed_volume_balance5", "V", "directional", 5, "unitless", ("adjusted_close", "volume_shares")),
    "F12": FactorMetadata("F12", "sector_relative20", "C", "directional", 20, "unitless", ("sector_index", "market_index")),
    "F13": FactorMetadata("F13", "sector_breadth20", "C", "directional", 20, "unitless", ("sector_breadth20",)),
    "F14": FactorMetadata("F14", "market_breadth20", "C", "directional", 20, "unitless", ("market_breadth20",)),
    "F15": FactorMetadata("F15", "market_trend20", "C", "directional", 20, "unitless", ("market_index",)),
    "Q01": FactorMetadata("Q01", "atr_pct14", "RISK", "risk", 14, "fraction", ("adjusted_high", "adjusted_low", "adjusted_close")),
    "Q02": FactorMetadata("Q02", "volatility20", "RISK", "risk", 20, "fraction", ("adjusted_close",)),
    "Q03": FactorMetadata("Q03", "adv20_cny", "RISK", "risk", 20, "CNY", ("amount_cny",)),
    "Q04": FactorMetadata("Q04", "illiquidity20", "RISK", "risk", 20, "per_CNY", ("adjusted_close", "amount_cny")),
    "Q05": FactorMetadata("Q05", "drawdown20", "RISK", "risk", 20, "fraction", ("adjusted_close",)),
    "Q06": FactorMetadata("Q06", "gap_today", "RISK", "risk", 1, "fraction", ("adjusted_open", "adjusted_close")),
}


def _compact_date(value: object) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip().replace("-", "")[:8]


def _finite(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric.where(np.isfinite(numeric))


def apply_adjustment_factors(
    raw_bars: pd.DataFrame,
    adjustments: pd.DataFrame,
    *,
    as_of: str,
) -> pd.DataFrame:
    bar_required = {"code", "date", "open", "high", "low", "close"}
    adjustment_required = {"code", "date", "adj_factor"}
    if missing := sorted(bar_required.difference(raw_bars.columns)):
        raise ValueError(f"raw bars missing columns: {', '.join(missing)}")
    if missing := sorted(adjustment_required.difference(adjustments.columns)):
        raise ValueError(f"adjustments missing columns: {', '.join(missing)}")
    cutoff = _compact_date(as_of)
    bars = raw_bars.copy()
    factors = adjustments.copy()
    for frame in (bars, factors):
        frame["code"] = frame["code"].astype(str)
        frame["date"] = frame["date"].map(_compact_date)
    bars = bars.loc[bars["date"] <= cutoff].sort_values(["code", "date"])
    factors = factors.loc[factors["date"] <= cutoff, ["code", "date", "adj_factor"]]
    factors["adj_factor"] = pd.to_numeric(factors["adj_factor"], errors="coerce")
    factors = factors.sort_values(["code", "date"]).drop_duplicates(["code", "date"], keep="last")
    reference = factors.dropna(subset=["adj_factor"]).groupby("code", as_index=False).tail(1)
    reference = reference[["code", "adj_factor"]].rename(columns={"adj_factor": "as_of_adj_factor"})
    out = bars.merge(factors, on=["code", "date"], how="left", validate="one_to_one")
    out = out.merge(reference, on="code", how="left", validate="many_to_one")
    valid = out["adj_factor"].gt(0) & out["as_of_adj_factor"].gt(0)
    scale = (out["adj_factor"] / out["as_of_adj_factor"]).where(valid)
    for column in ("open", "high", "low", "close"):
        out[f"adjusted_{column}"] = pd.to_numeric(out[column], errors="coerce") * scale
    out["adjustment_status"] = np.where(valid, "OK", "ADJUSTMENT_UNAVAILABLE")
    return out.reset_index(drop=True)


def _assign_metric(out: pd.DataFrame, metric_id: str, values: pd.Series, reason: str) -> None:
    clean = _finite(values)
    out[metric_id] = clean
    out[f"{metric_id}_reason"] = np.where(clean.notna(), "", reason)


def _preferred_assignments(context: TechnicalContext) -> pd.DataFrame:
    assignments = context.assignments
    if assignments is None or assignments.empty:
        return pd.DataFrame(columns=["code", "date", "namespace", "sector_id"])
    out = assignments.copy()
    preference = {"SW_L1": 0, "STOCK_BASIC_INDUSTRY": 1, "CURRENT": 1}
    out["preference"] = out["namespace"].astype(str).map(preference)
    out = out.loc[out["preference"].notna()]
    return (
        out.sort_values(["code", "date", "preference", "namespace", "sector_id"])
        .drop_duplicates(["code", "date"], keep="first")
        [["code", "date", "namespace", "sector_id"]]
    )


def _join_context(panel: pd.DataFrame, context: TechnicalContext) -> pd.DataFrame:
    out = panel.merge(_preferred_assignments(context), on=["code", "date"], how="left", validate="many_to_one")
    market_columns = {
        "index_level": "market_index",
        "sigma20": "market_sigma20",
        "log_return20": "market_log_return20",
        "breadth20": "market_breadth20",
        "status": "market_context_status",
        "reason_code": "market_context_reason",
    }
    if context.market is not None and not context.market.empty:
        market = context.market[["date", *market_columns]].rename(columns=market_columns)
        out = out.merge(market, on="date", how="left", validate="many_to_one")
    else:
        for column in market_columns.values():
            out[column] = np.nan
    sector_columns = {
        "index_level": "sector_index",
        "sigma20": "sector_sigma20",
        "log_return20": "sector_log_return20",
        "breadth20": "sector_breadth20",
        "status": "sector_context_status",
        "reason_code": "sector_context_reason",
    }
    if context.sectors is not None and not context.sectors.empty:
        sectors = context.sectors[["date", "namespace", "sector_id", *sector_columns]].rename(columns=sector_columns)
        out = out.merge(sectors, on=["date", "namespace", "sector_id"], how="left", validate="many_to_one")
    else:
        for column in sector_columns.values():
            out[column] = np.nan
    return out


def compute_technical_v2(
    panel: pd.DataFrame,
    context: TechnicalContext,
    *,
    as_of: str | None = None,
) -> pd.DataFrame:
    required = {
        "code",
        "date",
        "adjusted_open",
        "adjusted_high",
        "adjusted_low",
        "adjusted_close",
        "volume_shares",
        "amount_cny",
    }
    missing = sorted(required.difference(panel.columns))
    if missing:
        raise ValueError(f"technical panel missing columns: {', '.join(missing)}")
    out = panel.copy()
    out["code"] = out["code"].astype(str)
    out["date"] = out["date"].map(_compact_date)
    if as_of is not None:
        out = out.loc[out["date"] <= _compact_date(as_of)]
    out = out.sort_values(["code", "date"]).drop_duplicates(["code", "date"], keep="last").reset_index(drop=True)
    for column in ("adjusted_open", "adjusted_high", "adjusted_low", "adjusted_close", "volume_shares", "amount_cny"):
        out[column] = pd.to_numeric(out[column], errors="coerce")
    if "mark_only" not in out.columns:
        out["mark_only"] = False
    out["mark_only"] = out["mark_only"].fillna(False).astype(bool)

    if out.empty:
        columns = list(out.columns)
        for metric_id in DIRECTIONAL_FACTOR_IDS + RISK_INDICATOR_IDS:
            columns.extend([metric_id, f"{metric_id}_reason"])
        columns.extend(
            [
                "Q04_effective_days",
                "overextended",
                "observed_real_bars60",
                "price_basis",
                "factor_valid_count",
                "feature_status",
            ]
        )
        return pd.DataFrame(columns=list(dict.fromkeys(columns)))

    pieces = []
    for _, stock in out.groupby("code", sort=True):
        work = stock.sort_values("date").copy()
        close = work["adjusted_close"]
        high = work["adjusted_high"]
        low = work["adjusted_low"]
        open_price = work["adjusted_open"]
        amount = work["amount_cny"]
        volume = work["volume_shares"]
        log_return = np.log(close / close.shift(1))
        sigma20 = log_return.rolling(20, min_periods=20).std(ddof=0)
        s = sigma20.clip(lower=0.005)
        previous_close = close.shift(1)
        true_range = pd.concat(
            [(high - low), (high - previous_close).abs(), (low - previous_close).abs()], axis=1
        ).max(axis=1, skipna=False)
        atr14 = true_range.rolling(14, min_periods=14).mean()
        atr_pct14 = atr14 / close
        a = atr_pct14.clip(lower=0.001)
        log_return20 = np.log(close / close.shift(20)).where(close.rolling(21, min_periods=1).count().eq(21))
        log_return60 = np.log(close / close.shift(60)).where(close.rolling(61, min_periods=1).count().eq(61))
        real_bars60 = (~work["mark_only"] & close.notna()).astype(int).rolling(60, min_periods=1).sum()
        log_return60 = log_return60.where(real_bars60.ge(50))
        ma20 = close.rolling(20, min_periods=20).mean()
        ma60 = close.rolling(60, min_periods=60).mean()
        high20prev = high.shift(1).rolling(20, min_periods=20).max()
        high20 = high.rolling(20, min_periods=20).max()
        low20 = low.rolling(20, min_periods=20).min()
        range_width = high20 - low20
        range_position = ((2.0 * close - high20 - low20) / range_width).where(range_width.ne(0), 0.0)
        bar_width = high - low
        clv = ((2.0 * close - high - low) / bar_width).where(bar_width.ne(0), 0.0)
        amount_median20prev = amount.shift(1).rolling(20, min_periods=20).median()

        _assign_metric(work, "F01", np.tanh(log_return20 / (s * np.sqrt(20.0))), "INSUFFICIENT_HISTORY")
        _assign_metric(work, "F02", np.tanh(log_return60 / (s * np.sqrt(60.0))), "INSUFFICIENT_HISTORY")
        _assign_metric(work, "F03", np.tanh((ma20 / ma60 - 1.0) / (3.0 * a)), "INSUFFICIENT_HISTORY")
        work["_stock_log_return20"] = log_return20
        _assign_metric(work, "F06", np.tanh((close / high20prev - 1.0) / a), "INSUFFICIENT_HISTORY")
        _assign_metric(work, "F07", range_position, "INSUFFICIENT_HISTORY")
        _assign_metric(work, "F08", clv.rolling(5, min_periods=5).mean(), "INSUFFICIENT_HISTORY")
        f09 = np.sign(log_return) * np.tanh(np.log(amount / amount_median20prev))
        f09 = f09.where(amount.gt(0) & amount_median20prev.gt(0) & ~work["mark_only"])
        _assign_metric(work, "F09", f09, "INVALID_AMOUNT_OR_MARK_ONLY")
        amount_sum20 = amount.rolling(20, min_periods=20).sum()
        _assign_metric(
            work,
            "F10",
            (clv * amount).rolling(20, min_periods=20).sum().div(amount_sum20).where(amount_sum20.gt(0)),
            "INVALID_AMOUNT_WINDOW",
        )
        volume_sum5 = volume.rolling(5, min_periods=5).sum()
        _assign_metric(
            work,
            "F11",
            (np.sign(log_return) * volume).rolling(5, min_periods=5).sum().div(volume_sum5).where(volume_sum5.gt(0)),
            "INVALID_VOLUME_WINDOW",
        )
        _assign_metric(work, "Q01", atr_pct14, "INSUFFICIENT_HISTORY")
        _assign_metric(work, "Q02", sigma20, "INSUFFICIENT_HISTORY")
        _assign_metric(work, "Q03", amount.rolling(20, min_periods=20).mean(), "INSUFFICIENT_HISTORY")
        valid_illiquidity = amount.gt(0) & ~work["mark_only"] & log_return.notna()
        illiquidity_daily = (np.expm1(log_return).abs() / amount).where(valid_illiquidity)
        work["Q04_effective_days"] = valid_illiquidity.astype(int).rolling(20, min_periods=1).sum().astype(int)
        _assign_metric(work, "Q04", illiquidity_daily.rolling(20, min_periods=1).mean(), "NO_VALID_TRADING_DAYS")
        _assign_metric(work, "Q05", close / close.rolling(20, min_periods=20).max() - 1.0, "INSUFFICIENT_HISTORY")
        _assign_metric(work, "Q06", open_price / previous_close - 1.0, "INSUFFICIENT_HISTORY")
        work["overextended"] = ((close / ma20 - 1.0) > 3.0 * a).where(ma20.notna() & a.notna())
        work["observed_real_bars60"] = real_bars60.astype(int)
        work["price_basis"] = "ADJUSTED"
        pieces.append(work)

    result = pd.concat(pieces, ignore_index=True).sort_values(["code", "date"]).reset_index(drop=True)
    result = _join_context(result, context)
    sector_scale = pd.to_numeric(result["sector_sigma20"], errors="coerce").clip(lower=0.005)
    market_scale = pd.to_numeric(result["market_sigma20"], errors="coerce").clip(lower=0.005)
    stock_scale = pd.to_numeric(result["Q02"], errors="coerce").clip(lower=0.005)
    _assign_metric(
        result,
        "F04",
        np.tanh((result["_stock_log_return20"] - result["sector_log_return20"]) / (stock_scale * np.sqrt(20.0))),
        "SECTOR_CONTEXT_UNAVAILABLE",
    )
    _assign_metric(
        result,
        "F05",
        np.tanh((result["_stock_log_return20"] - result["market_log_return20"]) / (stock_scale * np.sqrt(20.0))),
        "MARKET_CONTEXT_UNAVAILABLE",
    )
    _assign_metric(
        result,
        "F12",
        np.tanh((result["sector_log_return20"] - result["market_log_return20"]) / (sector_scale * np.sqrt(20.0))),
        "SECTOR_CONTEXT_UNAVAILABLE",
    )
    _assign_metric(result, "F13", 2.0 * result["sector_breadth20"] - 1.0, "SECTOR_CONTEXT_UNAVAILABLE")
    _assign_metric(result, "F14", 2.0 * result["market_breadth20"] - 1.0, "MARKET_CONTEXT_UNAVAILABLE")
    _assign_metric(
        result,
        "F15",
        np.tanh(result["market_log_return20"] / (market_scale * np.sqrt(20.0))),
        "MARKET_CONTEXT_UNAVAILABLE",
    )
    result["factor_valid_count"] = result[list(DIRECTIONAL_FACTOR_IDS)].notna().sum(axis=1)
    result[list(DIRECTIONAL_FACTOR_IDS)] = result[list(DIRECTIONAL_FACTOR_IDS)].clip(-1.0, 1.0)
    result["feature_status"] = np.where(result["factor_valid_count"].ge(12), "OK", "INSUFFICIENT_FEATURES")
    return result.drop(columns=["_stock_log_return20"])
