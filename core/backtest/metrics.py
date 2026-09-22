from __future__ import annotations

import numpy as np
import pandas as pd


def annualized_return(daily_returns: pd.Series, trading_days: int = 252) -> float:
    daily_returns = daily_returns.dropna()
    if daily_returns.empty:
        return 0.0
    cumulative = (1 + daily_returns).prod()
    years = len(daily_returns) / trading_days
    if years <= 0:
        return 0.0
    return float(cumulative ** (1 / years) - 1)


def sharpe_ratio(daily_returns: pd.Series, risk_free: float = 0.0, trading_days: int = 252) -> float:
    daily_returns = daily_returns.dropna()
    if daily_returns.std() == 0 or daily_returns.empty:
        return 0.0
    excess = daily_returns - risk_free / trading_days
    return float(np.sqrt(trading_days) * excess.mean() / excess.std())


def max_drawdown(nav: pd.Series) -> float:
    nav = nav.dropna()
    if nav.empty:
        return 0.0
    rolling_max = nav.cummax()
    drawdown = nav / rolling_max - 1
    return float(drawdown.min())
