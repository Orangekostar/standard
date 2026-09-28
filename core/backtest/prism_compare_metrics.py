from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np
import pandas as pd

from core.pipeline.prism_compare_config import STRATEGIES
from core.strategies.prism_a_share import finite
from core.technical_v2.contracts import ContractError

SUMMARY_COLUMNS = (
    "strategy_id", "split", "cost_scenario", "start_date", "end_date", "initial_nav", "final_nav",
    "net_return", "annualized_return", "max_drawdown", "sharpe", "daily_turnover", "total_turnover",
    "average_gross_exposure", "average_cash_ratio", "filled_buy_count", "closed_trade_count",
    "traded_dates", "closed_trade_win_rate", "profit_factor", "fees_total", "modeled_slippage_total",
    "rejected_candidate_count", "unfilled_order_ratio", "open_position_count", "unresolved_asset_count",
    "data_scope_status", "comparison_status",
)


def exposure_control(a: Any, b: Any, config: dict[str, Any]) -> dict[str, Any]:
    ea, eb = finite(a), finite(b)
    if ea is None or eb is None or min(ea, eb) < 0:
        raise ContractError("C requires reconciled nonnegative validation mean exposures")
    policy = config["strategies"][STRATEGIES[2]]
    if ea == 0:
        scale, status = policy["zero_denominator_lambda"], "UNIDENTIFIABLE_ZERO_BASE_EXPOSURE"
    else:
        lower, upper = policy["lambda_clip"]
        scale = float(np.clip(eb / ea, lower, upper))
        status = "UNMATCHABLE_HIGHER_B_EXPOSURE" if eb > ea else "IDENTIFIABLE_VALIDATION_DOWNSCALE"
    return {"lambda_c": scale, "status": status, "a_validation_mean_exposure": ea,
            "b_validation_mean_exposure": eb, "test_returns_used": False,
            "stress_refits_lambda": False, "interpretation": "BUDGET_CONTROL_NOT_EXACT_RISK_MATCH"}


def _nav(frame: pd.DataFrame) -> pd.DataFrame:
    if not {"date", "nav_cents", "status"}.issubset(frame.columns):
        raise ContractError("comparison NAV lacks dates, values or accounting status")
    out = frame.copy()
    out["date"] = out.date.astype(str)
    if out.date.duplicated().any():
        raise ContractError("comparison NAV has duplicate sessions")
    out["nav_cents"] = pd.to_numeric(out.nav_cents, errors="coerce")
    return out.sort_values("date").reset_index(drop=True)


def _complete(nav: pd.DataFrame) -> bool:
    return (not nav.empty and nav.status.eq("OK").all()
            and np.isfinite(nav.nav_cents).all() and nav.nav_cents.gt(0).all())


def paired_bootstrap(a: pd.DataFrame, b: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    a, b = _nav(a), _nav(b)
    if a.date.tolist() != b.date.tolist():
        raise ContractError("paired bootstrap requires exactly the same common sessions")
    evaluation = config["evaluation"]
    length, repetitions = evaluation["bootstrap_block_sessions"], evaluation["bootstrap_repetitions"]
    result = {"method": evaluation["bootstrap_method"], "seed": evaluation["bootstrap_seed"],
              "block_sessions": length, "repetitions": repetitions, "common_sessions": len(a),
              "start_date": a.date.iloc[0] if len(a) else None,
              "end_date": a.date.iloc[-1] if len(a) else None,
              "point_b_minus_a": None, "ci95_b_minus_a": None,
              "sampling_unit": "COMMON_SESSION_BLOCKS_WITH_PAIRED_INDICES",
              "first_session_return": "ZERO_INITIAL_SIGNAL_CLOSE",
              "interpretation": "HISTORICAL_STABILITY_NOT_FUTURE_RETURN_GUARANTEE"}
    if not (_complete(a) and _complete(b)):
        return {**result, "status": "ACCOUNTING_OR_DATA_INCONCLUSIVE"}
    if length <= 0 or repetitions <= 0:
        raise ContractError("bootstrap needs positive block length and repetition count")
    if len(a) < length:
        return {**result, "status": "INSUFFICIENT_COMMON_SESSIONS"}
    ra = a.nav_cents.pct_change(fill_method=None).fillna(0).to_numpy()
    rb = b.nav_cents.pct_change(fill_method=None).fillna(0).to_numpy()
    rng = np.random.default_rng(evaluation["bootstrap_seed"])
    differences = np.empty(repetitions)
    offsets = np.arange(length)
    for iteration in range(repetitions):
        starts = rng.integers(0, len(a) - length + 1, size=math.ceil(len(a) / length))
        indices = (starts[:, None] + offsets).reshape(-1)[:len(a)]
        differences[iteration] = np.prod(1 + rb[indices]) - np.prod(1 + ra[indices])
    return {**result, "status": "OK",
            "point_b_minus_a": float(b.nav_cents.iloc[-1] / b.nav_cents.iloc[0]
                                     - a.nav_cents.iloc[-1] / a.nav_cents.iloc[0]),
            "ci95_b_minus_a": np.quantile(differences, [.025, .975]).tolist()}


def test_subperiods(nav: pd.DataFrame, signal_dates: list[str], config: dict[str, Any], *,
                    strategy_id: str, cost_scenario: str) -> list[dict[str, Any]]:
    nav = _nav(nav)
    counts = config["split"]["test_reporting_blocks"]
    if len(signal_dates) != sum(counts) or not set(signal_dates).issubset(nav.date):
        raise ContractError("test subperiods require the full frozen test signal sequence")
    result, offset = [], 0
    for block, count in enumerate(counts, start=1):
        first = signal_dates[offset]
        last = signal_dates[offset + count - 1] if block < len(counts) else nav.date.iloc[-1]
        selected = nav.loc[nav.date.between(first, last)]
        before = nav.loc[nav.date.lt(first)]
        baseline = before.nav_cents.iloc[-1] if len(before) else selected.nav_cents.iloc[0]
        valid = _complete(selected) and finite(baseline) is not None and baseline > 0
        path = np.r_[baseline, selected.nav_cents.to_numpy()] if valid else None
        result.append(dict(strategy_id=strategy_id, cost_scenario=cost_scenario, block=block,
            signal_count=count, session_count=len(selected), start_date=first, end_date=last,
            starting_nav_cents=float(baseline) if finite(baseline) is not None else None,
            final_nav_cents=float(selected.nav_cents.iloc[-1]) if valid else None,
            net_return=float(path[-1] / path[0] - 1) if valid else None,
            max_drawdown=float(-(path / np.maximum.accumulate(path) - 1).min()) if valid else None,
            average_gross_exposure=float(selected.gross_exposure.mean()) if valid else None,
            average_cash_ratio=float(selected.cash_ratio.mean()) if valid else None,
            status="OK" if valid else "ACCOUNTING_OR_DATA_INCONCLUSIVE", account_continuous=True,
            includes_settlement_tail=block == len(counts)))
        offset += count
    return result


def comparison_verdict(summary: pd.DataFrame, paired: dict[str, Any], halves: pd.DataFrame,
                       config: dict[str, Any], scope_flags: Iterable[str]) -> dict[str, Any]:
    def row(strategy, cost):
        selected = summary.loc[summary.strategy_id.eq(strategy) & summary.split.eq("test")
                               & summary.cost_scenario.eq(cost)]
        if len(selected) != 1:
            raise ContractError("verdict requires one completed test cell per strategy/cost")
        return selected.iloc[0]

    evaluation = config["evaluation"]
    a, b, c = (row(strategy, "base") for strategy in STRATEGIES)
    sa, sb = (row(strategy, "stress") for strategy in STRATEGIES[:2])
    tolerance = evaluation["return_tie_tolerance"]
    valid = all(item.accounting_status == "OK" and int(item.unresolved_asset_count) == 0
                and finite(item.net_return) is not None and finite(item.max_drawdown) is not None
                for item in (a, b))
    enough = all(int(item.closed_trade_count) >= evaluation["min_closed_trades_for_winner"]
                 and int(item.traded_dates) >= evaluation["min_traded_dates_for_winner"] for item in (a, b))
    difference = float(b.net_return - a.net_return) if valid else None
    numerical_leader = "B" if valid and difference > tolerance else "A" if valid and difference < -tolerance else None
    if not valid:
        verdict = "ACCOUNTING_OR_DATA_INCONCLUSIVE"
    elif not enough:
        verdict = "INSUFFICIENT_TRADING_EVIDENCE"
    elif numerical_leader is None:
        verdict = "TIE"
    else:
        high, low = (b, a) if numerical_leader == "B" else (a, b)
        risk_ok = high.max_drawdown <= low.max_drawdown + evaluation["drawdown_tolerance_absolute"] + 1e-12
        verdict = f"{numerical_leader}_RETURN_LEADER_WITHIN_RISK_TOLERANCE" if risk_ok else "RETURN_RISK_TRADEOFF"
    ci = paired.get("ci95_b_minus_a") if paired.get("status") == "OK" else None
    ci_support = bool(ci and numerical_leader and (ci[0] > 0 if numerical_leader == "B" else ci[1] < 0))
    stress_difference = float(sb.net_return - sa.net_return) if all(finite(item.net_return) is not None for item in (sa, sb)) else None
    direction = 1 if numerical_leader == "B" else -1 if numerical_leader == "A" else 0
    stress_support = bool(direction and stress_difference is not None and direction * stress_difference > tolerance)
    half_support = bool(direction)
    for block in range(1, len(config["split"]["test_reporting_blocks"]) + 1):
        subset = halves.loc[halves.cost_scenario.eq("base") & halves.block.eq(block)]
        values = {item.strategy_id: finite(item.net_return) for item in subset.itertuples()}
        ha, hb = values.get(STRATEGIES[0]), values.get(STRATEGIES[1])
        half_support &= ha is not None and hb is not None and direction * (hb - ha) > tolerance
    flags = sorted(set(scope_flags))
    return {"verdict": verdict, "numerical_return_leader": numerical_leader,
            "b_minus_a_net_return": difference, "paired_ci_supports_leader": ci_support,
            "cost_stress_preserves_lead": stress_support, "test_halves_consistent": bool(half_support),
            "beats_exposure_control": bool(finite(b.net_return) is not None and finite(c.net_return) is not None
                                            and b.net_return - c.net_return > tolerance),
            "scope_limited": bool(flags), "scope_flags": flags,
            "accounting_valid": valid, "sample_sufficient": enough,
            "evidence_supports_historical_leader": bool(verdict.endswith("WITHIN_RISK_TOLERANCE")
                                                        and not flags and ci_support and stress_support),
            "generalization": "FIXED_HISTORICAL_INTERVAL_ONLY_NOT_UNIVERSAL_LIVE_SUPERIORITY",
            "prediction_claim": "IDENTICAL_RAW_F0_SCORES; POLICY_NOT_NEW_DIRECTIONAL_FACTORS"}
