from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from contextlib import ExitStack, closing
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from core.backtest.execution_v2 import (
    PaperOrder, _affordable_buy, _money_cents, _round_to_tick,
    execute_open_orders, persist_orders,
)
from core.backtest.metrics_v2 import calculate_portfolio_metrics
from core.backtest.portfolio_v2 import PaperPortfolio, apply_corporate_actions, mark_portfolio, pending_share_quantities
from core.backtest.prism_compare_engine import _bool, _csv_value, _decimal, _export_csv, _gzip_writer, _open, _text, fee_schedule
from core.backtest.prism_compare_rules import security_rule
from core.data.symbols import is_buyable_mainboard_ts_code, is_risk_warning_name
from core.data.v2_store import V2Store
from core.pipeline.prism_compare_config import write_json
from core.strategies.prism_a_share import finite
from core.technical_v2.contracts import ContractError, canonical_json


@dataclass(frozen=True)
class RotationBuy:
    code: str
    replaces: str | None = None


@dataclass(frozen=True)
class RotationPlan:
    sells: dict[str, str]
    buys: tuple[RotationBuy, ...]


def plan_rotation(
    rows: Mapping[str, Mapping[str, Any]],
    held_codes: Iterable[str],
    *,
    horizon: int = 3,
    max_names: int = 3,
    pending_exits: Mapping[str, str] | None = None,
) -> RotationPlan:
    held = set(held_codes)
    if horizon not in {1, 3, 5} or max_names != 3 or len(held) > max_names:
        raise ContractError("rotation requires a valid horizon and at most three holdings")
    score_key, class_key = f"score{horizon}", f"forecast_class{horizon}"

    def score(code):
        row = rows.get(code, {})
        return finite(row.get(score_key)) if row.get("prediction_status") == "OK" else None

    sells = {code: reason for code, reason in (pending_exits or {}).items() if code in held}
    for code in sorted(held):
        row = rows.get(code, {})
        if row.get("forced_exit_reason"):
            sells[code] = str(row["forced_exit_reason"])
        elif row.get("prediction_status") == "OK" and row.get(class_key) == "down":
            sells[code] = "FORECAST_DOWN"
    survivors = held - set(sells)
    candidates = sorted(
        (code for code, row in rows.items()
         if code not in held and row.get("entry_eligible") is True
         and row.get(class_key) == "up" and score(code) is not None),
        key=lambda code: (-score(code), code),
    )
    buys = []
    for code in candidates:
        if len(survivors) < max_names:
            buys.append(RotationBuy(code))
            survivors.add(code)
            continue
        comparable = [other for other in survivors & held if score(other) is not None]
        if not comparable:
            break
        weakest = min(comparable, key=lambda other: (score(other), other))
        if score(code) <= score(weakest):
            break
        sells[weakest] = "HIGHER_SCORE_ROTATION"
        survivors.remove(weakest)
        survivors.add(code)
        buys.append(RotationBuy(code, weakest))
    return RotationPlan(sells, tuple(buys))


class _RotationReplay:
    def __init__(self, config, sessions, cost, output, run_id, split, scope_flags, actions):
        self.config, self.sessions, self.cost, self.output = config, sessions, cost, output
        self.policy = config["rank_rotation"]
        self.horizon, self.max_names = self.policy["horizon"], self.policy["max_names"]
        self.split = split
        self.account_id = f"{run_id}:rank-rotation:{split}:{cost}"
        self.store = V2Store(output / "account.db", data_mode="test")
        self.store.migrate()
        self.portfolio = PaperPortfolio(self.store, self.account_id)
        self.initial = _money_cents(Decimal(str(config["shared_portfolio"]["initial_capital_cny"])))
        self.portfolio.open_account(method="formula", initial_cash_cents=self.initial,
                                    initial_event_at=f"{sessions[0]}T00:00:00+08:00")
        self.fees = fee_schedule(config, cost, sessions[0])
        self.scope_flags = list(scope_flags)
        if self.policy["risk_warning_unknown"] == "ALLOW_RESEARCH_ONLY":
            self.scope_flags.append("HISTORICAL_ST_UNKNOWN_ALLOWED_FOR_RESEARCH")
        self.scope_flags = sorted(set(self.scope_flags))
        self.actions = [] if actions is None else actions.to_dict("records")
        self.pending_exits: dict[str, PaperOrder] = {}
        self.exit_reasons: dict[str, str] = {}
        self.pending_buys = ()
        self.pending_budget = 0
        self.previous_rows = {}
        self.order_metadata = {}
        self.last_marks = {}
        self.mark_status = {}
        self.slippage = {}
        self.nav_rows, self.position_rows, self.attempts = [], [], []
        self.peak_names = 0

    def _held(self):
        lots = self.portfolio.lots()
        return {lot.code for lot in lots if lot.status == "OPEN" and lot.quantity > 0} | set(pending_share_quantities(lots))

    def _check_account(self):
        count = len(self._held())
        self.peak_names = max(self.peak_names, count)
        if count > self.max_names or self.portfolio.cash_cents() < 0:
            raise ContractError("rotation breached the three-stock or nonnegative-cash constraint")

    def _metadata(self, order, **updates):
        with self.store._write_connection() as conn:
            rows = conn.execute("SELECT lot_id,metadata_json FROM paper_lots WHERE account_id=? AND code=?",
                                (self.account_id, order.code)).fetchall()
            for row in rows:
                metadata = json.loads(row["metadata_json"])
                if order.side == "SELL" and row["lot_id"] not in dict(order.lot_quantities):
                    continue
                if order.side == "BUY" and metadata.get("source_order_id") != order.order_id:
                    continue
                metadata.update(updates)
                conn.execute("UPDATE paper_lots SET metadata_json=? WHERE lot_id=?",
                             (canonical_json(metadata), row["lot_id"]))

    def _fill(self, order, date, row):
        market = _open(order.code, date, row)
        rule = security_rule(row.get("listing_board"), date, self.config)
        result = execute_open_orders(self.store, [order], {order.code: market},
                                    fee_schedule=self.fees, security_rules={order.code: rule} if rule else {}).results[0]
        self.attempts.append(dict(date=date, code=order.code, side=order.side, order_id=order.order_id,
                                  status=result.status, reason="|".join(result.reason_codes)))
        if result.filled_quantity and result.status != "ALREADY_FILLED":
            self.slippage[order.order_id] = float(abs(result.fill_price - market.raw_open) * result.filled_quantity)
            metadata = self.order_metadata[order.order_id]
            if order.side == "BUY":
                self._metadata(order, entry_signal_date=metadata["signal_date"],
                               entry_score=metadata["signal_score"], entry_sector_id=order.sector_id)
            else:
                self._metadata(order, exit_reason=metadata["intent_reason"])
        self._check_account()
        return result

    def _buy_budget(self, code):
        return min(self.pending_budget, self.portfolio.cash_cents())

    def _entry_reason(self):
        return "UP_RANK_ENTRY"

    def _metric_fields(self, nav, fills, closed, remaining):
        return {}

    def _execute(self, date, rows):
        prior_actions = [event for event in self.actions if _text(event.get("record_date")) < date]
        if prior_actions:
            apply_corporate_actions(self.store, self.account_id, prior_actions, as_of_date=date)
        exited_today = set()
        for code, order in sorted(list(self.pending_exits.items())):
            exited_today.add(code)
            result = self._fill(order, date, rows.get(code, {}))
            if not result.status.startswith("PENDING"):
                self.pending_exits.pop(code, None)
            if code not in self._held():
                self.exit_reasons.pop(code, None)
        for buy in self.pending_buys:
            code, row = buy.code, rows.get(buy.code, {})
            held = self._held()
            reason = None
            if buy.replaces is not None and buy.replaces in held:
                reason = "REPLACEMENT_EXIT_NOT_FILLED"
            elif len(held) >= self.max_names:
                reason = "ACTUAL_POSITION_CAPACITY_FULL"
            elif code in held or code in exited_today:
                reason = "SAME_SESSION_EXIT_OR_EXISTING_HOLDING"
            elif not is_buyable_mainboard_ts_code(code):
                reason = "OUTSIDE_TRADING_UNIVERSE"
            elif _bool(row.get("is_risk_warning")) is True or is_risk_warning_name(_text(row.get("name"))):
                reason = "RISK_WARNING_SECURITY"
            elif (_bool(row.get("is_risk_warning")) is None
                    and self.policy["risk_warning_unknown"] == "BLOCK_NEW_ENTRIES"):
                reason = "HISTORICAL_RISK_WARNING_UNKNOWN"
            rule = security_rule(row.get("listing_board"), date, self.config)
            opening = _decimal(row.get("execution_open"))
            if reason is None and (rule is None or opening is None or opening <= 0):
                reason = "OPEN_OR_RULE_UNAVAILABLE"
            if reason:
                self.attempts.append(dict(date=date, code=code, side="BUY", order_id=None, status="BLOCKED", reason=reason))
                continue
            # Size at the opening quote, using only the previous close NAV and actual cash after sales.
            price = _round_to_tick(opening * (1 + self.fees.slippage_each_side), rule.tick_size, buy=True)
            budget = self._buy_budget(code)
            requested = rule.floor_buy_quantity(Decimal(budget) / 100 / price)
            quantity, _, _, _ = _affordable_buy(requested, rule.lot_size, price, budget, self.fees, rule)
            if quantity <= 0:
                self.attempts.append(dict(date=date, code=code, side="BUY", order_id=None,
                                          status="BLOCKED", reason="INSUFFICIENT_CASH_OR_LOT"))
                continue
            prior = self.previous_rows[code]
            identity = f"{self.account_id}|BUY|{code}|{date}"
            order = PaperOrder(hashlib.sha256(identity.encode()).hexdigest(), self.account_id,
                               self.account_id, code, "BUY", quantity, date,
                               _decimal(prior.get("valuation_close")), None,
                               sector_id=_text(prior.get("sector_id"), "UNKNOWN"),
                               reason_codes=("HIGHER_SCORE_ROTATION" if buy.replaces else self._entry_reason(),))
            self.order_metadata[order.order_id] = dict(signal_date=prior["date"],
                signal_score=finite(prior.get(f"score{self.horizon}")),
                intent_reason=order.reason_codes[0], replaces=buy.replaces)
            self._fill(order, date, row)
        self.pending_buys = ()

    def _mark(self, date, rows):
        for code, row in rows.items():
            price = _decimal(row.get("valuation_close"))
            if price is not None and price > 0:
                self.last_marks[code] = price
        if self.actions:
            apply_corporate_actions(self.store, self.account_id, self.actions, as_of_date=date)
        prices = {}
        for code in self._held():
            row = rows.get(code, {})
            delist = _text(row.get("delist_date"))
            if delist and delist <= date:
                self.mark_status[code] = "TERMINAL_UNRESOLVED"
                prices[code] = {"price": None}
            elif code in self.last_marks:
                carried = finite(row.get("valuation_close")) is None
                self.mark_status[code] = "MARK_ONLY" if carried else "REAL_CLOSE"
                prices[code] = {"price": self.last_marks[code], "mark_only": carried}
            else:
                self.mark_status[code] = "VALUATION_UNAVAILABLE"
                prices[code] = {"price": None}
        mark = mark_portfolio(self.store, self.account_id, date, prices)
        self._check_account()
        self.nav_rows.append(dict(date=date, nav_cents=mark.nav_cents, cash_cents=mark.cash_cents,
            position_value_cents=mark.position_value_cents, status=mark.status,
            position_count=len(self._held()), held_codes=sorted(self._held()),
            sector_exposure=mark.sector_exposure, mark_only_codes=sorted(code for code in self._held()
                if self.mark_status.get(code) == "MARK_ONLY"), unresolved_codes=mark.unresolved_codes))
        for lot in self.portfolio.lots():
            if lot.status == "OPEN":
                self.position_rows.append(dict(date=date, code=lot.code, quantity=lot.quantity,
                    entry_date=lot.entry_date, score=finite(rows.get(lot.code, {}).get(f"score{self.horizon}")),
                    forecast_class=_text(rows.get(lot.code, {}).get(f"forecast_class{self.horizon}"), "unknown"),
                    valuation_status=self.mark_status.get(lot.code)))
        return mark

    def _entry_blockers(self, code, row, date):
        reasons = []
        if not is_buyable_mainboard_ts_code(code) or row.get("listing_board") not in {"MAIN_SH", "MAIN_SZ"}:
            reasons.append("OUTSIDE_TRADING_UNIVERSE")
        risk = _bool(row.get("is_risk_warning"))
        if risk is True or is_risk_warning_name(_text(row.get("name"))):
            reasons.append("RISK_WARNING_SECURITY")
        elif risk is None and self.policy["risk_warning_unknown"] == "BLOCK_NEW_ENTRIES":
            reasons.append("HISTORICAL_RISK_WARNING_UNKNOWN")
        if _bool(row.get("real_bar")) is not True or finite(row.get("valuation_close")) is None:
            reasons.append("SIGNAL_BAR_UNAVAILABLE")
        if row.get("prediction_status") != "OK" or finite(row.get(f"score{self.horizon}")) is None:
            reasons.append("PREDICTION_UNAVAILABLE")
        if _bool(row.get("is_suspended")) is not False:
            reasons.append("SUSPENDED_OR_UNKNOWN_AT_SIGNAL")
        listed, delist = _text(row.get("list_date")), _text(row.get("delist_date"))
        if (_bool(row.get("roster_active")) is not True or not listed or listed > date
                or (delist and delist <= date) or _text(row.get("instrument_type")).lower() != "stock"):
            reasons.append("INSTRUMENT_NOT_ACTIVE_OR_UNKNOWN")
        if finite(row.get("adj_factor")) is None or float(row["adj_factor"]) <= 0:
            reasons.append("ADJUSTMENT_UNAVAILABLE")
        return reasons

    def _submit_exits(self, date, next_date, rows, sells):
        for code, reason in sorted(sells.items()):
            self.exit_reasons[code] = reason
            lots = [lot for lot in self.portfolio.lots() if lot.status == "OPEN" and lot.code == code]
            caps = tuple(sorted((lot.lot_id, lot.quantity) for lot in lots if lot.quantity > 0))
            if not caps:
                continue
            old = self.pending_exits.get(code)
            if old is not None and old.lot_quantities == caps:
                self.order_metadata[old.order_id]["intent_reason"] = reason
                continue
            if old is not None:
                with self.store._write_connection() as conn:
                    conn.execute("UPDATE paper_orders SET status='SUPERSEDED_PENDING_EXIT' WHERE order_id=?", (old.order_id,))
            identity = canonical_json([self.account_id, code, next_date, caps])
            order = PaperOrder(hashlib.sha256(identity.encode()).hexdigest(), self.account_id,
                self.account_id, code, "SELL", sum(quantity for _, quantity in caps), next_date,
                self.last_marks.get(code, Decimal(0)), None, sector_id=_text(rows.get(code, {}).get("sector_id")),
                reason_codes=(reason,), lot_quantities=caps)
            self.order_metadata[order.order_id] = dict(signal_date=date,
                signal_score=finite(rows.get(code, {}).get(f"score{self.horizon}")), intent_reason=reason, replaces=None)
            persist_orders(self.store, [order])
            self.pending_exits[code] = order

    def day(self, date, frame, next_date, writer):
        if frame.code.duplicated().any() or not frame.date.astype(str).eq(date).all():
            raise ContractError("rotation daily rows have duplicate codes or mismatched dates")
        rows = {str(row["code"]): row for row in frame.to_dict("records")}
        self._execute(date, rows)
        mark = self._mark(date, rows)
        if next_date is None:
            return
        held, blockers = self._held(), {}
        for code, row in rows.items():
            blockers[code] = self._entry_blockers(code, row, date)
            row["entry_eligible"] = not blockers[code]
            if code in held:
                if "OUTSIDE_TRADING_UNIVERSE" in blockers[code] or "RISK_WARNING_SECURITY" in blockers[code]:
                    row["forced_exit_reason"] = blockers[code][0]
        plan = plan_rotation(rows, held, horizon=self.horizon, max_names=self.max_names,
                             pending_exits=self.exit_reasons)
        self._submit_exits(date, next_date, rows, plan.sells)
        self.pending_buys = plan.buys if mark.nav_cents is not None else ()
        self.pending_budget = int(mark.nav_cents or 0) // self.max_names
        self.previous_rows = rows
        selected = {buy.code: buy.replaces for buy in self.pending_buys}
        for code in sorted(set(rows) | held):
            row = rows.get(code, {})
            forecast = _text(row.get(f"forecast_class{self.horizon}"), "unknown")
            if code not in held and forecast != "up":
                continue
            action = "SELL" if code in plan.sells else "BUY" if code in selected else "HOLD" if code in held else "SKIP"
            reasons = [plan.sells[code]] if action == "SELL" else blockers.get(code, [])
            if action == "SKIP" and not reasons:
                reasons = ["NOT_HIGHER_THAN_HELD_OR_CAPACITY"]
            writer.writerow({key: _csv_value(value) for key, value in dict(date=date, code=code,
                score=finite(row.get(f"score{self.horizon}")), forecast_class=forecast, action=action,
                entry_eligible=bool(row.get("entry_eligible")), replaces=selected.get(code), reasons=reasons).items()})

    def finish(self, started):
        nav = pd.DataFrame(self.nav_rows)
        fills = self.store.read_paper_fills(self.account_id)
        sequence = {row["order_id"]: index for index, row in enumerate(self.attempts)
                    if row["status"] in {"FILLED", "PARTIAL_FILLED"}}
        fills["execution_sequence"] = fills.order_id.map(sequence)
        fills = fills.sort_values("execution_sequence").reset_index(drop=True)
        fills["modeled_slippage_cny"] = [self.slippage.get(key, 0.) for key in fills.order_id]
        with closing(self.store._connect()) as conn:
            orders = pd.read_sql_query("SELECT * FROM paper_orders WHERE account_id=? ORDER BY rowid", conn, params=[self.account_id])
        for field in ("signal_date", "signal_score", "intent_reason", "replaces"):
            orders[field] = [self.order_metadata.get(key, {}).get(field) for key in orders.order_id]
        closed = self.portfolio.closed_trades()
        index = {date: position for position, date in enumerate(self.sessions)}
        closed["holding_sessions"] = [index[end] - index[start] for start, end in zip(closed.entry_date, closed.closed_at)]
        remaining_rows = []
        for lot in self.portfolio.lots():
            if lot.status == "OPEN":
                remaining_rows.append(dict(code=lot.code, entry_date=lot.entry_date, quantity=lot.quantity,
                    cost_cents=lot.cost_cents, valuation_status=self.mark_status.get(lot.code), asset_type="LISTED_SHARES"))
            for entitlement in lot.metadata.get("corporate_action_entitlements", {}).values():
                if not entitlement["shares_listed"] and int(entitlement["share_quantity"]) > 0:
                    remaining_rows.append(dict(code=lot.code, entry_date=lot.entry_date,
                        quantity=int(entitlement["share_quantity"]), cost_cents=0,
                        valuation_status=self.mark_status.get(lot.code), asset_type="CORPORATE_SHARE_RECEIVABLE"))
        remaining = pd.DataFrame(remaining_rows,
            columns=["code", "entry_date", "quantity", "cost_cents", "valuation_status", "asset_type"])
        native = calculate_portfolio_metrics(nav, fills=fills, orders=orders, closed_trades=closed)
        metrics = {**asdict(native), "strategy_id": f"R0_RANK_ROTATION_{self.horizon}D",
            "split": self.split, "cost_scenario": self.cost, "start_date": self.sessions[0], "end_date": self.sessions[-1],
            "initial_nav": self.initial / 100, "final_nav": float(nav.iloc[-1].nav_cents) / 100 if pd.notna(nav.iloc[-1].nav_cents) else None,
            "filled_buy_count": int(fills.side.eq("BUY").sum()), "filled_sell_count": int(fills.side.eq("SELL").sum()),
            "closed_trade_count": len(closed), "peak_position_count": self.peak_names,
            "open_position_count": len(self._held()), "fees_total": float(fills.fee_cents.sum()) / 100,
            "modeled_slippage_total": float(fills.modeled_slippage_cny.sum()),
            "average_holding_sessions": float(closed.holding_sessions.mean()) if len(closed) else None,
            "rotation_exit_count": int(closed.exit_reason.eq("HIGHER_SCORE_ROTATION").sum()),
            "down_exit_count": int(closed.exit_reason.eq("FORECAST_DOWN").sum()),
            "execution_blockers": dict(Counter(row["reason"] for row in self.attempts if row["status"] not in {"FILLED", "PARTIAL_FILLED"})),
            "scope_flags": self.scope_flags, "result_status": "RESEARCH_SCOPE_LIMITED",
            "prediction_method": "formula", "prediction_horizon": self.horizon,
            "fixed_holding_expiry": False, "end_liquidation": False,
            "execution_approximation": "NEXT_OPEN_QUOTE_SIZING_AND_OPEN_AUCTION_FILL_APPROXIMATION"}
        metrics.update(self._metric_fields(nav, fills, closed, remaining))
        attempts = pd.DataFrame(self.attempts, columns=["date", "code", "side", "order_id", "status", "reason"])
        positions = pd.DataFrame(self.position_rows, columns=["date", "code", "quantity", "entry_date", "score", "forecast_class", "valuation_status"])
        for name, frame in (("daily_nav", nav), ("fills", fills), ("orders", orders), ("closed_trades", closed),
                            ("daily_positions", positions), ("execution_attempts", attempts)):
            _export_csv(self.output / f"{name}.csv.gz", frame)
        _export_csv(self.output / "remaining_positions.csv", remaining)
        write_json(self.output / "metrics.json", metrics, immutable=True)
        runtime = {"status": "COMPLETE", "elapsed_seconds": time.perf_counter() - started,
                   "session_count": len(nav), "api_calls": 0}
        write_json(self.output / "runtime.json", runtime, immutable=True)
        return {"metrics": metrics, "runtime": runtime}


def replay_rotation(frames: Iterable[tuple[str, pd.DataFrame]], *, sessions: list[str],
                    config: dict[str, Any], cost_scenario: str, output_dir: str | Path,
                    run_id: str, split: str = "test", scope_flags: Iterable[str] = (),
                    corporate_actions: pd.DataFrame | None = None) -> dict[str, Any]:
    policy = config["rank_rotation"]
    if (sessions != sorted(set(sessions)) or len(sessions) < 2
            or policy["horizon"] not in {1, 3, 5} or policy["max_names"] != 3
            or policy["risk_warning_unknown"] not in {"BLOCK_NEW_ENTRIES", "ALLOW_RESEARCH_ONLY"}
            or cost_scenario not in config["costs"]["slippage_cases"]):
        raise ContractError("invalid rotation calendar or research policy")
    output = Path(output_dir).resolve()
    if output.exists() and any(output.iterdir()):
        raise ContractError("rotation results must use a fresh output directory")
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    replay = _RotationReplay(config, sessions, cost_scenario, output, run_id, split, scope_flags, corporate_actions)
    seen = []
    with ExitStack() as stack:
        writer = _gzip_writer(stack, output / "decisions.csv.gz",
                             ["date", "code", "score", "forecast_class", "action", "entry_eligible", "replaces", "reasons"])
        for date, frame in frames:
            position = len(seen)
            if position >= len(sessions) or date != sessions[position]:
                raise ContractError("rotation frame coverage differs from the actual-session calendar")
            next_date = sessions[position + 1] if position + 1 < len(sessions) else None
            replay.day(date, frame, next_date, writer)
            seen.append(date)
    if seen != sessions:
        raise ContractError("rotation replay omitted actual sessions")
    return replay.finish(started)
