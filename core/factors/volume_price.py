from __future__ import annotations

import pandas as pd

from core.factors.base import BaseFactor, causal_expanding_percentile


class TurnoverRateFactor(BaseFactor):
    name = "turnover_rate_5"

    def __init__(self, window: int = 5) -> None:
        self.window = window

    def compute(self, df: pd.DataFrame) -> pd.Series:
        if "turnover_rate" not in df.columns:
            return pd.Series(0.0, index=df.index)
        return df["turnover_rate"].rolling(self.window).mean().fillna(0.0)


class VolumeSurgeFactor(BaseFactor):
    name = "volume_surge_10"

    def __init__(self, window: int = 10) -> None:
        self.window = window

    def compute(self, df: pd.DataFrame) -> pd.Series:
        vol_ma = df["vol"].rolling(self.window).mean()
        score = (df["vol"] / (vol_ma + 1e-12)) - 1.0
        return score.fillna(0.0)


class PriceVolumeCorrFactor(BaseFactor):
    name = "price_volume_corr_10"

    def __init__(self, window: int = 10) -> None:
        self.window = window

    def compute(self, df: pd.DataFrame) -> pd.Series:
        ret = df["close"].pct_change().fillna(0.0)
        vol_chg = df["vol"].pct_change().fillna(0.0)
        corr = ret.rolling(self.window).corr(vol_chg)
        return corr.fillna(0.0)



class Alpha6TurnoverCovFactor(BaseFactor):
    name = "legacy_causal_alpha6_turnover_cov_v2"

    def __init__(self, window: int = 20) -> None:
        self.window = window

    def compute(self, df: pd.DataFrame) -> pd.Series:
        if "turnover_rate" not in df.columns:
            turnover_rank = pd.Series(0.5, index=df.index)
        else:
            turnover_rank = causal_expanding_percentile(df["turnover_rate"]).fillna(0.5)
        ret_t = df["close"].pct_change().fillna(0.0)
        ret_t_1 = ret_t.shift(1).fillna(0.0)
        corr = ret_t.rolling(self.window).corr(ret_t_1).fillna(0.0)
        return (-1.0 * corr * turnover_rank).fillna(0.0)
