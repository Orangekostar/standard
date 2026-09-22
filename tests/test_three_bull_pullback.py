from __future__ import annotations

import unittest

import pandas as pd

from core.strategies.three_bull_pullback import (
    ThreeBullPullbackConfig,
    append_forward_returns,
    build_three_bull_pullback_signals,
    summarize_score_bins,
    summarize_three_bull_backtest,
)


def _sample_panel() -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-01", periods=36)
    closes: list[float] = []
    for idx in range(len(dates)):
        if idx < 25:
            closes.append(10.0 + idx * 0.02)
        elif idx == 25:
            closes.append(10.80)
        elif idx == 26:
            closes.append(11.10)
        elif idx == 27:
            closes.append(11.45)
        elif idx == 28:
            closes.append(11.10)
        elif idx == 29:
            closes.append(11.25)
        elif idx == 30:
            closes.append(11.65)
        elif idx == 31:
            closes.append(11.72)
        elif idx == 32:
            closes.append(11.85)
        else:
            closes.append(closes[-1] * 1.002)

    rows = []
    for idx, (day, close) in enumerate(zip(dates, closes)):
        if idx == 27:
            high = 11.55
            low = 11.05
        elif idx == 28:
            high = 11.20
            low = 10.98
        elif idx == 29:
            high = 11.35
            low = 11.05
        elif idx == 30:
            high = 11.80
            low = 11.25
        else:
            high = close * 1.01
            low = close * 0.99
        rows.append(
            {
                "ts_code": "000001.SZ",
                "trade_date": day,
                "open": close * 0.995,
                "high": high,
                "low": low,
                "close": close,
                "vol": 1000.0 + idx * 10.0,
                "amount": 1200.0 if idx not in {25, 26, 27, 29, 30} else 1800.0,
                "turnover_rate": 4.0,
                "pe": 20.0,
                "pb": 2.0,
                "total_mv": 500000.0,
                "name": "样本股",
                "industry": "测试行业",
                "market": "主板",
            }
        )
    return pd.DataFrame(rows)


class ThreeBullPullbackTest(unittest.TestCase):
    def test_builds_confirmed_signal_from_three_bull_pullback(self) -> None:
        signals = build_three_bull_pullback_signals(
            _sample_panel(),
            config=ThreeBullPullbackConfig(min_score=0.0),
        )

        self.assertFalse(signals.empty)
        self.assertIn("策略评分", signals.columns)
        self.assertTrue((signals["回踩天数"] >= 1).any())
        self.assertTrue(signals["候选来源"].astype(str).str.contains("右侧确认候选").any())
        self.assertTrue((signals["量能比"] >= 1.2).all())

    def test_weak_volume_is_watch_not_confirmed_buy(self) -> None:
        panel = _sample_panel()
        panel.loc[panel.index >= 30, "amount"] = 900.0

        confirmed = build_three_bull_pullback_signals(
            panel,
            config=ThreeBullPullbackConfig(min_score=0.0, include_watch=False),
        )
        watch = build_three_bull_pullback_signals(
            panel,
            config=ThreeBullPullbackConfig(min_score=0.0, include_watch=True),
        )

        self.assertTrue(confirmed.empty)
        self.assertFalse(watch.empty)
        self.assertTrue(watch["候选来源"].astype(str).str.contains("右侧观察候选").all())

    def test_backtest_summarizes_topn_and_score_bins(self) -> None:
        panel = _sample_panel()
        signals = build_three_bull_pullback_signals(
            panel,
            config=ThreeBullPullbackConfig(min_score=0.0),
        )
        trades = append_forward_returns(signals, panel, horizons=[1, 3], fee_rate=0.0005)
        summary = summarize_three_bull_backtest(trades, top_n_values=[1], horizon_days=3)
        bins = summarize_score_bins(trades, horizon_days=3)

        self.assertFalse(trades.empty)
        self.assertFalse(summary.empty)
        self.assertIn("次日胜率", summary.columns)
        self.assertFalse(bins.empty)


if __name__ == "__main__":
    unittest.main()
