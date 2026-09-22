from __future__ import annotations

from typing import Dict

from core.backtest.engine import BacktestEngine
from core.data.data_manager import DataManager
from core.data.symbols import normalize_ts_code
from core.strategies.ai_prediction import AIPredictionStrategy
from core.strategies.base import BaseStrategy
from core.strategies.factor_selection import FactorSelectionStrategy
from core.strategies.momentum import MomentumStrategy
from core.strategies.short_term import ShortTermStrategy


def build_strategies() -> Dict[str, BaseStrategy]:
    return {
        "因子选股": FactorSelectionStrategy(),
        "动量策略": MomentumStrategy(),
        "AI预测": AIPredictionStrategy(),
        "短线策略": ShortTermStrategy(),
    }


def run_pipeline(
    ts_code: str,
    start_date: str,
    end_date: str,
    strategy_name: str,
):
    data_manager = DataManager()
    strategies = build_strategies()
    strategy = strategies[strategy_name]
    normalized_code = normalize_ts_code(ts_code)
    raw_df = data_manager.get_daily_data(
        ts_code=normalized_code,
        start_date=start_date,
        end_date=end_date,
        use_cache=True,
    )
    signal_df = strategy.generate_signals(raw_df)
    engine = BacktestEngine()
    result = engine.run(signal_df)
    return result
