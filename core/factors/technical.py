from __future__ import annotations

import numpy as np
import pandas as pd

from core.factors.base import BaseFactor, causal_expanding_percentile


class ReversalFactor(BaseFactor):
    name = "reversal_5"

    def __init__(self, window: int = 5) -> None:
        self.window = window

    def compute(self, df: pd.DataFrame) -> pd.Series:
        return -df["close"].pct_change(self.window).fillna(0.0)


class VolatilityFactor(BaseFactor):
    name = "volatility_20"

    def __init__(self, window: int = 20) -> None:
        self.window = window

    def compute(self, df: pd.DataFrame) -> pd.Series:
        ret = df["close"].pct_change().fillna(0.0)
        vol = ret.rolling(self.window).std().fillna(0.0)
        return -vol


class RSIFactor(BaseFactor):
    name = "rsi_14"

    def __init__(self, window: int = 14) -> None:
        self.window = window

    def compute(self, df: pd.DataFrame) -> pd.Series:
        delta = df["close"].diff().fillna(0.0)
        gain = delta.clip(lower=0.0)
        loss = (-delta).clip(lower=0.0)
        avg_gain = gain.rolling(self.window).mean()
        avg_loss = loss.rolling(self.window).mean()
        rs = avg_gain / (avg_loss + 1e-12)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        return ((rsi - 50.0) / 50.0).fillna(0.0)


class MABiasFactor(BaseFactor):
    name = "ma_bias_20"

    def __init__(self, window: int = 20) -> None:
        self.window = window

    def compute(self, df: pd.DataFrame) -> pd.Series:
        ma = df["close"].rolling(self.window).mean()
        return ((df["close"] / (ma + 1e-12)) - 1.0).fillna(0.0)


class BreakoutFactor(BaseFactor):
    name = "breakout_20"

    def __init__(self, window: int = 20) -> None:
        self.window = window

    def compute(self, df: pd.DataFrame) -> pd.Series:
        rolling_high = df["high"].rolling(self.window).max()
        rolling_low = df["low"].rolling(self.window).min()
        width = (rolling_high - rolling_low).replace(0.0, np.nan)
        score = (df["close"] - rolling_low) / width
        return score.fillna(0.5) - 0.5



class Alpha9ReversalFactor(BaseFactor):
    name = "legacy_causal_alpha9_reversal_v2"

    def __init__(self, window: int = 5) -> None:
        self.window = window

    def compute(self, df: pd.DataFrame) -> pd.Series:
        ret_n = df["close"].pct_change(self.window).fillna(0.0)
        rank_n = causal_expanding_percentile(ret_n).fillna(0.5)
        sign_n = ret_n.apply(lambda x: 1.0 if x > 0 else (-1.0 if x < 0 else 0.0))
        return (rank_n * sign_n).fillna(0.0)
