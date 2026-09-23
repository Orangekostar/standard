from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import numpy as np
import pandas as pd

from core.technical_v2.contracts import ContractError


@dataclass(frozen=True)
class PortfolioMetrics:
    status: str
    net_return: float | None
    annualized_return: float | None
    annualized_volatility: float | None
    sharpe: float | None
    max_drawdown: float | None
    fill_count: int
    closed_trade_win_rate: float | None
    positive_day_ratio: float | None
    turnover: float | None
    average_cash_ratio: float | None
    unfilled_order_ratio: float | None
    max_sector_exposure: float | None
    unresolved_nav_days: int
    classification_accuracy: float | None
    mean_rank_ic: float | None


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _classification_metrics(predictions: pd.DataFrame | None) -> tuple[float | None, float | None]:
    if predictions is None or predictions.empty:
        return None, None
    required = {"as_of_trade_date", "target_class", "forecast_class"}
    if missing := sorted(required.difference(predictions.columns)):
        raise ContractError(f"prediction metrics missing columns: {', '.join(missing)}")
    normalized = predictions.copy()
    normalized["target_class"] = normalized["target_class"].astype(str).str.upper()
    normalized["forecast_class"] = normalized["forecast_class"].astype(str).str.upper()
    observed = normalized.loc[
        normalized["target_class"].isin(("UP", "FLAT", "DOWN"))
        & normalized["forecast_class"].isin(("UP", "FLAT", "DOWN"))
    ].copy()
    accuracy = float(observed["target_class"].eq(observed["forecast_class"]).mean()) if not observed.empty else None
    rank_ic = None
    if {"signal_strength", "realized_return"}.issubset(observed.columns):
        observed["signal_strength"] = pd.to_numeric(observed["signal_strength"], errors="coerce")
        observed["realized_return"] = pd.to_numeric(observed["realized_return"], errors="coerce")
        daily = []
        for _, group in observed.groupby("as_of_trade_date", sort=True):
            valid = group[["signal_strength", "realized_return"]].dropna()
            if len(valid) >= 2 and valid["signal_strength"].nunique() > 1 and valid["realized_return"].nunique() > 1:
                signal_rank = valid["signal_strength"].rank(method="average")
                return_rank = valid["realized_return"].rank(method="average")
                daily.append(float(signal_rank.corr(return_rank)))
        if daily:
            rank_ic = float(np.mean(daily))
    return accuracy, rank_ic


def calculate_portfolio_metrics(
    nav_rows: pd.DataFrame,
    *,
    fills: pd.DataFrame | None = None,
    orders: pd.DataFrame | None = None,
    closed_trades: pd.DataFrame | None = None,
    predictions: pd.DataFrame | None = None,
    trading_days: int = 252,
) -> PortfolioMetrics:
    required = {"date", "nav_cents", "cash_cents", "status"}
    if missing := sorted(required.difference(nav_rows.columns)):
        raise ContractError(f"NAV metrics missing columns: {', '.join(missing)}")
    nav = nav_rows.copy().sort_values("date").drop_duplicates("date", keep="last")
    nav["nav_cents"] = pd.to_numeric(nav["nav_cents"], errors="coerce")
    nav["cash_cents"] = pd.to_numeric(nav["cash_cents"], errors="coerce")
    unresolved_days = int((nav["status"].astype(str).ne("OK") | nav["nav_cents"].isna()).sum())
    complete = unresolved_days == 0 and not nav.empty
    values = nav["nav_cents"].astype(float) if complete else pd.Series(dtype=float)
    returns = values.pct_change().dropna() if complete else pd.Series(dtype=float)
    net_return = float(values.iloc[-1] / values.iloc[0] - 1.0) if complete and len(values) >= 2 else None
    annualized_return = None
    annualized_volatility = None
    sharpe = None
    max_drawdown = None
    positive_day_ratio = None
    if complete and len(values) >= 2:
        periods = len(values) - 1
        if values.iloc[0] > 0 and values.iloc[-1] > 0:
            annualized_return = float((values.iloc[-1] / values.iloc[0]) ** (trading_days / periods) - 1.0)
        annualized_volatility = float(returns.std(ddof=0) * math.sqrt(trading_days))
        if annualized_volatility > 0:
            sharpe = float(math.sqrt(trading_days) * returns.mean() / returns.std(ddof=0))
        rolling_peak = values.cummax()
        max_drawdown = float(-(values / rolling_peak - 1.0).min())
        positive_day_ratio = float(returns.gt(0).mean())

    fill_frame = fills if fills is not None else pd.DataFrame()
    fill_count = len(fill_frame)
    turnover = None
    if complete and not fill_frame.empty and {"quantity", "price"}.issubset(fill_frame.columns):
        notionals_cents = []
        for row in fill_frame.to_dict(orient="records"):
            price = _decimal(row.get("price"))
            quantity = int(row.get("quantity", 0) or 0)
            if price is not None and price >= 0 and quantity >= 0:
                notionals_cents.append(float(price * quantity * Decimal(100)))
        if notionals_cents and values.mean() > 0:
            turnover = float(sum(notionals_cents) / values.mean())

    closed = closed_trades if closed_trades is not None else pd.DataFrame()
    closed_win_rate = None
    if not closed.empty and {"status", "realized_pnl_cents"}.issubset(closed.columns):
        realized = closed.loc[closed["status"].astype(str).eq("CLOSED")].copy()
        pnl = pd.to_numeric(realized["realized_pnl_cents"], errors="coerce").dropna()
        if not pnl.empty:
            closed_win_rate = float(pnl.gt(0).mean())

    average_cash_ratio = None
    if complete:
        valid_cash = nav.loc[nav["nav_cents"].gt(0), ["cash_cents", "nav_cents"]].dropna()
        if not valid_cash.empty:
            average_cash_ratio = float((valid_cash["cash_cents"] / valid_cash["nav_cents"]).mean())

    order_frame = orders if orders is not None else pd.DataFrame()
    unfilled_ratio = None
    if not order_frame.empty and "status" in order_frame.columns:
        unfilled_ratio = float((~order_frame["status"].astype(str).eq("FILLED")).mean())

    max_sector_exposure = None
    if "sector_exposure" in nav.columns:
        exposures = []
        for value in nav["sector_exposure"]:
            if isinstance(value, dict):
                exposures.extend(float(item) for item in value.values() if math.isfinite(float(item)))
        if exposures:
            max_sector_exposure = max(exposures)

    accuracy, rank_ic = _classification_metrics(predictions)
    if not complete:
        status = "NAV_INCOMPLETE"
    elif len(nav) < 2:
        status = "INSUFFICIENT_HISTORY"
    else:
        status = "OK"
    return PortfolioMetrics(
        status=status,
        net_return=net_return,
        annualized_return=annualized_return,
        annualized_volatility=annualized_volatility,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        fill_count=fill_count,
        closed_trade_win_rate=closed_win_rate,
        positive_day_ratio=positive_day_ratio,
        turnover=turnover,
        average_cash_ratio=average_cash_ratio,
        unfilled_order_ratio=unfilled_ratio,
        max_sector_exposure=max_sector_exposure,
        unresolved_nav_days=unresolved_days,
        classification_accuracy=accuracy,
        mean_rank_ic=rank_ic,
    )
