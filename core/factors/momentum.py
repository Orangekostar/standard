from __future__ import annotations

import pandas as pd

from core.factors.base import BaseFactor


class MomentumFactor(BaseFactor):
    name = "momentum_20"

    def __init__(self, window: int = 20) -> None:
        self.window = window

    def compute(self, df: pd.DataFrame) -> pd.Series:
        return df["close"].pct_change(self.window).fillna(0.0)
