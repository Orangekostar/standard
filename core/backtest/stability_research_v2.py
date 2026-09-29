from __future__ import annotations

import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from decimal import Decimal
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

from core.backtest.prism_compare_engine import _gzip_writer
from core.backtest.strategy_search_v2 import Candidate, SearchMarket, _SearchReplay, search_windows
from core.strategies.prism_a_share import finite
from core.technical_v2.contracts import ContractError

ENVIRONMENT_GATES = ("SECTOR20", "BREADTH", "SECTOR20_BREADTH")


@dataclass(frozen=True)
class EnvironmentPolicy:
    holding_sessions: int
    gate: str
    range_scale: float

    def __post_init__(self):
        if (self.holding_sessions not in {5, 10} or self.range_scale not in {.5, 1.}
                or self.gate not in {*ENVIRONMENT_GATES, "CONTROL"}
                or (self.gate == "CONTROL" and (self.holding_sessions != 10 or self.range_scale != 1.))):
            raise ContractError("policy is outside the fixed stability research grid")

    @property
    def policy_id(self):
        return f"ENV_H{self.holding_sessions}_{self.gate}_R{int(self.range_scale*100)}"

    @property
    def candidate(self):
        return Candidate("TREND_PULLBACK", 1, self.holding_sessions, .04)


def policy_grid():
    return [EnvironmentPolicy(*values) for values in product((5, 10), ENVIRONMENT_GATES, (.5, 1.))]


def control_policy():
    return EnvironmentPolicy(10, "CONTROL", 1.)


def environment_mask(frame, gate):
    if gate == "CONTROL":
        return pd.Series(True, index=frame.index)
    if gate not in ENVIRONMENT_GATES:
        raise ContractError("unknown environment gate")
    eligible = frame.market_context_status.eq("OK") & frame.sector_context_status.eq("OK")
    if gate in {"SECTOR20", "SECTOR20_BREADTH"}:
        eligible &= np.isfinite(frame.sector_log_return20) & frame.sector_log_return20.gt(0)
    if gate in {"BREADTH", "SECTOR20_BREADTH"}:
        eligible &= (np.isfinite(frame.market_breadth20) & np.isfinite(frame.sector_breadth20)
                     & frame.market_breadth20.ge(.5) & frame.sector_breadth20.ge(.5))
    return eligible.fillna(False)


def stability_windows(sessions, holdout):
    windows = search_windows(sessions, holdout, window_count=6, window_sessions=63, minimum_context=120)
    if len(holdout) != 132:
        raise ContractError("stability research requires two fixed 66-session holdout windows")
    windows.pop("holdout")
    return {**windows, "holdout_1": list(holdout[:66]), "holdout_2": list(holdout[66:])}


class _EnvironmentMarket:
    def __init__(self, base, policy):
        self.base, self.policy, self.frames = base, policy, base.frames
        self.filtered = {}

    def ranks(self, date, candidate):
        if date not in self.filtered:
            ranks = self.base.ranks(date, candidate)
            mask = environment_mask(self.frames[date].loc[ranks.index], self.policy.gate)
            self.filtered[date] = ranks.loc[mask]
        return self.filtered[date]


class _EnvironmentReplay(_SearchReplay):
    def __init__(self, *args, environment_policy, **kwargs):
        super().__init__(*args, **kwargs)
        self.environment_policy = environment_policy

    def _buy_budget(self, code):
        state = self.previous_rows[code].get("market_state")
        scale = self.environment_policy.range_scale if state == "RANGE" else 1. if state == "TREND_EXPANSION" else 0.
        target = int(Decimal(self.pending_budget) * Decimal(str(scale)))
        return min(super()._buy_budget(code), target)

    def _metric_fields(self, nav, fills, closed, remaining):
        return {**super()._metric_fields(nav, fills, closed, remaining),
                "strategy_id": self.environment_policy.policy_id,
                "environment_policy": asdict(self.environment_policy),
                "range_budget_applies_to": "NEW_ENTRIES_ONLY_NO_FORCED_REBALANCE",
                "budget_regime_timing": "PREVIOUS_SIGNAL_CLOSE"}


def replay_environment(market, *, sessions, policy, config, cost_scenario, output_dir,
                       run_id, split="selection_1", scope_flags=(), corporate_actions=None):
    if (sessions != sorted(set(sessions)) or len(sessions) <= 12
            or not isinstance(market, SearchMarket) or not set(sessions).issubset(market.frames)
            or cost_scenario not in config["costs"]["slippage_cases"]):
        raise ContractError("invalid environment replay calendar, market or cost case")
    output = Path(output_dir).resolve()
    if output.exists() and any(output.iterdir()):
        raise ContractError("environment replay requires a fresh result cell")
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    replay = _EnvironmentReplay(config, sessions, cost_scenario, output,
        run_id+":"+policy.policy_id, split, scope_flags, corporate_actions,
        candidate=policy.candidate, tail_sessions=11, environment_policy=policy)
    filtered_market = _EnvironmentMarket(market, policy)
    with ExitStack() as stack:
        writer = _gzip_writer(stack, output/"decisions.csv.gz",
            ["date", "code", "score", "forecast_class", "rank_score", "action", "reasons"])
        for index, date in enumerate(sessions):
            replay.search_day(filtered_market, date, sessions[index+1] if index+1 < len(sessions) else None, writer)
    return replay.finish(started)


def summarize_stability(policy, cells):
    expected = {(f"selection_{i}", cost) for i in range(1, 7) for cost in ("base", "stress")}
    keys = [(row["split"], row["cost_scenario"]) for row in cells]
    if len(keys) != len(expected) or set(keys) != expected:
        raise ContractError("stability selection requires exactly six paired discovery windows, never holdout")
    aggregates, reasons = {}, []
    for cost in ("base", "stress"):
        rows = sorted((row for row in cells if row["cost_scenario"] == cost), key=lambda r:r["split"])
        complete = all(row["status"] == "OK" and finite(row.get("net_return")) is not None
                       and finite(row.get("max_drawdown")) is not None for row in rows)
        returns = [row["net_return"] for row in rows] if complete else []
        count = sum(row["closed_trade_count"] for row in rows)
        wins = sum(row["winning_closed_trade_count"] for row in rows)
        aggregate = dict(status="OK" if complete else "NAV_INCOMPLETE",
            window_returns=returns, profitable_windows=sum(value > 0 for value in returns),
            mean_window_return=float(np.mean(returns)) if complete else None,
            lower_quartile_return=float(np.quantile(returns,.25)) if complete else None,
            worst_window_return=min(returns) if complete else None,
            max_drawdown=max(row["max_drawdown"] for row in rows) if complete else None,
            closed_trade_count=count, winning_closed_trade_count=wins, win_rate=wins/count if count else None,
            traded_date_count=sum(row["traded_date_count"] for row in rows),
            open_position_count=sum(row["open_position_count"] for row in rows),
            fees_total=sum(row["fees_total"] for row in rows),
            modeled_slippage_total=sum(row["modeled_slippage_total"] for row in rows))
        cost_reasons = []
        if not complete:
            cost_reasons.append("INCOMPLETE_NAV")
        else:
            if aggregate["profitable_windows"] < 5:
                cost_reasons.append("TOO_FEW_PROFITABLE_WINDOWS")
            if aggregate["mean_window_return"] <= 0:
                cost_reasons.append("NONPOSITIVE_MEAN_RETURN")
            if aggregate["worst_window_return"] < -.03-1e-12:
                cost_reasons.append("QUARTER_LOSS_ABOVE_LIMIT")
            if aggregate["max_drawdown"] > .10+1e-12:
                cost_reasons.append("DRAWDOWN_ABOVE_LIMIT")
        if count < 60:
            cost_reasons.append("TOO_FEW_CLOSED_TRADES")
        if aggregate["traded_date_count"] < 40:
            cost_reasons.append("TOO_FEW_TRADED_DATES")
        if aggregate["open_position_count"]:
            cost_reasons.append("UNSETTLED_AFTER_TAIL")
        reasons.extend(cost+":"+reason for reason in cost_reasons)
        aggregates[cost] = aggregate
    complete = all(item["status"] == "OK" for item in aggregates.values())
    return dict(policy_id=policy.policy_id, policy=asdict(policy), qualified=not reasons,
        rejection_reasons=reasons, cost_aggregates=aggregates,
        robust_lower_quartile=min(a["lower_quartile_return"] for a in aggregates.values()) if complete else None,
        robust_mean_return=min(a["mean_window_return"] for a in aggregates.values()) if complete else None,
        worst_drawdown=max(a["max_drawdown"] for a in aggregates.values()) if complete else None)


def select_stability(summaries):
    candidates = [row for row in summaries if row["policy"]["gate"] != "CONTROL"]
    if not candidates or len({row["policy_id"] for row in candidates}) != len(candidates):
        raise ContractError("stability selection requires unique non-control candidates")
    def key(row):
        lower, mean, drawdown = [finite(row[field]) for field in
                                ("robust_lower_quartile", "robust_mean_return", "worst_drawdown")]
        return (-(lower if lower is not None else -1.), -(mean if mean is not None else -1.),
                drawdown if drawdown is not None else 1., row["policy_id"])
    ranked = sorted(candidates, key=key)
    qualified = [row for row in ranked if row["qualified"]]
    return dict(chosen_policy_id=qualified[0]["policy_id"] if qualified else None,
        diagnostic_policy_id=ranked[0]["policy_id"], qualified_policy_count=len(qualified),
        ranking=[row["policy_id"] for row in ranked])
