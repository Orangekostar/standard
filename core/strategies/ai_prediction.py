from __future__ import annotations

import pandas as pd

from core.strategies.base import BaseStrategy


class AIPredictionStrategy(BaseStrategy):
    name = "ai_prediction"

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["pred_return"] = out["close"].pct_change().rolling(5).mean().shift(1).fillna(0)
        out["signal"] = (out["pred_return"] > 0).astype(int)
        return out
