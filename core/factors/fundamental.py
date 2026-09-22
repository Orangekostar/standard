from __future__ import annotations

import pandas as pd

from core.factors.base import BaseFactor


class ValuePEFactor(BaseFactor):
    name = "value_pe"

    def compute(self, df: pd.DataFrame) -> pd.Series:
        if "pe" not in df.columns:
            return pd.Series(0.0, index=df.index)
        pe = df["pe"].replace(0.0, pd.NA).astype(float)
        return (-pe).fillna(0.0)


class ValuePBFactor(BaseFactor):
    name = "value_pb"

    def compute(self, df: pd.DataFrame) -> pd.Series:
        if "pb" not in df.columns:
            return pd.Series(0.0, index=df.index)
        pb = df["pb"].replace(0.0, pd.NA).astype(float)
        return (-pb).fillna(0.0)


class DividendFactor(BaseFactor):
    name = "dividend_ratio"

    def compute(self, df: pd.DataFrame) -> pd.Series:
        if "dv_ratio" not in df.columns:
            return pd.Series(0.0, index=df.index)
        return df["dv_ratio"].fillna(0.0)


class MarketCapFactor(BaseFactor):
    name = "small_cap"

    def compute(self, df: pd.DataFrame) -> pd.Series:
        if "total_mv" not in df.columns:
            return pd.Series(0.0, index=df.index)
        return (-df["total_mv"]).fillna(0.0)
