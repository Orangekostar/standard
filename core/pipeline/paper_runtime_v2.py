from __future__ import annotations

import hashlib
import json
import math
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pandas as pd

from core.backtest.execution_v2 import (
    FeeSchedule,
    MarketOpen,
    PaperOrder,
    SecurityRule,
    allocate_orders,
    execute_open_orders,
    persist_orders,
)
from core.backtest.portfolio_v2 import (
    PaperPortfolio,
    apply_corporate_actions,
    mark_portfolio,
)
from core.data.v2_store import V2Store
from core.technical_v2.contracts import ContractError, json_safe

ACCOUNT_BY_METHOD = {
    "formula": "formula-paper",
    "jev": "jev-shadow-paper",
}
INITIAL_CASH_CENTS = 100_000_000


@dataclass(frozen=True)
class PaperCycleResult:
    run_id: str
    as_of_trade_date: str
    next_trade_date: str
    orders_created: int
    orders_executed: int
    decision_rows: int
    account_statuses: dict[str, str]


@dataclass(frozen=True)
class MaturedEvaluationResult:
    evaluated_rows: int
    total_evaluated_rows: int
    mature_signal_dates: int
    forward_statuses: dict[str, str]


def _payloads(store: V2Store, run_id: str, method: str) -> list[dict[str, Any]]:
    frame = store.read_prediction_rows(run_id, method=method, horizon=5)
    rows: list[dict[str, Any]] = []
    for record in frame.loc[frame["entity_type"].eq("stock")].to_dict(orient="records"):
        try:
            payload = json.loads(record["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractError(f"invalid persisted prediction payload: {run_id}") from exc
        if not isinstance(payload, dict):
            raise ContractError(f"prediction payload is not an object: {run_id}")
        rows.append(payload)
    return rows


def _rules(codes: list[str]) -> dict[str, SecurityRule]:
    return {
        code: SecurityRule(
            lot_size=100,
            tick_size=Decimal("0.01"),
            effective_from="19900101",
            effective_to=None,
            source="standard-v2-cn-equity-rule-v1",
        )
        for code in codes
    }


def _pending_orders(store: V2Store, account_id: str, as_of: str) -> list[PaperOrder]:
    with closing(store._connect()) as conn:
        rows = conn.execute(
            """
            SELECT o.*, d.reference_price, d.planned_exit_date,
                   d.planned_stop_price, d.sector_id
            FROM paper_orders o
            JOIN paper_order_details d ON d.order_id = o.order_id
            WHERE o.account_id = ? AND o.earliest_trade_date <= ?
              AND (o.status = 'PENDING' OR o.status LIKE 'PENDING_EXIT_%')
            ORDER BY CASE WHEN o.side = 'SELL' THEN 0 ELSE 1 END,
                     o.earliest_trade_date, o.order_id
            """,
            (account_id, as_of),
        ).fetchall()
    return [
        PaperOrder(
            order_id=str(row["order_id"]),
            run_id=str(row["run_id"]),
            account_id=str(row["account_id"]),
            code=str(row["code"]),
            side=str(row["side"]),
            quantity=int(row["quantity"]),
            earliest_trade_date=str(row["earliest_trade_date"]),
            reference_price=Decimal(str(row["reference_price"])),
            price_ceiling_floor=(
                Decimal(str(row["price_ceiling_floor"]))
                if row["price_ceiling_floor"] is not None
                else None
            ),
            planned_exit_date=row["planned_exit_date"],
            planned_stop_price=(
                Decimal(str(row["planned_stop_price"]))
                if row["planned_stop_price"] is not None
                else None
            ),
            sector_id=row["sector_id"],
        )
        for row in rows
    ]


def _market_open(
    row: dict[str, Any],
    status: dict[str, Any] | None,
    *,
    data_mode: str,
    listing_board: str,
) -> MarketOpen:
    pre_close = Decimal(str(row["pre_close"])) if pd.notna(row.get("pre_close")) else None
    if data_mode in {"demo", "test"}:
        limit = Decimal("0.20") if listing_board in {"STAR", "CHINEXT"} else Decimal("0.10")
        up_limit = pre_close * (Decimal(1) + limit) if pre_close is not None else None
        down_limit = pre_close * (Decimal(1) - limit) if pre_close is not None else None
        suspended: bool | None = False
        no_price_limit: bool | None = False
    else:
        up_limit = (
            Decimal(str(status["up_limit"]))
            if status is not None and pd.notna(status.get("up_limit"))
            else None
        )
        down_limit = (
            Decimal(str(status["down_limit"]))
            if status is not None and pd.notna(status.get("down_limit"))
            else None
        )
        suspended = (
            bool(status["is_suspended"])
            if status is not None and pd.notna(status.get("is_suspended"))
            else None
        )
        no_price_limit = (
            bool(status["no_price_limit"])
            if status is not None and pd.notna(status.get("no_price_limit"))
            else False
        )
    return MarketOpen(
        code=str(row["code"]),
        trade_date=str(row["date"]),
        raw_open=Decimal(str(row["open"])) if pd.notna(row.get("open")) else None,
        up_limit=up_limit,
        down_limit=down_limit,
        no_price_limit=no_price_limit,
        is_suspended=suspended,
    )


def _holdings(
    portfolio: PaperPortfolio,
    as_of: str,
    next_trade_date: str,
    prices: dict[str, float],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for lot in portfolio.lots():
        if lot.status != "OPEN" or lot.quantity <= 0:
            continue
        item = grouped.setdefault(
            lot.code,
            {
                "code": lot.code,
                "quantity": 0,
                "sellable_quantity": 0,
                "market_value_cny": 0.0,
                "sector_id": str(lot.metadata.get("sector_id") or "UNKNOWN"),
                "force_exit": False,
                "exit_reason_codes": [],
            },
        )
        item["quantity"] += lot.quantity
        item["sellable_quantity"] += lot.sellable_quantity(as_of)
        if lot.planned_exit_date and str(lot.planned_exit_date) <= next_trade_date:
            item["force_exit"] = True
            item["exit_reason_codes"].append("PLANNED_EXIT_DUE")
        stop_raw = lot.metadata.get("planned_stop_price")
        try:
            stop_price = Decimal(str(stop_raw)) if stop_raw is not None else None
        except (ValueError, ArithmeticError):
            stop_price = None
        close = prices.get(lot.code)
        if stop_price is not None and close is not None and Decimal(str(close)) <= stop_price:
            item["force_exit"] = True
            item["exit_reason_codes"].append("STOP_TRIGGERED_AT_CLOSE")
    for code, item in grouped.items():
        item["market_value_cny"] = float(prices.get(code, 0.0)) * int(item["quantity"])
    return list(grouped.values())


def _candidate(payload: dict[str, Any], holdings: dict[str, dict[str, Any]], next_trade_date: str) -> dict[str, Any]:
    code = str(payload["entity_id"])
    risk = payload.get("risk_metrics") if isinstance(payload.get("risk_metrics"), dict) else {}
    action = str(payload.get("account_action") or "")
    holding = holdings.get(code)
    if holding and bool(holding.get("force_exit")):
        action = "SELL"
    elif holding and action == "BUY":
        action = "ADD"
    return {
        "code": code,
        "horizon": 5,
        "account_action": action,
        "prediction_status": str(payload.get("prediction_status") or "UNAVAILABLE"),
        "trade_eligible": bool(payload.get("trade_eligible")),
        "expected_net_edge": payload.get("expected_net_edge"),
        "formula_score": payload.get("formula_score"),
        "reference_price": payload.get("reference_price"),
        "atr_pct14": risk.get("Q01"),
        "adv20_cny": risk.get("Q03"),
        "sector_id": str(payload.get("sector_id") or "UNKNOWN"),
        "planned_exit_date": payload.get("planned_exit_date"),
        "earliest_trade_date": next_trade_date,
        "reason_codes": tuple(holding.get("exit_reason_codes", ())) if holding else (),
    }


def run_paper_cycle(
    store: V2Store,
    *,
    run_id: str,
    as_of_trade_date: str,
    methods: tuple[str, ...] = ("formula", "jev"),
) -> PaperCycleResult:
    as_of = str(as_of_trade_date).replace("-", "")[:8]
    next_trade_date = store.next_session("SSE", as_of)
    if next_trade_date is None:
        raise ContractError("paper cycle requires a known next exchange session")
    instruments = store.read_instrument_versions(as_of)
    metadata = {
        str(row["code"]): row for row in instruments.to_dict(orient="records")
    }
    codes = sorted(metadata)
    raw = store.read_daily_raw(as_of, as_of, codes)
    raw_lookup = {str(row["code"]): row for row in raw.to_dict(orient="records")}
    status_frame = store.read_trading_status(as_of, as_of, codes)
    status_lookup = {
        str(row["code"]): row for row in status_frame.to_dict(orient="records")
    }
    rules = _rules(codes)
    opens = {
        code: _market_open(
            row,
            status_lookup.get(code),
            data_mode=store.data_mode,
            listing_board=str(metadata.get(code, {}).get("listing_board") or ""),
        )
        for code, row in raw_lookup.items()
    }
    closes = {
        code: float(row["close"])
        for code, row in raw_lookup.items()
        if pd.notna(row.get("close"))
    }
    actions = store.read_corporate_actions().to_dict(orient="records")
    fees = FeeSchedule.research_defaults("19900101", source="standard-v2-research-fees-v1")
    total_created = 0
    total_executed = 0
    total_decisions = 0
    account_statuses: dict[str, str] = {}
    for method in methods:
        if method not in ACCOUNT_BY_METHOD:
            raise ContractError(f"unsupported paper method: {method}")
        account_id = ACCOUNT_BY_METHOD[method]
        portfolio = PaperPortfolio(store, account_id)
        portfolio.open_account(method=method, initial_cash_cents=INITIAL_CASH_CENTS)
        pending = _pending_orders(store, account_id, as_of)
        if pending:
            execution = execute_open_orders(
                store,
                pending,
                opens,
                fee_schedule=fees,
                security_rules=rules,
            )
            total_executed += sum(
                result.status in {"FILLED", "PARTIAL_FILLED", "ALREADY_FILLED"}
                for result in execution.results
            )
        apply_corporate_actions(store, account_id, actions, as_of_date=as_of)
        mark = mark_portfolio(store, account_id, as_of, closes)
        store.upsert_paper_valuations(
            [
                {
                    "account_id": account_id,
                    "date": as_of,
                    "nav_cents": mark.nav_cents,
                    "cash_cents": mark.cash_cents,
                    "market_value_cents": mark.position_value_cents,
                    "status": mark.status,
                    "reason_codes": [
                        *("VALUATION_PRICE_UNAVAILABLE" for _ in mark.unresolved_codes),
                    ],
                    "payload": {
                        "known_value_cents": mark.known_value_cents,
                        "unresolved_codes": mark.unresolved_codes,
                        "sector_exposure": mark.sector_exposure,
                        "account_type": "paper" if method == "formula" else "shadow_paper",
                    },
                }
            ]
        )
        account_statuses[method] = mark.status
        if mark.nav_cents is None:
            continue
        holdings = _holdings(portfolio, as_of, next_trade_date, closes)
        holding_lookup = {str(item["code"]): item for item in holdings}
        payloads = _payloads(store, run_id, method)
        candidates = [
            _candidate(payload, holding_lookup, next_trade_date)
            for payload in payloads
        ]
        breadth_values = []
        for payload in payloads:
            factors = payload.get("factor_values") if isinstance(payload.get("factor_values"), dict) else {}
            value = pd.to_numeric(pd.Series([factors.get("F14")]), errors="coerce").iloc[0]
            if pd.notna(value) and math.isfinite(float(value)):
                breadth_values.append((float(value) + 1.0) / 2.0)
        breadth = float(pd.Series(breadth_values).median()) if breadth_values else 0.0
        allocation = allocate_orders(
            candidates,
            run_id=run_id,
            account_id=account_id,
            as_of_trade_date=as_of,
            earliest_trade_date=next_trade_date,
            nav_cents=int(mark.nav_cents),
            cash_cents=portfolio.cash_cents(),
            market_breadth20=breadth,
            holdings=holdings,
            security_rules=rules,
        )
        total_created += persist_orders(store, allocation.orders)
        status_by_code = {str(row["code"]): row for row in allocation.status_rows}
        decisions = []
        for payload in payloads:
            entity_id = str(payload["entity_id"])
            status_row = status_by_code.get(
                entity_id,
                {"status": "BLOCKED", "reason_codes": ("ALLOCATION_STATUS_MISSING",)},
            )
            decision_id = hashlib.sha256(
                f"paper-decision|{run_id}|{account_id}|{entity_id}|{as_of}".encode()
            ).hexdigest()
            decisions.append(
                {
                    "decision_id": decision_id,
                    "run_id": run_id,
                    "account_id": account_id,
                    "entity_id": entity_id,
                    "as_of_trade_date": as_of,
                    "method": method,
                    "status": status_row["status"],
                    "reason_codes": status_row["reason_codes"],
                    "payload": {
                        "horizon": 5,
                        "account_action": payload.get("account_action"),
                        "prediction_status": payload.get("prediction_status"),
                        "next_trade_date": next_trade_date,
                        "account_type": "paper" if method == "formula" else "shadow_paper",
                    },
                }
            )
        total_decisions += store.write_paper_decisions(decisions)
    return PaperCycleResult(
        run_id=run_id,
        as_of_trade_date=as_of,
        next_trade_date=next_trade_date,
        orders_created=total_created,
        orders_executed=total_executed,
        decision_rows=total_decisions,
        account_statuses=account_statuses,
    )


def evaluate_matured_predictions(store: V2Store) -> MaturedEvaluationResult:
    predictions = store.read_prediction_rows()
    labels = store.read_label_rows(matured_only=True)
    if predictions.empty or labels.empty:
        existing = store.read_evaluation_rows()
        return MaturedEvaluationResult(0, len(existing), 0, {"jev": "JEV_SHADOW_UNVALIDATED"})
    parsed: list[dict[str, Any]] = []
    for row in predictions.to_dict(orient="records"):
        if str(row["prediction_status"]) != "OK":
            continue
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ContractError(f"invalid persisted prediction payload: {row['run_id']}") from exc
        parsed.append(
            {
                **row,
                "as_of_trade_date": str(payload.get("as_of_trade_date") or ""),
                "forecast_class": str(payload.get("forecast_class") or "unknown").lower(),
                "p_up": payload.get("p_cal_up") if payload.get("p_cal_up") is not None else payload.get("p_raw_up"),
                "p_flat": payload.get("p_cal_flat") if payload.get("p_cal_flat") is not None else payload.get("p_raw_flat"),
                "p_down": payload.get("p_cal_down") if payload.get("p_cal_down") is not None else payload.get("p_raw_down"),
            }
        )
    if not parsed:
        existing = store.read_evaluation_rows()
        return MaturedEvaluationResult(0, len(existing), 0, {"jev": "JEV_SHADOW_UNVALIDATED"})
    prediction_frame = pd.DataFrame(parsed)
    keys = ["entity_type", "entity_id", "as_of_trade_date", "horizon"]
    joined = prediction_frame.merge(
        labels,
        on=keys,
        how="inner",
        validate="many_to_one",
        suffixes=("_prediction", "_label"),
    )
    rows = []
    for record in joined.to_dict(orient="records"):
        target = str(record["target_class"] or "unknown").lower()
        forecast = str(record["forecast_class"] or "unknown").lower()
        probabilities = {
            "up": record.get("p_up"),
            "flat": record.get("p_flat"),
            "down": record.get("p_down"),
        }
        probability = pd.to_numeric(pd.Series([probabilities.get(target)]), errors="coerce").iloc[0]
        brier = None
        log_loss = None
        numeric_probabilities = pd.to_numeric(pd.Series(list(probabilities.values())), errors="coerce")
        if numeric_probabilities.notna().all():
            values = [float(value) for value in numeric_probabilities]
            brier = sum(
                (value - (1.0 if name == target else 0.0)) ** 2
                for name, value in zip(("up", "flat", "down"), values)
            ) / 3.0
            log_loss = -math.log(max(float(probability), 1e-15))
        payload = {
            "forecast_class": forecast,
            "target_class": target,
            "direction_correct": forecast == target,
            "realized_return": float(record["realized_return"]),
            "brier_score": brier,
            "log_loss": log_loss,
            "target_definition_version": record["target_definition_version"],
        }
        rows.append(
            {
                "run_id": record["run_id"],
                "entity_type": record["entity_type"],
                "entity_id": record["entity_id"],
                "as_of_trade_date": record["as_of_trade_date"],
                "horizon": record["horizon"],
                "method": record["method"],
                "label_end_date": record["label_end_date"],
                "evaluation_status": "OK",
                "payload": json_safe(payload),
            }
        )
    inserted = store.write_evaluation_rows(rows)
    existing = store.read_evaluation_rows()
    mature_dates = int(existing["as_of_trade_date"].nunique()) if not existing.empty else 0
    statuses: dict[str, str] = {}
    for method in ("formula", "jev"):
        subset = existing.loc[existing["method"].eq(method)] if not existing.empty else existing
        entity_counts = subset.groupby("entity_type").size() if not subset.empty else pd.Series(dtype=int)
        enough = (
            int(subset["as_of_trade_date"].nunique()) >= 60
            and not entity_counts.empty
            and bool((entity_counts >= 1000).all())
        )
        statuses[method] = (
            "FORWARD_OBSERVATION_THRESHOLD_REACHED"
            if enough
            else "JEV_SHADOW_UNVALIDATED"
            if method == "jev"
            else "FORWARD_EVIDENCE_ACCUMULATING"
        )
    return MaturedEvaluationResult(inserted, len(existing), mature_dates, statuses)
