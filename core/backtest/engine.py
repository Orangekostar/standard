from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.backtest.metrics import annualized_return, max_drawdown, sharpe_ratio


@dataclass
class BacktestResult:
    equity_curve: pd.DataFrame
    stats: dict


class BacktestEngine:
    """Legacy close-to-close reference; not valid as a Technical V2 return engine."""

    engine_status = "LEGACY_REFERENCE_ONLY"

    def __init__(self, fee_rate: float = 0.0005) -> None:
        self.fee_rate = fee_rate

    def run(self, signal_df: pd.DataFrame) -> BacktestResult:
        df = signal_df.copy()
        df["ret"] = df["close"].pct_change().fillna(0.0)
        df["position"] = df["signal"].shift(1).fillna(0.0)
        turnover = df["position"].diff().abs().fillna(0.0)
        df["strategy_ret"] = df["position"] * df["ret"] - turnover * self.fee_rate
        df["benchmark_ret"] = df["ret"]
        df["strategy_nav"] = (1 + df["strategy_ret"]).cumprod()
        df["benchmark_nav"] = (1 + df["benchmark_ret"]).cumprod()

        stats = {
            "ann_return": annualized_return(df["strategy_ret"]),
            "sharpe": sharpe_ratio(df["strategy_ret"]),
            "max_drawdown": max_drawdown(df["strategy_nav"]),
            "win_rate": float((df["strategy_ret"] > 0).mean()),
        }
        return BacktestResult(equity_curve=df, stats=stats)
