from __future__ import annotations

import time
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from decimal import Decimal
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from core.backtest.execution_v2 import _money_cents
from core.backtest.prism_compare_engine import _bool, _csv_value, _decimal, _gzip_writer, _text
from core.backtest.rank_rotation_v2 import RotationBuy, _RotationReplay
from core.data.symbols import is_buyable_mainboard_ts_code, is_risk_warning_name
from core.strategies.prism_a_share import finite
from core.technical_v2.contracts import ContractError

FAMILIES = ("TREND_PULLBACK", "REBOUND", "LOW_FREQ_UP")


@dataclass(frozen=True)
class Candidate:
    family: str
    strength: int
    max_holding_sessions: int
    take_profit: float
    stop_loss: float = .03

    def __post_init__(self):
        if (self.family not in FAMILIES or self.strength not in {0, 1}
                or self.max_holding_sessions not in {3, 5, 10}
                or self.take_profit not in {.02, .04} or self.stop_loss != .03):
            raise ContractError("candidate is outside the frozen 36-rule grid")

    @property
    def candidate_id(self):
        return f"{self.family}_L{self.strength + 1}_H{self.max_holding_sessions}_T{int(self.take_profit * 100)}_S3"


def candidate_grid() -> list[Candidate]:
    return [Candidate(*values) for values in product(FAMILIES, (0, 1), (3, 5, 10), (.02, .04))]


def enrich_features(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.sort_values(["code", "date"]).copy()
    if out.duplicated(["code", "date"]).any():
        raise ContractError("search features contain duplicate code/date rows")
    grouped = out.groupby("code", sort=False)["comparison_close"]
    out["ret1"] = grouped.pct_change(1, fill_method=None)
    out["ret3"] = grouped.pct_change(3, fill_method=None)
    average = grouped.transform(lambda series: series.rolling(20, min_periods=20).mean())
    out["close_over_ma20"] = out.comparison_close / average
    return out


def entry_scores(frame: pd.DataFrame, family: str, strength: int) -> pd.Series:
    if family not in FAMILIES or strength not in {0, 1}:
        raise ContractError("unknown search entry rule")
    codes = frame["code"] if "code" in frame else pd.Series(frame.index, index=frame.index)
    eligible = (frame.real_bar.eq(True) & frame.roster_active.eq(True)
        & frame.instrument_type.eq("stock") & frame.listing_board.isin(["MAIN_SH", "MAIN_SZ"])
        & codes.map(is_buyable_mainboard_ts_code)
        & ~frame.name.fillna("").map(is_risk_warning_name)
        & ~frame.is_risk_warning.eq(True) & frame.is_suspended.eq(False)
        & frame.prediction_status.eq("OK") & np.isfinite(frame.score3)
        & frame.market_state.isin(["RANGE", "TREND_EXPANSION"])
        & frame.Q03.ge(50000000.) & frame.adj_factor.gt(0) & frame.valuation_close.gt(0))
    if family == "TREND_PULLBACK":
        eligible &= (frame.F03.gt(0) & frame.F02.ge(.25 if strength else 0.)
            & frame.close_over_ma20.between(.98, 1.01 if strength else 1.03)
            & frame.ret1.between(.005 if strength else .001, .03)
            & frame.ret3.le(.02) & frame.Q05.between(-.12, -.02))
        rank = frame.score3
    elif family == "REBOUND":
        eligible &= (frame.ret3.le(-.06 if strength else -.04)
            & frame.ret1.ge(.01 if strength else .005)
            & frame.Q05.between(-.20, -.08 if strength else -.05)
            & frame.comparison_close.gt(frame.comparison_open))
        rank = -frame.ret3 * 100.
    else:
        eligible &= (frame.forecast_class3.eq("up") & frame.score3.ge(75. if strength else 65.)
            & frame.overextended.eq(False) & frame.Q01.le(.03 if strength else .04))
        rank = frame.score3
    return rank.where(eligible & np.isfinite(rank))


def search_windows(sessions, holdout, *, window_count=3, window_sessions=63, minimum_context=120):
    if (sessions != sorted(set(sessions)) or not holdout
            or holdout != sorted(set(holdout)) or not set(holdout).issubset(sessions)):
        raise ContractError("invalid search/holdout calendar")
    first = sessions.index(holdout[0])
    if sessions[first:first + len(holdout)] != holdout:
        raise ContractError("holdout must be contiguous actual sessions")
    start = first - window_count * window_sessions
    if window_count < 2 or window_sessions < 13 or start < minimum_context:
        raise ContractError("insufficient pre-holdout history for the frozen search windows")
    windows = {f"selection_{i + 1}": sessions[start + i * window_sessions:start + (i + 1) * window_sessions]
               for i in range(window_count)}
    return {**windows, "holdout": list(holdout)}


def qualification(metrics, *, min_closed=30, min_dates=20, max_drawdown=.15):
    reasons = []
    if metrics.get("status") != "OK" or finite(metrics.get("net_return")) is None:
        reasons.append("INCOMPLETE_NAV")
    if metrics.get("closed_trade_count", 0) < min_closed:
        reasons.append("TOO_FEW_CLOSED_TRADES")
    if metrics.get("traded_date_count", 0) < min_dates:
        reasons.append("TOO_FEW_TRADED_DATES")
    if finite(metrics.get("net_return")) is None or metrics["net_return"] <= 0:
        reasons.append("NONPOSITIVE_NET_RETURN")
    drawdown = finite(metrics.get("max_drawdown"))
    if drawdown is None or drawdown > max_drawdown:
        reasons.append("DRAWDOWN_ABOVE_LIMIT")
    if metrics.get("open_position_count", 0):
        reasons.append("UNSETTLED_AFTER_TAIL")
    return reasons


def choose_candidate(summaries):
    if not summaries:
        raise ContractError("no searched candidates")
    def key(row):
        rate, value, drawdown = (finite(row.get(field)) for field in ("win_rate", "net_return", "max_drawdown"))
        return (-(rate if rate is not None else -1.), -(value if value is not None else -1.),
                drawdown if drawdown is not None else 1., row["candidate_id"])
    ranked = sorted(summaries, key=key)
    eligible = [row for row in ranked if row["qualified"]]
    return {"chosen_candidate_id": eligible[0]["candidate_id"] if eligible else None,
            "diagnostic_candidate_id": ranked[0]["candidate_id"],
            "qualified_candidate_count": len(eligible), "ranking": [row["candidate_id"] for row in ranked]}


class SearchMarket:
    def __init__(self, frames: Mapping[str, pd.DataFrame]):
        self.frames = {}
        for date, frame in frames.items():
            if frame.code.duplicated().any() or not frame.date.astype(str).eq(date).all():
                raise ContractError("search day has duplicate codes or mismatched dates")
            self.frames[date] = frame.set_index("code", drop=False)
        self.signals = {}

    def ranks(self, date, candidate):
        key = (date, candidate.family, candidate.strength)
        if key not in self.signals:
            scores = entry_scores(self.frames[date], candidate.family, candidate.strength).dropna()
            ranked = pd.DataFrame({"rank_score": scores, "code_sort": scores.index})
            self.signals[key] = ranked.sort_values(["rank_score", "code_sort"], ascending=[False, True]).rank_score
        return self.signals[key]


class _SearchReplay(_RotationReplay):
    def __init__(self, *args, candidate, tail_sessions, **kwargs):
        super().__init__(*args, **kwargs)
        self.candidate, self.tail_sessions = candidate, tail_sessions
        self.date_index = {date: index for index, date in enumerate(self.sessions)}

    def _entry_reason(self):
        return "SEARCH_" + self.candidate.family

    def _buy_budget(self, code):
        adv = _decimal(self.previous_rows[code].get("Q03"))
        cap = _money_cents(adv * Decimal("0.01")) if adv and adv > 0 else 0
        return min(super()._buy_budget(code), cap)

    def _fill(self, order, date, row):
        result = super()._fill(order, date, row)
        if order.side == "BUY" and result.filled_quantity and result.status != "ALREADY_FILLED":
            factor = _decimal(row.get("adj_factor"))
            if factor is None or factor <= 0:
                raise ContractError("filled search entry has no comparison price basis")
            self._metadata(order, search_entry_price_basis=str(result.fill_price * factor))
        return result

    def _metric_fields(self, nav, fills, closed, remaining):
        return {"strategy_id": self.candidate.candidate_id, "candidate": asdict(self.candidate),
            "prediction_method": "FIXED_TECHNICAL_ENTRY_RULE", "fixed_holding_expiry": True,
            "winning_closed_trade_count": int(closed.realized_pnl_cents.gt(0).sum()),
            "traded_date_count": int(fills.trade_date.nunique()),
            "unrealized_pnl_cny": (int(nav.iloc[-1].position_value_cents) - int(remaining.cost_cents.sum())) / 100,
            "take_profit_exit_count": int(closed.exit_reason.eq("SEARCH_TAKE_PROFIT").sum()),
            "stop_loss_exit_count": int(closed.exit_reason.eq("SEARCH_STOP_LOSS").sum()),
            "expiry_exit_count": int(closed.exit_reason.eq("SEARCH_MAX_HOLDING").sum()),
            "max_observed_holding_sessions": int(closed.holding_sessions.max()) if len(closed) else None,
            "new_entry_tail_sessions": self.tail_sessions, "max_adv_participation": .01,
            "win_definition": "POSITIVE_REALIZED_PNL_AFTER_BUY_AND_SELL_FEES_WITH_SLIPPAGE_IN_FILL_PRICES"}

    def search_day(self, market, date, next_date, writer):
        frame, ranks = market.frames[date], market.ranks(date, self.candidate)
        relevant = self._held() | {buy.code for buy in self.pending_buys} | set(ranks.iloc[:6].index)
        compact = frame.loc[frame.index.isin(relevant)]
        rows = {str(row["code"]): row for row in compact.to_dict("records")}
        self._execute(date, rows)
        mark = self._mark(date, rows)
        if next_date is None:
            return
        held, sells = self._held(), dict(self.exit_reasons)
        for code in held:
            row = rows.get(code, {})
            if not is_buyable_mainboard_ts_code(code) or is_risk_warning_name(_text(row.get("name"))) or _bool(row.get("is_risk_warning")) is True:
                sells[code] = "RISK_WARNING_SECURITY"
            for lot in self.portfolio.lots():
                if lot.code != code or lot.status != "OPEN":
                    continue
                price, basis = _decimal(row.get("comparison_close")), _decimal(lot.metadata.get("search_entry_price_basis"))
                change = price / basis - 1 if price is not None and basis and basis > 0 else None
                if change is not None and change <= -Decimal(str(self.candidate.stop_loss)):
                    sells.setdefault(code, "SEARCH_STOP_LOSS")
                elif change is not None and change >= Decimal(str(self.candidate.take_profit)):
                    sells.setdefault(code, "SEARCH_TAKE_PROFIT")
                elif self.date_index[next_date] - self.date_index[lot.entry_date] >= self.candidate.max_holding_sessions:
                    sells.setdefault(code, "SEARCH_MAX_HOLDING")
                elif self.candidate.family == "LOW_FREQ_UP" and row.get("forecast_class3") == "down" and row.get("prediction_status") == "OK":
                    sells.setdefault(code, "FORECAST_DOWN")
        self._submit_exits(date, next_date, rows, sells)
        capacity = self.max_names - len(held - set(sells))
        buys = []
        if mark.nav_cents is not None and self.date_index[date] < len(self.sessions) - self.tail_sessions - 1:
            for code in ranks.index:
                if code not in held and code in rows and not self._entry_blockers(code, rows[code], date):
                    buys.append(RotationBuy(code))
                    if len(buys) >= capacity:
                        break
            if capacity <= 0:
                buys = []
        self.pending_buys = tuple(buys)
        self.pending_budget = int(mark.nav_cents or 0) // self.max_names
        self.previous_rows = rows
        for code in sorted(held | {buy.code for buy in buys}):
            row = rows.get(code, {})
            writer.writerow({key: _csv_value(value) for key, value in dict(date=date, code=code,
                score=finite(row.get("score3")), forecast_class=_text(row.get("forecast_class3")),
                rank_score=finite(ranks.get(code)), action="SELL" if code in sells else "HOLD" if code in held else "BUY",
                reasons=[sells[code]] if code in sells else []).items()})


def replay_search(frames, *, sessions, candidate, config, cost_scenario, output_dir,
                  run_id, split="selection", scope_flags=(), corporate_actions=None, tail_sessions=11):
    if (sessions != sorted(set(sessions)) or len(sessions) <= tail_sessions + 1
            or tail_sessions < candidate.max_holding_sessions + 1
            or cost_scenario not in config["costs"]["slippage_cases"]):
        raise ContractError("invalid bounded search replay calendar or policy")
    output = Path(output_dir).resolve()
    if output.exists() and any(output.iterdir()):
        raise ContractError("search results require a fresh cell directory")
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    replay = _SearchReplay(config, sessions, cost_scenario, output, run_id + ":" + candidate.candidate_id,
        split, scope_flags, corporate_actions, candidate=candidate, tail_sessions=tail_sessions)
    market = frames if isinstance(frames, SearchMarket) else SearchMarket(frames)
    if not set(sessions).issubset(market.frames):
        raise ContractError("search replay omitted actual sessions")
    with ExitStack() as stack:
        writer = _gzip_writer(stack, output / "decisions.csv.gz",
                             ["date", "code", "score", "forecast_class", "rank_score", "action", "reasons"])
        for index, date in enumerate(sessions):
            replay.search_day(market, date, sessions[index + 1] if index + 1 < len(sessions) else None, writer)
    return replay.finish(started)
