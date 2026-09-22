from __future__ import annotations

import pandas as pd

from core.strategies.base import BaseStrategy


class ShortTermStrategy(BaseStrategy):
    name = "short_term"

    def __init__(self, lookback: int = 3, rebound_threshold: float = 0.015) -> None:
        self.lookback = lookback
        self.rebound_threshold = rebound_threshold

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        pullback = out["close"].pct_change(self.lookback)
        rebound = out["close"].pct_change()
        out["signal"] = ((pullback < -0.02) & (rebound > self.rebound_threshold)).astype(int)
        return out
