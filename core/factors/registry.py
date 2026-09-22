from __future__ import annotations

from typing import Dict

from core.factors.base import BaseFactor
from core.factors.fundamental import DividendFactor, MarketCapFactor, ValuePBFactor, ValuePEFactor
from core.factors.momentum import MomentumFactor
from core.factors.technical import Alpha9ReversalFactor, BreakoutFactor, MABiasFactor, RSIFactor, ReversalFactor, VolatilityFactor
from core.factors.volume_price import Alpha6TurnoverCovFactor, PriceVolumeCorrFactor, TurnoverRateFactor, VolumeSurgeFactor


def build_default_factors() -> Dict[str, BaseFactor]:
    factors: Dict[str, BaseFactor] = {
        "momentum_20": MomentumFactor(window=20),
        "reversal_5": ReversalFactor(window=5),
        "volatility_20": VolatilityFactor(window=20),
        "rsi_14": RSIFactor(window=14),
        "ma_bias_20": MABiasFactor(window=20),
        "breakout_20": BreakoutFactor(window=20),
        "turnover_rate_5": TurnoverRateFactor(window=5),
        "volume_surge_10": VolumeSurgeFactor(window=10),
        "price_volume_corr_10": PriceVolumeCorrFactor(window=10),
        "alpha_6_turnover_cov": Alpha6TurnoverCovFactor(window=20),
        "alpha_9_reversal": Alpha9ReversalFactor(window=5),
        "value_pe": ValuePEFactor(),
        "value_pb": ValuePBFactor(),
        "dividend_ratio": DividendFactor(),
        "small_cap": MarketCapFactor(),
    }
    return factors
