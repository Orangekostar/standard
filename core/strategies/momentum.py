from __future__ import annotations

import pandas as pd

from core.strategies.base import BaseStrategy


class MomentumStrategy(BaseStrategy):
    name = "momentum"

    def __init__(self, fast: int = 10, slow: int = 30) -> None:
        self.fast = fast
        self.slow = slow

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["ma_fast"] = out["close"].rolling(self.fast).mean()
        out["ma_slow"] = out["close"].rolling(self.slow).mean()
        out["signal"] = (out["ma_fast"] > out["ma_slow"]).astype(int)
        out["signal"] = out["signal"].fillna(0)
        return out
