from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np
import pandas as pd


class BaseFactor(ABC):
    name: str = "base_factor"

    @abstractmethod
    def compute(self, df: pd.DataFrame) -> pd.Series:
        raise NotImplementedError


def causal_expanding_percentile(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")

    def rank_last(values: np.ndarray) -> float:
        clean = values[np.isfinite(values)]
        if clean.size == 0 or not np.isfinite(values[-1]):
            return np.nan
        current = values[-1]
        less = float(np.sum(clean < current))
        equal = float(np.sum(clean == current))
        return (less + (equal + 1.0) / 2.0) / float(clean.size)

    return numeric.expanding(min_periods=1).apply(rank_last, raw=True)
