from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import math
import resource
import time
from collections import Counter, defaultdict
from contextlib import ExitStack, closing
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from core.backtest.execution_v2 import (
    FeeSchedule, MarketOpen, PaperOrder, _affordable_buy, _money_cents,
    _round_to_tick, execute_open_orders, persist_orders,
)
from core.backtest.metrics_v2 import calculate_portfolio_metrics
from core.backtest.portfolio_v2 import PaperPortfolio, apply_corporate_actions, mark_portfolio, pending_share_quantities
from core.backtest.prism_compare_rules import security_rule
from core.data.v2_store import V2Store
from core.pipeline.prism_compare_config import STRATEGIES, file_sha256, implementation_identity, reference_cost, write_json
from core.strategies.formula_v2 import ReturnBinModel, ReturnBinStats, estimate_formula_return
from core.strategies.intent_v2 import derive_research_intent
from core.strategies.prism_a_share import adaptive_distance, entry_allowed, finite, update_stop
from core.technical_v2.contracts import ContractError, canonical_json, sha256_json

DECISION_COLUMNS = (
    "date", "code", "strategy_id", "split", "cost_scenario", "signal_enabled",
    "current_quantity", "sellable_quantity", "score1", "score3", "score5",
    "prediction_status", "market_state", "state_multiplier", "state_reason", "width_fraction", "width_reasons",
    "expected_gross_return", "reference_net_edge", "quantity_cost", "quantity_net_edge",
    "research_intent", "account_action", "entry_candidate", "allocated_quantity", "order_id",
    "primary_blocker", "all_blockers", "exit_reasons", "gate_roster", "gate_real_bar",
    "gate_factors", "gate_score65_55", "gate_not_extended", "gate_return_estimate",
    "gate_net_edge", "gate_trade_eligibility", "gate_state_cooldown", "gate_quantity", "gate_order",
)
GATES = ("roster", "real_bar", "factors", "score65_55", "not_extended", "return_estimate",
         "net_edge", "trade_eligibility", "state_cooldown", "quantity", "order")


def _decimal(value: Any) -> Decimal | None:
    number = finite(value)
    return Decimal(str(number)) if number is not None else None


def _bool(value: Any) -> bool | None:
    if value is None or pd.isna(value):
        return None
    return bool(value)


def _text(value: Any, default: str = "") -> str:
    return default if value is None or pd.isna(value) else str(value)


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, tuple, dict)):
        return canonical_json(value)
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return ""
    return value


def _gzip_writer(stack: ExitStack, path: Path, columns: Iterable[str]) -> csv.DictWriter:
    raw = stack.enter_context(path.open("wb"))
    compressed = stack.enter_context(gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0))
    handle = stack.enter_context(io.TextIOWrapper(compressed, encoding="utf-8", newline=""))
    writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    return writer


def _export_csv(path: Path, frame: pd.DataFrame) -> None:
    if path.suffix == ".gz":
        with ExitStack() as stack:
            writer = _gzip_writer(stack, path, frame.columns)
            writer.writerows({key: _csv_value(value) for key, value in row.items()}
                             for row in frame.to_dict("records"))
    else:
        frame.to_csv(path, index=False)


def fee_schedule(config: dict[str, Any], scenario: str, first_date: str) -> FeeSchedule:
    costs = config["costs"]
    return FeeSchedule(
        first_date, None, Decimal(str(costs["commission_each_side"])),
        Decimal(str(costs["minimum_commission_cny"])), Decimal(str(costs["sell_addon_rate"])),
        Decimal(str(costs["other_rate_each_side"])), Decimal(str(costs["slippage_cases"][scenario])),
        costs["basis"],
    )


def quantity_cost(reference: Decimal, quantity: int, fees: FeeSchedule, rule: Any) -> float:
    buy = _round_to_tick(reference * (1 + fees.slippage_each_side), rule.tick_size, buy=True)
    sell = _round_to_tick(reference * (1 - fees.slippage_each_side), rule.tick_size, buy=False)
    buy_fee, _ = fees.fees(buy * quantity, "BUY")
    sell_fee, _ = fees.fees(sell * quantity, "SELL")
    return float((buy - sell) / reference + Decimal(buy_fee + sell_fee) / (reference * quantity * 100))


def _open(code: str, date: str, row: dict[str, Any]) -> MarketOpen:
    up, down = _decimal(row.get("up_limit")), _decimal(row.get("down_limit"))
    no_limit = _bool(row.get("no_price_limit"))
    if no_limit is None and up is not None and down is not None and up > down > 0:
        no_limit = False  # Observed exchange bounds, not an invented limit percentage.
    return MarketOpen(code, date, _decimal(row.get("execution_open")), up, down,
                      no_limit, _bool(row.get("is_suspended")))


class _Replay:
    def __init__(self, *, config, strategy_id, split, cost_scenario, model, lambda_c,
                 output_dir, run_id, sessions, signal_dates, scope_flags, corporate_actions):
        self.config, self.strategy, self.split, self.cost = config, strategy_id, split, cost_scenario
        self.model, self.lambda_c = model, lambda_c
        self.output = Path(output_dir)
        self.output.mkdir(parents=True, exist_ok=True)
        if (self.output / "account.db").exists():
            raise ContractError("replay requires a fresh cell account; reuse only a verified completed cell")
        self.store = V2Store(self.output / "account.db", data_mode="test")
        self.store.migrate()
        self.account_id = f"{run_id}:{strategy_id}:{split}:{cost_scenario}"
        self.portfolio = PaperPortfolio(self.store, self.account_id)
        self.initial = _money_cents(Decimal(str(config["shared_portfolio"]["initial_capital_cny"])))
        self.portfolio.open_account(method="formula", initial_cash_cents=self.initial,
                                    initial_event_at=f"{signal_dates[0]}T00:00:00+08:00")
        self.sessions = sessions
        self.index = {date: index for index, date in enumerate(sessions)}
        self.signal_dates = set(signal_dates)
        self.fees = fee_schedule(config, cost_scenario, signal_dates[0])
        self.policy = config["strategies"]["B0_PRISM_A_SHARE_V1"]
        self.b = strategy_id == "B0_PRISM_A_SHARE_V1"
        self.scale = lambda_c if strategy_id == "C0_V2_EXPOSURE_CONTROL" else 1.
        self.scope_flags = list(scope_flags)
        self.actions = [] if corporate_actions is None else corporate_actions.to_dict("records")
        self.pending_buys: list[PaperOrder] = []
        self.pending_exits: dict[str, PaperOrder] = {}
        self.order_metadata: dict[str, dict[str, Any]] = {}
        self.last_flat: dict[str, int] = {}
        self.last_marks: dict[str, Decimal] = {}
        self.mark_status: dict[str, str] = {}
        self.nav_rows: list[dict[str, Any]] = []
        self.slippage: dict[str, Decimal] = {}
        self.funnel: list[dict[str, Any]] = []
        self.rejected = 0

    def _update_lot(self, lot_id: str, metadata: dict[str, Any], exit_date: str | None = None) -> None:
        with self.store._write_connection() as conn:
            if exit_date is None:
                conn.execute("UPDATE paper_lots SET metadata_json=? WHERE lot_id=?", (canonical_json(metadata), lot_id))
            else:
                conn.execute("UPDATE paper_lots SET metadata_json=?,planned_exit_date=? WHERE lot_id=?",
                             (canonical_json(metadata), exit_date, lot_id))

    def _execute(self, date: str, rows: dict[str, dict[str, Any]]) -> None:
        applicable = [event for event in self.actions if _text(event.get("record_date")) < date]
        if applicable:
            apply_corporate_actions(self.store, self.account_id, applicable, as_of_date=date)
        orders = [*self.pending_exits.values(), *self.pending_buys]
        self.pending_buys = []
        if not orders:
            return
        before = {lot.lot_id: lot for lot in self.portfolio.lots() if lot.status == "OPEN"}
        market = {order.code: _open(order.code, date, rows.get(order.code, {})) for order in orders}
        rules = {code: rule for code in market
                 if (rule := security_rule(rows.get(code, {}).get("listing_board"), date, self.config)) is not None}
        batch = execute_open_orders(self.store, orders, market, fee_schedule=self.fees, security_rules=rules)
        by_id = {order.order_id: order for order in orders}
        for result in batch.results:
            order = by_id[result.order_id]
            if result.filled_quantity and result.status != "ALREADY_FILLED":
                self.slippage[order.order_id] = abs(result.fill_price - market[order.code].raw_open) * result.filled_quantity
                if order.side == "BUY":
                    lot_id = hashlib.sha256(f"lot|{order.order_id}".encode()).hexdigest()
                    lot = next(item for item in self.portfolio.lots() if item.lot_id == lot_id)
                    data = dict(lot.metadata)
                    planned = self.order_metadata[order.order_id]
                    factor = finite(rows.get(order.code, {}).get("adj_factor"))
                    comparison_entry = float(result.fill_price) * factor if factor is not None and factor > 0 else None
                    data.update(
                        entry_quantity=result.filled_quantity, entry_cost_cents=lot.cost_cents,
                        entry_signal_date=planned["signal_date"], entry_distance_fraction=planned["width_fraction"],
                        entry_sector_id=order.sector_id, entry_regime=planned["market_state"],
                        entry_comparison_factor=factor, highest_comparison_close=0.,
                        comparison_stop=(comparison_entry * (1 - planned["width_fraction"]))
                        if self.b and self.policy["enable_adaptive_stop"] and comparison_entry is not None else None,
                    )
                    policy_id = self.strategy if self.b else "A0_V2_F0"
                    expiry_index = self.index[date] + self.config["strategies"][policy_id]["max_planned_holding_sessions"]
                    expiry = self.sessions[expiry_index] if expiry_index < len(self.sessions) else order.planned_exit_date
                    self._update_lot(lot_id, data, expiry)
                else:
                    after = {lot.lot_id: lot for lot in self.portfolio.lots()}
                    for lot_id, previous in before.items():
                        current = after[lot_id]
                        if previous.code == order.code and current.quantity < previous.quantity:
                            data = dict(current.metadata)
                            data["exit_reason"] = self.order_metadata[order.order_id]["lot_reasons"].get(lot_id, "SCORE_EXIT")
                            self._update_lot(lot_id, data)
                    if not any(lot.status == "OPEN" and lot.code == order.code and lot.quantity > 0 for lot in after.values()) \
                            and order.code not in pending_share_quantities(list(after.values())):
                        self.last_flat[order.code] = self.index[date]
            if order.side == "SELL" and not result.status.startswith("PENDING"):
                self.pending_exits.pop(order.code, None)
                if result.status == "PARTIAL_FILLED":
                    alive = {lot.lot_id: lot.quantity for lot in self.portfolio.lots() if lot.status == "OPEN"}
                    caps = {key: min(value, alive.get(key, 0)) for key, value in order.lot_quantities}
                    consumed = result.filled_quantity
                    for key, value in order.lot_quantities:
                        old = before.get(key)
                        used = (old.quantity - alive.get(key, 0)) if old is not None else 0
                        caps[key] = min(max(0, value - used), alive.get(key, 0))
                        consumed -= used
                    if consumed != 0:
                        raise ContractError("partial sell lot consumption failed reconciliation")
                    self.order_metadata[order.order_id]["residual_caps"] = {key: value for key, value in caps.items() if value > 0}

    def _mark(self, date: str, rows: dict[str, dict[str, Any]]) -> Any:
        for code, row in rows.items():
            close = _decimal(row.get("valuation_close"))
            if close is not None and close > 0:
                self.last_marks[code] = close
        if self.actions:
            apply_corporate_actions(self.store, self.account_id, self.actions, as_of_date=date)
        lots = self.portfolio.lots()
        active_codes = {lot.code for lot in lots if lot.status == "OPEN"} | set(pending_share_quantities(lots))
        prices: dict[str, Any] = {}
        for code in active_codes:
            row = rows.get(code, {})
            delist = _text(row.get("delist_date"))
            if delist and date >= delist:
                self.mark_status[code] = "TERMINAL_UNRESOLVED"
                prices[code] = {"price": None}
            elif code in self.last_marks:
                self.mark_status[code] = "REAL_CLOSE" if finite(row.get("valuation_close")) is not None else "MARK_ONLY"
                prices[code] = {"price": self.last_marks[code], "mark_only": self.mark_status[code] == "MARK_ONLY"}
            else:
                self.mark_status[code] = "VALUATION_UNAVAILABLE"
                prices[code] = {"price": None}
        mark = mark_portfolio(self.store, self.account_id, date, prices)
        with closing(self.store._connect()) as conn:
            ledger_total = conn.execute("SELECT SUM(amount_cents) FROM paper_cash_ledger WHERE account_id=?", (self.account_id,)).fetchone()[0]
        if int(ledger_total) != mark.cash_cents or mark.cash_cents < 0:
            raise ContractError("cash ledger does not reconcile")
        if mark.nav_cents is not None and mark.nav_cents != mark.cash_cents + mark.position_value_cents + mark.receivable_cents:
            raise ContractError("NAV components do not reconcile")
        regime = next((_text(row.get("market_state"), "UNKNOWN") for row in rows.values()), "UNKNOWN")
        nav_row = dict(
            date=date, nav_cents=mark.nav_cents, known_value_cents=mark.known_value_cents,
            cash_cents=mark.cash_cents, position_value_cents=mark.position_value_cents,
            receivable_cents=mark.receivable_cents, status=mark.status,
            share_receivable_cents=mark.share_receivable_cents,
            gross_exposure=(mark.position_value_cents + mark.share_receivable_cents) / mark.nav_cents if mark.nav_cents else None,
            cash_ratio=mark.cash_cents / mark.nav_cents if mark.nav_cents else None,
            sector_exposure=mark.sector_exposure, market_state=regime,
            mark_only_codes=sorted(code for code in active_codes if self.mark_status.get(code) == "MARK_ONLY"),
            unresolved_codes=list(mark.unresolved_codes), accounting_reconciled=True,
        )
        self.nav_rows.append(nav_row)
        self.store.upsert_paper_valuations([{**nav_row, "account_id": self.account_id, "market_value_cents": mark.position_value_cents,
            "payload": {"receivable_cents": mark.receivable_cents, "sector_exposure": mark.sector_exposure}}])
        return mark

    def _decide(self, date: str, rows: dict[str, dict[str, Any]], mark: Any) -> list[dict[str, Any]]:
        index = self.index[date]
        next_date = self.sessions[index + 1] if index + 1 < len(self.sessions) else None
        lots = [lot for lot in self.portfolio.lots() if lot.status == "OPEN" and lot.quantity > 0]
        pending_shares = pending_share_quantities(self.portfolio.lots())
        by_code: dict[str, list[Any]] = defaultdict(list)
        for lot in lots:
            by_code[lot.code].append(lot)
        caps: dict[str, dict[str, int]] = defaultdict(dict)
        reasons: dict[str, dict[str, str]] = defaultdict(dict)
        for order in self.pending_exits.values():
            for lot_id, quantity in order.lot_quantities:
                caps[order.code][lot_id] = quantity
                reasons[order.code][lot_id] = self.order_metadata[order.order_id]["lot_reasons"][lot_id]
        for metadata in self.order_metadata.values():
            for lot_id, quantity in metadata.pop("residual_caps", {}).items():
                lot = next(item for item in lots if item.lot_id == lot_id)
                caps[lot.code][lot_id] = quantity
                reasons[lot.code][lot_id] = metadata["lot_reasons"][lot_id]
        decisions = []
        for code in sorted(set(rows) | set(by_code)):
            row, held_lots = rows.get(code, {}), by_code.get(code, [])
            quantity = sum(lot.quantity for lot in held_lots)
            sellable = sum(lot.sellable_quantity(date) for lot in held_lots)
            score5, score3 = finite(row.get("score5")), finite(row.get("score3"))
            native = self.config["native_contract"]
            factor_ok = row.get("prediction_status") == "OK" and score5 is not None
            overextended = _bool(row.get("overextended"))
            width, width_reasons = self._distance(row)
            expected = estimate_formula_return(self.model, score=score5, estimated_round_trip_cost=reference_cost(self.config, self.cost)) \
                if self.model is not None and score5 is not None else None
            gross = expected.expected_gross_return if expected else None
            edge = expected.expected_net_edge if expected else None
            predictions = [dict(method="formula", horizon=horizon, formula_score=finite(row.get(f"score{horizon}")),
                                prediction_status=_text(row.get("prediction_status"), "UNAVAILABLE"),
                                overextended=overextended is not False, expected_net_edge=edge if horizon == 5 else None)
                           for horizon in self.config["native_contract"]["horizons"]]
            intent = derive_research_intent(predictions, {"current_quantity": quantity, "sellable_quantity": sellable, "account_available": True}, horizon=5)
            raw_condition = score5 is not None and score5 >= native["entry_score5_at_least"] and (
                quantity > 0 or (score3 is not None and score3 >= native["entry_score3_at_least"]))
            candidate = raw_condition and factor_ok and (quantity > 0 or overextended is False) and date in self.signal_dates
            multiplier = finite(row.get("state_multiplier")) if self.b and self.policy["enable_market_state"] else 1.
            multiplier = 1. if multiplier is None else multiplier
            blocks: list[str] = []
            if date not in self.signal_dates:
                blocks.append("SIGNAL_WINDOW_CLOSED")
            if _bool(row.get("real_bar")) is not True:
                blocks.append("MISSING_OR_INVALID_REAL_BAR")
            if not factor_ok:
                blocks.append("PREDICTION_UNAVAILABLE")
            if not raw_condition:
                blocks.append("SCORE_ENTRY_CONDITION_NOT_MET")
            if quantity == 0 and overextended is not False:
                blocks.append("OVEREXTENDED_OR_UNKNOWN")
            if gross is None:
                blocks.append("RETURN_ESTIMATE_UNAVAILABLE")
            if edge is None or edge <= self.config["shared_portfolio"]["net_edge_min_exclusive"]:
                blocks.append("NET_EDGE_BELOW_MINIMUM")
            eligibility = self._eligibility(row, next_date)
            blocks.extend(eligibility)
            if code in pending_shares:
                blocks.append("PENDING_CORPORATE_SHARES_BLOCK_NEW_RISK")
            if mark.nav_cents is None:
                blocks.append("NAV_UNRESOLVED_BLOCKS_NEW_RISK")
            if next_date is None:
                blocks.append("NO_NEXT_SESSION")
            if width is None or finite(row.get("Q03")) is None or finite(row.get("Q03")) <= 0:
                blocks.append("SIZING_INPUT_UNAVAILABLE")
            if multiplier <= 0:
                blocks.append("MARKET_STATE_BLOCKS_NEW_RISK")
            cooling = self.b and self.policy["enable_cooldown"] and not entry_allowed(index + 1, self.last_flat.get(code), self.policy["cooldown_full_sessions"])
            if cooling:
                blocks.append("COOLDOWN_ACTIVE")
            code_reasons = list(reasons.get(code, {}).values())
            for lot in held_lots:
                reason = None
                delist = _text(row.get("delist_date"))
                if delist and next_date is not None and next_date >= delist:
                    reason = "REQUIRED_TERMINAL_DATA_EXIT"
                elif next_date is not None and lot.planned_exit_date and lot.planned_exit_date <= next_date:
                    reason = "FIXED_5_SESSION_EXPIRY"
                data = dict(lot.metadata)
                if self.b and self.policy["enable_adaptive_stop"]:
                    stop = finite(data.get("comparison_stop"))
                    close, factor = finite(row.get("valuation_close")), finite(row.get("adj_factor"))
                    comparison = close * factor if close is not None and factor is not None and close > 0 and factor > 0 else None
                    if stop is not None:
                        new_stop, highest, crossed = update_stop(stop, data.get("highest_comparison_close", 0.), comparison, width)
                        data.update(comparison_stop=new_stop, highest_comparison_close=highest)
                        self._update_lot(lot.lot_id, data)
                        if crossed and reason is None:
                            reason = "PRISM_CLOSE_STOP"
                if reason:
                    caps[code][lot.lot_id] = lot.quantity
                    reasons[code][lot.lot_id] = reason
                    code_reasons.append(reason)
            if intent.account_action in {"SELL", "REDUCE"}:
                rule = security_rule(row.get("listing_board"), next_date or date, self.config)
                target = sellable if intent.account_action == "SELL" else int(sellable * self.config["shared_portfolio"]["reduce_quantity_fraction"])
                if rule is not None:
                    target = rule.floor_sell_quantity(target, sellable)
                for lot in held_lots:
                    selected = min(target, lot.sellable_quantity(date))
                    target -= selected
                    if selected > caps[code].get(lot.lot_id, 0):
                        caps[code][lot.lot_id] = selected
                        reasons[code][lot.lot_id] = f"SCORE_{intent.account_action}"
                    if selected:
                        code_reasons.append(f"SCORE_{intent.account_action}")
            if caps.get(code):
                blocks.append("PENDING_EXIT_BLOCKS_NEW_RISK")
            gates = dict(roster=True, real_bar=_bool(row.get("real_bar")) is True, factors=factor_ok,
                         score65_55=raw_condition, not_extended=quantity > 0 or overextended is False,
                         return_estimate=gross is not None,
                         net_edge=edge is not None and edge > self.config["shared_portfolio"]["net_edge_min_exclusive"],
                         trade_eligibility=not eligibility, state_cooldown=multiplier > 0 and not cooling and not caps.get(code) and code not in pending_shares,
                         quantity=False, order=False)
            decisions.append(dict(
                date=date, code=code, strategy_id=self.strategy, split=self.split, cost_scenario=self.cost,
                signal_enabled=date in self.signal_dates, current_quantity=quantity, sellable_quantity=sellable,
                score1=finite(row.get("score1")), score3=score3, score5=score5,
                prediction_status=_text(row.get("prediction_status"), "UNAVAILABLE"),
                market_state=_text(row.get("market_state"), "UNKNOWN"), state_multiplier=multiplier,
                state_reason=_text(row.get("state_reason")) if self.b and self.policy["enable_market_state"] else "",
                width_fraction=width, width_reasons=width_reasons, expected_gross_return=gross,
                reference_net_edge=edge, quantity_cost=None, quantity_net_edge=None,
                research_intent=intent.research_intent, account_action=intent.account_action,
                entry_candidate=candidate, allocated_quantity=0, order_id=None,
                primary_blocker=blocks[0] if blocks else "", all_blockers=list(dict.fromkeys(blocks)),
                exit_reasons=list(dict.fromkeys(code_reasons)), **{f"gate_{key}": value for key, value in gates.items()},
            ))
        if next_date is not None:
            self._submit_exits(date, next_date, rows, caps, reasons, lots)
            self._allocate(date, next_date, rows, decisions, mark, lots)
        return decisions

    def _distance(self, row: dict[str, Any]) -> tuple[float | None, tuple[str, ...]]:
        if self.b and self.policy["enable_adaptive_stop"]:
            return adaptive_distance(row.get("Q01"), row.get("Q02"), row.get("Q02_prior60_median"), self.config)
        atr = finite(row.get("Q01"))
        shared = self.config["shared_portfolio"]
        return (max(shared["minimum_risk_distance_fraction"], shared["fixed_atr_multiple"] * atr), ()) if atr is not None and atr >= 0 else (None, ("ATR_UNAVAILABLE",))

    def _eligibility(self, row: dict[str, Any], next_date: str | None) -> list[str]:
        reasons = []
        if _text(row.get("instrument_type")).strip().lower() != "stock" or _bool(row.get("roster_active")) is not True or not _text(row.get("list_date")):
            reasons.append("INSTRUMENT_OR_HISTORY_UNKNOWN")
        if security_rule(row.get("listing_board"), next_date or "", self.config) is None:
            reasons.append("SECURITY_RULE_UNKNOWN")
        if not _text(row.get("sector_id")) or row.get("sector_context_status") != "OK":
            reasons.append("SECTOR_HISTORY_UNAVAILABLE")
        if row.get("market_context_status") != "OK" or finite(row.get("market_breadth20")) is None:
            reasons.append("MARKET_CONTEXT_UNAVAILABLE")
        risk = _bool(row.get("is_risk_warning"))
        if risk is True:
            reasons.append("RISK_WARNING_SECURITY")
        elif risk is None and self.config["data"]["risk_warning_unknown"] == "BLOCK_NEW_ENTRIES":
            reasons.append("HISTORICAL_RISK_WARNING_UNKNOWN")
        if _bool(row.get("is_suspended")) is not False:
            reasons.append("SUSPENDED_OR_UNKNOWN_AT_SIGNAL")
        if finite(row.get("adj_factor")) is None or finite(row.get("adj_factor")) <= 0:
            reasons.append("ADJUSTMENT_UNAVAILABLE")
        return reasons

    def _submit_exits(self, date, next_date, rows, caps, reasons, lots) -> None:
        alive = {lot.lot_id: lot.quantity for lot in lots}
        for code, requested in caps.items():
            selected = tuple((key, min(value, alive.get(key, 0))) for key, value in sorted(requested.items()) if min(value, alive.get(key, 0)) > 0)
            if not selected:
                continue
            old = self.pending_exits.get(code)
            if old is not None and old.lot_quantities == selected:
                self.order_metadata[old.order_id]["lot_reasons"] = {key: reasons[code][key] for key, _ in selected}
                continue
            if old is not None:
                with self.store._write_connection() as conn:
                    conn.execute("UPDATE paper_orders SET status='SUPERSEDED_PENDING_EXIT' WHERE order_id=?", (old.order_id,))
            identity = canonical_json([self.account_id, code, next_date, selected])
            order_id = hashlib.sha256(identity.encode()).hexdigest()
            order = PaperOrder(order_id, f"{self.account_id}:{date}", self.account_id, code, "SELL",
                               sum(value for _, value in selected), next_date,
                               self.last_marks.get(code, Decimal(0)), None,
                               sector_id=_text(rows.get(code, {}).get("sector_id"), "UNKNOWN"),
                               reason_codes=tuple(dict.fromkeys(reasons[code][key] for key, _ in selected)), lot_quantities=selected)
            persist_orders(self.store, [order])
            self.pending_exits[code] = order
            self.order_metadata[order_id] = {"signal_date": date, "lot_reasons": {key: reasons[code][key] for key, _ in selected}, "intent_reasons": order.reason_codes}

    def _allocate(self, date, next_date, rows, decisions, mark, lots) -> None:
        if mark.nav_cents is None:
            return
        shared = self.config["shared_portfolio"]
        nav, cash = Decimal(mark.nav_cents) / 100, mark.cash_cents
        values: dict[str, Decimal] = defaultdict(Decimal)
        sectors: dict[str, Decimal] = defaultdict(Decimal)
        names = {lot.code for lot in lots}
        for lot in lots:
            value = self.last_marks.get(lot.code, Decimal(0)) * lot.quantity
            values[lot.code] += value
            sectors[_text(rows.get(lot.code, {}).get("sector_id"), "UNKNOWN")] += value
        all_lots = self.portfolio.lots()
        for code, quantity in pending_share_quantities(all_lots, date).items():
            value = self.last_marks.get(code, Decimal(0)) * quantity
            sector = _text(rows.get(code, {}).get("sector_id")) or next(
                (_text(lot.metadata.get("sector_id")) for lot in all_lots
                 if lot.code == code and _text(lot.metadata.get("sector_id"))), "UNKNOWN")
            values[code] += value
            sectors[sector] += value
            names.add(code)
        gross = sum(values.values(), Decimal(0))
        eligible = sorted((item for item in decisions if item["entry_candidate"]),
                          key=lambda item: (-(item["reference_net_edge"] if item["reference_net_edge"] is not None else -math.inf), -item["score5"], item["code"]))
        for decision in eligible:
            code, row = decision["code"], rows[decision["code"]]
            blocks = decision["all_blockers"]
            breadth = finite(row.get("market_breadth20"))
            lower, upper = shared["market_breadth_breakpoints"]
            gross_cap = shared["market_gross_caps"][0 if breadth is None or breadth < lower else 1 if breadth < upper else 2] * self.scale
            if gross_cap <= 0:
                blocks.append("MARKET_EXPOSURE_CAP_ZERO")
            if code not in names and len(names) >= shared["max_names"]:
                blocks.append("MAX_NAMES_REACHED")
            if blocks:
                continue
            reference = _decimal(row.get("valuation_close"))
            if reference is None or reference <= 0:
                blocks.append("REFERENCE_PRICE_UNAVAILABLE")
                continue
            rule = security_rule(row.get("listing_board"), next_date, self.config)
            ceiling_fraction = min(shared["buy_ceiling_max_fraction"], shared["buy_ceiling_atr_fraction"] * float(row["Q01"]))
            ceiling = _round_to_tick(reference * (Decimal(1) + Decimal(str(ceiling_fraction))), rule.tick_size, buy=False)
            if ceiling <= 0:
                blocks.append("INVALID_PRICE_CEILING")
                continue
            sector = _text(row.get("sector_id"), "UNKNOWN")
            cap = min(
                max(Decimal(0), Decimal(str(shared["max_stock_weight"] * self.scale)) * nav - values[code]) / ceiling,
                max(Decimal(0), Decimal(str(shared["max_sector_weight"] * self.scale)) * nav - sectors[sector]) / ceiling,
                max(Decimal(0), Decimal(str(gross_cap)) * nav - gross) / ceiling,
                Decimal(cash) / (100 * ceiling),
                Decimal(str(shared["stock_risk_budget"] * self.scale)) * nav / (Decimal(str(decision["width_fraction"])) * reference),
                Decimal(str(shared["max_adv_participation"])) * Decimal(str(row["Q03"])) / reference,
            )
            quantity = rule.floor_buy_quantity(cap * Decimal(str(decision["state_multiplier"])))
            quantity, reserved_notional, reserved_fees, _ = _affordable_buy(quantity, rule.lot_size, ceiling, cash, self.fees, rule)
            if quantity <= 0:
                blocks.append("BELOW_LEGAL_QUANTITY_OR_BUDGET")
                continue
            exact_cost = quantity_cost(reference, quantity, self.fees, rule)
            decision["quantity_cost"] = exact_cost
            decision["quantity_net_edge"] = decision["expected_gross_return"] - exact_cost
            if decision["quantity_net_edge"] <= shared["net_edge_min_exclusive"]:
                blocks.append("QUANTITY_SPECIFIC_NET_EDGE")
                continue
            order_id = hashlib.sha256(f"{self.account_id}|{code}|BUY|{next_date}".encode()).hexdigest()
            expiry_index = self.index[next_date] + self.config["strategies"]["A0_V2_F0"]["max_planned_holding_sessions"]
            expiry = self.sessions[expiry_index] if expiry_index < len(self.sessions) else None
            order = PaperOrder(order_id, f"{self.account_id}:{date}", self.account_id, code, "BUY", quantity,
                               next_date, reference, ceiling, expiry,
                               reference * (Decimal(1) - Decimal(str(decision["width_fraction"]))), sector)
            persist_orders(self.store, [order])
            self.pending_buys.append(order)
            self.order_metadata[order_id] = {"signal_date": date, "width_fraction": decision["width_fraction"],
                "market_state": decision["market_state"], "expected_gross_return": decision["expected_gross_return"],
                "quantity_cost": exact_cost, "quantity_net_edge": decision["quantity_net_edge"], "intent_reasons": []}
            decision.update(allocated_quantity=quantity, order_id=order_id, gate_quantity=True, gate_order=True)
            reserved_value = Decimal(reserved_notional) / 100
            cash -= reserved_notional + reserved_fees
            values[code] += reserved_value
            sectors[sector] += reserved_value
            gross += reserved_value
            names.add(code)

    def day(self, date, frame, writer) -> None:
        if frame.code.duplicated().any() or ("date" in frame and not frame.date.astype(str).eq(date).all()):
            raise ContractError("daily cache has duplicate codes or mismatched sessions")
        rows = {str(row["code"]): row for row in frame.to_dict("records")}
        self._execute(date, rows)
        mark = self._mark(date, rows)
        decisions = self._decide(date, rows, mark)
        counts, primary, all_reasons = Counter(), Counter(), Counter()
        for decision in decisions:
            blocks = list(dict.fromkeys(decision["all_blockers"]))
            decision["all_blockers"] = blocks
            decision["primary_blocker"] = blocks[0] if blocks else ""
            cumulative = True
            for gate in GATES:
                cumulative &= bool(decision[f"gate_{gate}"])
                if cumulative and (decision["signal_enabled"] or gate == "roster"):
                    counts[gate] += 1
            if decision["entry_candidate"] and decision["allocated_quantity"] == 0:
                self.rejected += 1
            if decision["signal_enabled"]:
                primary.update([decision["primary_blocker"]] if decision["primary_blocker"] else [])
                all_reasons.update(blocks)
            writer.writerow({key: _csv_value(value) for key, value in decision.items()})
        for gate in GATES:
            self.funnel.append({"date": date, "stage": gate, "count": counts[gate], "reason_scope": "cumulative_gate"})
        for scope, reason_counts in (("first_blocker", primary), ("all_blockers", all_reasons)):
            self.funnel.extend({"date": date, "stage": key, "count": value, "reason_scope": scope} for key, value in sorted(reason_counts.items()))

    def finish(self, started: float) -> dict[str, Any]:
        nav = pd.DataFrame(self.nav_rows)
        with closing(self.store._connect()) as conn:
            orders = pd.read_sql_query(
                "SELECT o.*,d.reference_price,d.planned_exit_date,d.planned_stop_price,d.sector_id,d.payload_json "
                "FROM paper_orders o JOIN paper_order_details d USING(order_id) WHERE o.account_id=? "
                "ORDER BY o.earliest_trade_date,o.order_id", conn, params=[self.account_id])
        fills = self.store.read_paper_fills(self.account_id)
        fills["modeled_slippage_cny"] = [float(self.slippage.get(str(key), Decimal(0))) for key in fills.order_id]
        for field in ("signal_date", "intent_reasons", "quantity_cost", "quantity_net_edge"):
            orders[field] = [self.order_metadata.get(str(key), {}).get(field) for key in orders.order_id]
        remaining_columns = ["lot_id", "code", "entry_date", "planned_exit_date", "quantity", "cost_cents", "sector_id", "comparison_stop", "highest_comparison_close", "valuation_status", "asset_type"]
        remaining = []
        for lot in self.portfolio.lots():
            if lot.status == "OPEN":
                remaining.append(dict(lot_id=lot.lot_id, code=lot.code, entry_date=lot.entry_date,
                    planned_exit_date=lot.planned_exit_date, quantity=lot.quantity, cost_cents=lot.cost_cents,
                    sector_id=lot.metadata.get("entry_sector_id"), comparison_stop=lot.metadata.get("comparison_stop"),
                    highest_comparison_close=lot.metadata.get("highest_comparison_close"), valuation_status=self.mark_status.get(lot.code, "VALUATION_UNAVAILABLE"), asset_type="LISTED_SHARES"))
            for event_id, entitlement in lot.metadata.get("corporate_action_entitlements", {}).items():
                if not entitlement["shares_listed"] and int(entitlement["share_quantity"]) > 0:
                    remaining.append(dict(lot_id=f"{lot.lot_id}:receivable:{event_id}", code=lot.code,
                        entry_date=lot.entry_date, planned_exit_date=lot.planned_exit_date,
                        quantity=entitlement["share_quantity"], cost_cents=0,
                        sector_id=lot.metadata.get("entry_sector_id"), comparison_stop=lot.metadata.get("comparison_stop"),
                        highest_comparison_close=lot.metadata.get("highest_comparison_close"),
                        valuation_status=self.mark_status.get(lot.code, "VALUATION_UNAVAILABLE"), asset_type="CORPORATE_SHARE_RECEIVABLE"))
        closed_frame, remaining_frame = self.portfolio.closed_trades(), pd.DataFrame(remaining, columns=remaining_columns)
        for name, frame in (("daily_nav", nav), ("orders", orders), ("fills", fills), ("closed_trades", closed_frame)):
            _export_csv(self.output / f"{name}.csv.gz", frame)
        _export_csv(self.output / "remaining_positions.csv", remaining_frame)
        native = calculate_portfolio_metrics(nav, fills=fills, orders=orders, closed_trades=closed_frame,
                                             trading_days=self.config["evaluation"]["annualization_sessions"])
        pnl = pd.to_numeric(closed_frame.realized_pnl_cents, errors="coerce").dropna()
        loss = -float(pnl.loc[pnl.lt(0)].sum())
        unfilled = float(orders.status.ne("FILLED").mean()) if not orders.empty else None
        complete = native.status == "OK"
        turnover = native.turnover if native.turnover is not None else (0. if complete and fills.empty else None)
        buys = int(fills.side.eq("BUY").sum())
        metrics = dict(
            strategy_id=self.strategy, split=self.split, cost_scenario=self.cost,
            start_date=nav.iloc[0].date, end_date=nav.iloc[-1].date, initial_nav=self.initial / 100,
            final_nav=float(nav.iloc[-1].nav_cents) / 100 if pd.notna(nav.iloc[-1].nav_cents) else None,
            net_return=native.net_return, annualized_return=native.annualized_return,
            max_drawdown=native.max_drawdown, sharpe=native.sharpe if not fills.empty else None,
            daily_turnover=turnover / len(nav) if turnover is not None else None, total_turnover=turnover,
            average_gross_exposure=float(nav.gross_exposure.mean()) if complete else None,
            average_cash_ratio=native.average_cash_ratio, filled_buy_count=buys,
            closed_trade_count=len(closed_frame), traded_dates=int(fills.trade_date.nunique()),
            closed_trade_win_rate=native.closed_trade_win_rate, profit_factor=float(pnl.loc[pnl.gt(0)].sum()) / loss if loss > 0 else None,
            fees_total=float(fills.fee_cents.sum()) / 100, modeled_slippage_total=float(fills.modeled_slippage_cny.sum()),
            rejected_candidate_count=self.rejected, unfilled_order_ratio=unfilled,
            open_position_count=len({row["code"] for row in remaining}),
            unresolved_asset_count=len({row["code"] for row in remaining if row["valuation_status"] in {"TERMINAL_UNRESOLVED", "VALUATION_UNAVAILABLE"}}),
            data_scope_status="|".join(self.scope_flags) if self.scope_flags else "AVAILABLE_SCOPE",
            comparison_status="ACCOUNTING_OR_DATA_INCONCLUSIVE" if not complete else "INSUFFICIENT_TRADING_EVIDENCE"
                if len(closed_frame) < self.config["evaluation"]["min_closed_trades_for_winner"] or fills.trade_date.nunique() < self.config["evaluation"]["min_traded_dates_for_winner"] else "VALID_SCOPE_LIMITED" if self.scope_flags else "VALID",
            accounting_status=native.status, unresolved_nav_days=native.unresolved_nav_days,
            profit_factor_reason="NO_CLOSED_LOSS_DENOMINATOR" if loss == 0 else "",
            turnover_definition="BOTH_SIDES_NOTIONAL_OVER_MEAN_NAV; DAILY_DIVIDES_ALL_COMMON_SESSIONS",
            candidate_rejection_definition="RAW_NATIVE_BUY_OR_ADD_CONDITION_WITHIN_SIGNAL_WINDOW_WITHOUT_SUBMITTED_BUY",
            superseded_order_count=int(orders.status.eq("SUPERSEDED_PENDING_EXIT").sum()),
            execution_approximation="OPEN_AUCTION_EXECUTION_APPROXIMATION", lambda_c=self.lambda_c,
        )
        runtime = dict(status="COMPLETE", elapsed_seconds=time.perf_counter() - started,
                       peak_process_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                       session_count=len(nav), source="SHARED_V2_STORE_PORTFOLIO_EXECUTION", api_calls=0)
        for stage, count in (("fill", len(fills)), ("closed", len(closed_frame))):
            self.funnel.append({"date": "ALL", "stage": stage, "count": count, "reason_scope": "actual_events"})
        write_json(self.output / "metrics.json", metrics)
        write_json(self.output / "runtime.json", runtime)
        write_json(self.output / "gate_funnel.json", self.funnel)
        return {"metrics": metrics, "runtime": runtime, "funnel": self.funnel}


def replay_frames(frames: Iterable[tuple[str, pd.DataFrame]], *, sessions: list[str],
                  signal_dates: list[str], config: dict[str, Any], strategy_id: str,
                  split: str, cost_scenario: str, model: ReturnBinModel | None,
                  lambda_c: float, output_dir: str | Path, run_id: str,
                  scope_flags: Iterable[str] = (), corporate_actions: pd.DataFrame | None = None) -> dict[str, Any]:
    if strategy_id not in STRATEGIES or cost_scenario not in config["costs"]["slippage_cases"]:
        raise ContractError("unknown strategy or cost scenario")
    if sessions != sorted(set(sessions)) or signal_dates != sorted(set(signal_dates)) or not signal_dates:
        raise ContractError("replay needs ordered unique actual sessions and nonempty signal dates")
    if not set(signal_dates).issubset(sessions) or not 0 <= lambda_c <= 1:
        raise ContractError("signal dates or frozen exposure control are invalid")
    end_index = sessions.index(signal_dates[-1]) + config["split"]["final_tail_sessions"]
    if end_index >= len(sessions):
        raise ContractError("replay lacks the common settlement tail")
    expected = sessions[sessions.index(signal_dates[0]):end_index + 1]
    started = time.perf_counter()
    replay = _Replay(config=config, strategy_id=strategy_id, split=split, cost_scenario=cost_scenario,
                     model=model, lambda_c=lambda_c, output_dir=output_dir, run_id=run_id,
                     sessions=sessions, signal_dates=signal_dates, scope_flags=scope_flags,
                     corporate_actions=corporate_actions)
    seen = []
    with ExitStack() as stack:
        writer = _gzip_writer(stack, replay.output / "decisions.csv.gz", DECISION_COLUMNS)
        for date, frame in frames:
            if date not in expected:
                continue
            if len(seen) >= len(expected) or date != expected[len(seen)]:
                raise ContractError("daily cache order or coverage differs from the common replay window")
            replay.day(date, frame, writer)
            seen.append(date)
    if seen != expected:
        raise ContractError("daily cache omits a common replay session")
    return replay.finish(started)


CELL_FILES = ("daily_nav.csv.gz", "decisions.csv.gz", "orders.csv.gz", "fills.csv.gz",
              "closed_trades.csv.gz", "remaining_positions.csv", "metrics.json", "runtime.json",
              "gate_funnel.json", "account.db")


def replay_cell(experiment_root: str | Path, *, config: dict[str, Any], strategy_id: str,
                split: str, cost_scenario: str, lambda_c: float, output_dir: str | Path,
                run_id: str) -> dict[str, Any]:
    from core.pipeline.prism_compare_data import _verify_cache_file, load_daily_cache

    root, output = Path(experiment_root).resolve(), Path(output_dir).resolve()
    dataset_path = root / "dataset_manifest.json"
    dataset = json.loads(dataset_path.read_text())
    if dataset["configuration_sha256"] != sha256_json(config):
        raise ContractError("prepared dataset configuration differs from the replay configuration")
    if dataset["status"] != "OK" or split not in dataset["split_plan"]["replay_windows"]:
        raise ContractError("prepared dataset has no eligible frozen replay window")
    for path, expected in ((root / "feature_manifest.json", dataset["feature_manifest_sha256"]),
                           (root / "split.csv", dataset["split_sha256"])):
        if file_sha256(path) != expected:
            raise ContractError(f"prepared dataset SHA256 mismatch: {path}")
    _verify_cache_file(dataset["common_return_bins"])
    identity = implementation_identity()
    scale = lambda_c if strategy_id == "C0_V2_EXPOSURE_CONTROL" else 1.
    binding = {"dataset_sha256": file_sha256(dataset_path),
        "snapshot_sha256": dataset["snapshot"]["snapshot_sha256"],
        "feature_identity": dataset["feature_identity"], "configuration_sha256": sha256_json(config),
        "implementation_hash": identity["implementation_hash"], "strategy_id": strategy_id,
        "split": split, "cost_scenario": cost_scenario, "lambda_c": scale, "run_id": run_id}
    receipt_path = output / "cell_manifest.json"
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if receipt["status"] != "COMPLETE" or receipt["binding"] != binding:
            raise ContractError("completed cell binding/hash differs; do not reuse unrelated results")
        for name in CELL_FILES:
            record, path = receipt["files"][name], output / name
            if path.stat().st_size != record["bytes"] or file_sha256(path) != record["sha256"]:
                raise ContractError(f"completed cell SHA256 mismatch: {path}")
        return {"metrics": json.loads((output / "metrics.json").read_text()),
                "runtime": json.loads((output / "runtime.json").read_text()),
                "funnel": json.loads((output / "gate_funnel.json").read_text()),
                "cell_manifest": receipt, "reused": True}
    if output.exists() and any(output.iterdir()):
        raise ContractError("incomplete cell preserved; recover to a fresh attempt before replay")
    feature_manifest = json.loads((root / "feature_manifest.json").read_text())
    sessions = feature_manifest["sessions"]
    window = dataset["split_plan"]["replay_windows"][split]
    dates = sessions[sessions.index(window["start_date"]):sessions.index(window["end_date"]) + 1]
    data = json.loads(Path(dataset["common_return_bins"]["path"]).read_text())
    model = ReturnBinModel(**{**data, "bins": tuple(ReturnBinStats(**item) for item in data["bins"])})
    if model.entity_type != "stock" or model.horizon != config["strategies"]["A0_V2_F0"]["primary_horizon"]:
        raise ContractError("shared return bins have a different entity or horizon")
    result = replay_frames(load_daily_cache(root, dates), sessions=sessions,
        signal_dates=window["signal_dates"], config=config, strategy_id=strategy_id, split=split,
        cost_scenario=cost_scenario, model=model, lambda_c=scale, output_dir=output, run_id=run_id,
        scope_flags=dataset["scope_flags"], corporate_actions=pd.read_parquet(root / "corporate_actions.parquet"))
    receipt = {"status": "COMPLETE", "binding": binding, "source_identity": identity,
               "files": {name: {"bytes": (output / name).stat().st_size,
                                "sha256": file_sha256(output / name)} for name in CELL_FILES}}
    write_json(receipt_path, receipt, immutable=True)
    return {**result, "cell_manifest": receipt, "reused": False}
