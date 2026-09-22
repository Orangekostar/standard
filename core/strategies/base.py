from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class BaseStrategy(ABC):
    name: str = "base_strategy"

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError
