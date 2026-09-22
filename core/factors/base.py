from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class BaseFactor(ABC):
    name: str = "base_factor"

    @abstractmethod
    def compute(self, df: pd.DataFrame) -> pd.Series:
        raise NotImplementedError
