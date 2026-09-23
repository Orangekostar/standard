from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from core.data.v2_store import V2Store
from core.technical_v2.contracts import ContractError, canonical_json


def _date(value: Any) -> str:
    return str(value or "").strip().replace("-", "")[:8]


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _sort_float(value: Any, default: float) -> float:
    parsed = _decimal(value)
    return float(parsed) if parsed is not None else default


def _money_cents(value_cny: Decimal) -> int:
    return int((value_cny * Decimal(100)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _round_to_tick(price: Decimal, tick: Decimal, *, buy: bool) -> Decimal:
    rounding = ROUND_CEILING if buy else ROUND_FLOOR
    return (price / tick).to_integral_value(rounding=rounding) * tick


@dataclass(frozen=True)
class FeeSchedule:
    effective_from: str
    effective_to: str | None
    commission_rate: Decimal
    minimum_commission_cny: Decimal
    sell_addon_rate: Decimal
    other_rate_each_side: Decimal
    slippage_each_side: Decimal
    source: str

    @classmethod
    def research_defaults(cls, effective_from: str, *, source: str) -> FeeSchedule:
        return cls(
            effective_from=_date(effective_from),
            effective_to=None,
            commission_rate=Decimal("0.0003"),
            minimum_commission_cny=Decimal(5),
            sell_addon_rate=Decimal("0.0005"),
            other_rate_each_side=Decimal("0.00001"),
            slippage_each_side=Decimal("0.001"),
            source=source,
        )

    def applies(self, trade_date: str) -> bool:
        value = _date(trade_date)
        start = _date(self.effective_from)
        end = _date(self.effective_to)
        return bool(self.source and start and start <= value and (not end or value <= end))

    def fees(self, notional_cny: Decimal, side: str) -> tuple[int, dict[str, int]]:
        commission = max(self.minimum_commission_cny, notional_cny * self.commission_rate)
        other = notional_cny * self.other_rate_each_side
        sell_addon = notional_cny * self.sell_addon_rate if side == "SELL" else Decimal(0)
        components = {
            "commission_cents": _money_cents(commission),
            "other_cents": _money_cents(other),
            "sell_addon_cents": _money_cents(sell_addon),
        }
        return sum(components.values()), components


@dataclass(frozen=True)
class SecurityRule:
    lot_size: int
    tick_size: Decimal
    effective_from: str
    effective_to: str | None
    source: str

    def applies(self, trade_date: str) -> bool:
        value = _date(trade_date)
        start = _date(self.effective_from)
        end = _date(self.effective_to)
        return (
            self.lot_size > 0
            and self.tick_size > 0
            and bool(self.source)
            and bool(start)
            and start <= value
            and (not end or value <= end)
        )


@dataclass(frozen=True)
class MarketOpen:
    code: str
    trade_date: str
    raw_open: Decimal | None
    up_limit: Decimal | None
    down_limit: Decimal | None
    no_price_limit: bool | None
    is_suspended: bool | None


@dataclass(frozen=True)
class PaperOrder:
    order_id: str
    run_id: str
    account_id: str
    code: str
    side: str
    quantity: int
    earliest_trade_date: str
    reference_price: Decimal
    price_ceiling_floor: Decimal | None
    planned_exit_date: str | None = None
    planned_stop_price: Decimal | None = None
    sector_id: str | None = None
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class FillResult:
    order_id: str
    status: str
    filled_quantity: int
    fill_price: Decimal | None
    fee_cents: int
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionBatchResult:
    results: tuple[FillResult, ...]


@dataclass(frozen=True)
class AllocationResult:
    orders: tuple[PaperOrder, ...]
    status_rows: tuple[dict[str, Any], ...]


def _stable_order_id(run_id: str, account_id: str, code: str, side: str, trade_date: str) -> str:
    payload = f"{run_id}|{account_id}|{code}|{side}|{trade_date}"
    return hashlib.sha256(payload.encode()).hexdigest()


def allocate_orders(
    candidates: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    account_id: str,
    as_of_trade_date: str,
    earliest_trade_date: str,
    nav_cents: int,
    cash_cents: int,
    market_breadth20: float,
    holdings: Sequence[Mapping[str, Any]],
    security_rules: Mapping[str, SecurityRule],
) -> AllocationResult:
    if nav_cents <= 0 or cash_cents < 0:
        raise ContractError("allocation requires positive NAV and non-negative cash")
    if not math.isfinite(float(market_breadth20)):
        raise ContractError("market breadth must be finite")
    exposure_cap = 0.0 if market_breadth20 < 0.35 else 0.30 if market_breadth20 < 0.55 else 0.60
    nav_cny = Decimal(nav_cents) / Decimal(100)
    cash_cny = Decimal(cash_cents) / Decimal(100)
    holding_by_code = {str(item["code"]): dict(item) for item in holdings}
    gross_cny = sum(
        (_decimal(item.get("market_value_cny")) or Decimal(0)) for item in holding_by_code.values()
    )
    sector_cny: dict[str, Decimal] = {}
    for item in holding_by_code.values():
        sector = str(item.get("sector_id") or "UNKNOWN")
        sector_cny[sector] = sector_cny.get(sector, Decimal(0)) + (
            _decimal(item.get("market_value_cny")) or Decimal(0)
        )
    orders: list[PaperOrder] = []
    statuses: list[dict[str, Any]] = []
    ordered = sorted(
        candidates,
        key=lambda row: (
            0 if str(row.get("account_action")) in {"SELL", "REDUCE"} else 1,
            -_sort_float(row.get("expected_net_edge"), -math.inf),
            -_sort_float(row.get("formula_score"), 0.0),
            str(row.get("code") or ""),
        ),
    )
    planned_names = {code for code, item in holding_by_code.items() if int(item.get("quantity", 0) or 0) > 0}
    for candidate in ordered:
        code = str(candidate.get("code") or "")
        action = str(candidate.get("account_action") or "")
        reasons: list[str] = []
        rule = security_rules.get(code)
        if int(candidate.get("horizon", 0) or 0) != 5:
            reasons.append("DIAGNOSTIC_HORIZON")
        if not code or action not in {"BUY", "ADD", "REDUCE", "SELL"}:
            reasons.append("NO_EXECUTABLE_ACTION")
        if rule is None or not rule.applies(earliest_trade_date):
            reasons.append("RULE_METADATA_MISSING")
        holding = holding_by_code.get(code, {})
        if action in {"SELL", "REDUCE"}:
            sellable = int(holding.get("sellable_quantity", 0) or 0)
            if sellable <= 0:
                reasons.append("NO_SELLABLE_QUANTITY")
            quantity = sellable if action == "SELL" else sellable // 2
            if rule is not None and action == "REDUCE":
                quantity = quantity // rule.lot_size * rule.lot_size
            if quantity <= 0:
                reasons.append("BELOW_TRADING_UNIT")
            if not reasons:
                reference = _decimal(candidate.get("reference_price")) or Decimal(0)
                order = PaperOrder(
                    _stable_order_id(run_id, account_id, code, "SELL", earliest_trade_date),
                    run_id,
                    account_id,
                    code,
                    "SELL",
                    quantity,
                    _date(earliest_trade_date),
                    reference,
                    None,
                    candidate.get("planned_exit_date"),
                    sector_id=str(candidate.get("sector_id") or holding.get("sector_id") or "UNKNOWN"),
                    reason_codes=tuple(str(value) for value in candidate.get("reason_codes", ())),
                )
                orders.append(order)
                gross_cny = max(Decimal(0), gross_cny - reference * quantity)
            statuses.append({"code": code, "status": "PLANNED" if not reasons else "BLOCKED", "reason_codes": tuple(reasons)})
            continue

        if str(candidate.get("prediction_status")) != "OK" or not bool(candidate.get("trade_eligible")):
            reasons.append("NOT_TRADE_ELIGIBLE")
        edge = _decimal(candidate.get("expected_net_edge"))
        edge_required = not bool(candidate.get("allow_without_return_estimate", False))
        if edge_required and (edge is None or edge <= Decimal("0.001")):
            reasons.append("NET_EDGE_BELOW_MINIMUM")
        if exposure_cap <= 0:
            reasons.append("MARKET_EXPOSURE_CAP_ZERO")
        reference = _decimal(candidate.get("reference_price"))
        atr_pct = _decimal(candidate.get("atr_pct14"))
        adv20 = _decimal(candidate.get("adv20_cny"))
        if reference is None or reference <= 0 or atr_pct is None or atr_pct < 0 or adv20 is None or adv20 <= 0:
            reasons.append("SIZING_INPUT_UNAVAILABLE")
        sector = str(candidate.get("sector_id") or "UNKNOWN")
        if action == "BUY" and code not in planned_names and len(planned_names) >= 10:
            reasons.append("MAX_NAMES_REACHED")
        if reasons:
            statuses.append({"code": code, "status": "BLOCKED", "reason_codes": tuple(dict.fromkeys(reasons))})
            continue
        if rule is None or reference is None or atr_pct is None or adv20 is None:
            raise ContractError("validated allocation inputs unexpectedly unavailable")
        stop_distance = max(Decimal("2.5") * atr_pct * reference, Decimal("0.03") * reference)
        stock_room = max(Decimal(0), Decimal("0.10") * nav_cny - (_decimal(holding.get("market_value_cny")) or Decimal(0)))
        sector_room = max(Decimal(0), Decimal("0.25") * nav_cny - sector_cny.get(sector, Decimal(0)))
        gross_room = max(Decimal(0), Decimal(str(exposure_cap)) * nav_cny - gross_cny)
        risk_quantity = Decimal("0.005") * nav_cny / stop_distance
        adv_quantity = Decimal("0.01") * adv20 / reference
        quantity_cap = min(
            stock_room / reference,
            sector_room / reference,
            gross_room / reference,
            cash_cny / reference,
            risk_quantity,
            adv_quantity,
        )
        quantity = int(quantity_cap) // rule.lot_size * rule.lot_size
        if quantity <= 0:
            statuses.append({"code": code, "status": "BLOCKED", "reason_codes": ("BELOW_TRADING_UNIT",)})
            continue
        ceiling_fraction = min(Decimal("0.02"), Decimal("0.5") * atr_pct)
        ceiling = _round_to_tick(reference * (Decimal(1) + ceiling_fraction), rule.tick_size, buy=False)
        planned_stop = _round_to_tick(reference - stop_distance, rule.tick_size, buy=False)
        order = PaperOrder(
            _stable_order_id(run_id, account_id, code, "BUY", earliest_trade_date),
            run_id,
            account_id,
            code,
            "BUY",
            quantity,
            _date(earliest_trade_date),
            reference,
            ceiling,
            candidate.get("planned_exit_date"),
            planned_stop,
            sector,
        )
        orders.append(order)
        planned_names.add(code)
        notional = reference * quantity
        gross_cny += notional
        sector_cny[sector] = sector_cny.get(sector, Decimal(0)) + notional
        cash_cny = max(Decimal(0), cash_cny - notional)
        statuses.append({"code": code, "status": "PLANNED", "reason_codes": ()})
    return AllocationResult(tuple(orders), tuple(statuses))


def _cash_balance(conn: Any, account_id: str) -> int:
    row = conn.execute(
        """
        SELECT balance_cents FROM paper_cash_ledger
        WHERE account_id = ? ORDER BY event_at DESC, rowid DESC LIMIT 1
        """,
        (account_id,),
    ).fetchone()
    if row is None:
        raise ContractError(f"paper account has no cash ledger: {account_id}")
    return int(row[0])


def _persist_order(conn: Any, order: PaperOrder) -> str:
    if order.side not in {"BUY", "SELL"} or order.quantity <= 0:
        raise ContractError("paper order side/quantity is invalid")
    existing = conn.execute("SELECT * FROM paper_orders WHERE order_id = ?", (order.order_id,)).fetchone()
    immutable = (
        order.run_id,
        order.account_id,
        order.code,
        order.side,
        int(order.quantity),
        _date(order.earliest_trade_date),
        str(order.price_ceiling_floor) if order.price_ceiling_floor is not None else None,
    )
    if existing is not None:
        stored = tuple(existing[key] for key in ("run_id", "account_id", "code", "side", "quantity", "earliest_trade_date", "price_ceiling_floor"))
        if stored != immutable:
            raise ContractError("paper order definition is immutable")
        status = str(existing["status"])
    else:
        conn.execute(
            """
            INSERT INTO paper_orders(
                order_id, run_id, account_id, code, side, quantity,
                earliest_trade_date, price_ceiling_floor, status, reason_codes_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
            """,
            (*((order.order_id,) + immutable), canonical_json(order.reason_codes)),
        )
        status = "PENDING"
    details = (
        str(order.reference_price),
        _date(order.planned_exit_date) or None,
        str(order.planned_stop_price) if order.planned_stop_price is not None else None,
        order.sector_id,
        canonical_json({}),
    )
    recorded = conn.execute(
        """
        SELECT reference_price, planned_exit_date, planned_stop_price,
               sector_id, payload_json
        FROM paper_order_details WHERE order_id = ?
        """,
        (order.order_id,),
    ).fetchone()
    if recorded is not None and tuple(recorded) != details:
        raise ContractError("paper order details are immutable")
    conn.execute(
        """
        INSERT OR IGNORE INTO paper_order_details(
            order_id, reference_price, planned_exit_date,
            planned_stop_price, sector_id, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (order.order_id, *details),
    )
    return status


def persist_orders(store: V2Store, orders: Sequence[PaperOrder]) -> int:
    inserted = 0
    with store._write_connection() as conn:
        for order in orders:
            existed = conn.execute(
                "SELECT 1 FROM paper_orders WHERE order_id = ?",
                (order.order_id,),
            ).fetchone()
            _persist_order(conn, order)
            inserted += int(existed is None)
    return inserted


def _set_order_status(conn: Any, order_id: str, status: str, reasons: Sequence[str]) -> None:
    conn.execute(
        "UPDATE paper_orders SET status = ?, reason_codes_json = ? WHERE order_id = ?",
        (status, canonical_json(tuple(reasons)), order_id),
    )


def _terminal_result(order: PaperOrder, status: str, reason: str | None = None) -> FillResult:
    reasons = () if reason is None else (reason,)
    return FillResult(order.order_id, status, 0, None, 0, reasons)


def _affordable_buy(
    requested_quantity: int,
    lot_size: int,
    fill_price: Decimal,
    cash_cents: int,
    fee_schedule: FeeSchedule,
) -> tuple[int, int, int, dict[str, int]]:
    high = max(0, requested_quantity // lot_size)
    low = 0
    while low < high:
        midpoint = (low + high + 1) // 2
        notional = fill_price * midpoint * lot_size
        notional_cents = _money_cents(notional)
        fee_cents, _ = fee_schedule.fees(notional, "BUY")
        if notional_cents + fee_cents <= cash_cents:
            low = midpoint
        else:
            high = midpoint - 1
    quantity = low * lot_size
    notional = fill_price * quantity
    notional_cents = _money_cents(notional)
    fee_cents, components = fee_schedule.fees(notional, "BUY")
    return quantity, notional_cents, fee_cents, components


def _execute_one(
    store: V2Store,
    order: PaperOrder,
    market: MarketOpen | None,
    fee_schedule: FeeSchedule,
    rule: SecurityRule | None,
) -> FillResult:
    with store._write_connection() as conn:
        existing_status = _persist_order(conn, order)
        fill = conn.execute("SELECT * FROM paper_fills WHERE order_id = ?", (order.order_id,)).fetchone()
        if fill is not None:
            return FillResult(
                order.order_id,
                "ALREADY_FILLED",
                int(fill["quantity"]),
                Decimal(str(fill["price"])),
                int(fill["fee_cents"]),
                (),
            )
        if existing_status.startswith("EXPIRED") or existing_status in {"RULE_METADATA_MISSING", "REJECTED"}:
            return _terminal_result(order, existing_status)
        if market is None:
            status = "PENDING_EXIT_NO_OPEN" if order.side == "SELL" else "EXPIRED_NO_OPEN"
            _set_order_status(conn, order.order_id, status, ("RAW_OPEN_UNAVAILABLE",))
            return _terminal_result(order, status, "RAW_OPEN_UNAVAILABLE")
        trade_date = _date(market.trade_date)
        if trade_date < _date(order.earliest_trade_date):
            _set_order_status(conn, order.order_id, "PENDING", ("BEFORE_EARLIEST_TRADE_DATE",))
            return _terminal_result(order, "PENDING", "BEFORE_EARLIEST_TRADE_DATE")
        if rule is None or not rule.applies(trade_date):
            _set_order_status(conn, order.order_id, "RULE_METADATA_MISSING", ("SECURITY_RULE_MISSING",))
            return _terminal_result(order, "RULE_METADATA_MISSING", "SECURITY_RULE_MISSING")
        if not fee_schedule.applies(trade_date):
            _set_order_status(conn, order.order_id, "FEE_SCHEDULE_MISSING", ("FEE_SCHEDULE_MISSING",))
            return _terminal_result(order, "FEE_SCHEDULE_MISSING", "FEE_SCHEDULE_MISSING")
        if market.is_suspended is not False:
            status = "PENDING_EXIT_SUSPENDED" if order.side == "SELL" else "EXPIRED_SUSPENDED"
            _set_order_status(conn, order.order_id, status, ("SUSPENDED_OR_UNKNOWN",))
            return _terminal_result(order, status, "SUSPENDED_OR_UNKNOWN")
        open_price = _decimal(market.raw_open)
        if open_price is None or open_price <= 0:
            status = "PENDING_EXIT_NO_OPEN" if order.side == "SELL" else "EXPIRED_NO_OPEN"
            _set_order_status(conn, order.order_id, status, ("RAW_OPEN_UNAVAILABLE",))
            return _terminal_result(order, status, "RAW_OPEN_UNAVAILABLE")
        if market.no_price_limit is not True:
            up_limit = _decimal(market.up_limit)
            down_limit = _decimal(market.down_limit)
            if market.no_price_limit is not False or up_limit is None or down_limit is None:
                status = "PENDING_EXIT_RULE_UNKNOWN" if order.side == "SELL" else "EXPIRED_RULE_UNKNOWN"
                _set_order_status(conn, order.order_id, status, ("PRICE_LIMIT_METADATA_MISSING",))
                return _terminal_result(order, status, "PRICE_LIMIT_METADATA_MISSING")
            if order.side == "BUY" and open_price >= up_limit:
                _set_order_status(conn, order.order_id, "EXPIRED_LIMIT_UP", ("OPEN_AT_UP_LIMIT",))
                return _terminal_result(order, "EXPIRED_LIMIT_UP", "OPEN_AT_UP_LIMIT")
            if order.side == "SELL" and open_price <= down_limit:
                _set_order_status(conn, order.order_id, "PENDING_EXIT_LIMIT_DOWN", ("OPEN_AT_DOWN_LIMIT",))
                return _terminal_result(order, "PENDING_EXIT_LIMIT_DOWN", "OPEN_AT_DOWN_LIMIT")
        multiplier = Decimal(1) + fee_schedule.slippage_each_side * (1 if order.side == "BUY" else -1)
        fill_price = _round_to_tick(open_price * multiplier, rule.tick_size, buy=order.side == "BUY")
        if order.side == "BUY" and order.price_ceiling_floor is not None and fill_price > order.price_ceiling_floor:
            _set_order_status(conn, order.order_id, "EXPIRED_PRICE_CEILING", ("PRICE_CEILING_EXCEEDED",))
            return _terminal_result(order, "EXPIRED_PRICE_CEILING", "PRICE_CEILING_EXCEEDED")

        quantity = int(order.quantity)
        if order.side == "BUY":
            cash = _cash_balance(conn, order.account_id)
            quantity, notional_cents, fee_cents, components = _affordable_buy(
                quantity,
                rule.lot_size,
                fill_price,
                cash,
                fee_schedule,
            )
            if quantity <= 0:
                _set_order_status(conn, order.order_id, "EXPIRED_INSUFFICIENT_CASH", ("INSUFFICIENT_CASH",))
                return _terminal_result(order, "EXPIRED_INSUFFICIENT_CASH", "INSUFFICIENT_CASH")
            cash_change = -(notional_cents + fee_cents)
        else:
            conn.execute(
                """
                UPDATE paper_lots SET sellable_quantity = quantity
                WHERE account_id = ? AND code = ? AND status = 'OPEN' AND entry_date < ?
                """,
                (order.account_id, order.code, trade_date),
            )
            lots = conn.execute(
                """
                SELECT * FROM paper_lots WHERE account_id = ? AND code = ?
                  AND status = 'OPEN' AND sellable_quantity > 0
                ORDER BY entry_date, lot_id
                """,
                (order.account_id, order.code),
            ).fetchall()
            available = sum(int(lot["sellable_quantity"]) for lot in lots)
            quantity = min(quantity, available)
            if quantity <= 0:
                _set_order_status(conn, order.order_id, "PENDING_EXIT_T1", ("NO_SELLABLE_QUANTITY",))
                return _terminal_result(order, "PENDING_EXIT_T1", "NO_SELLABLE_QUANTITY")
            notional = fill_price * quantity
            notional_cents = _money_cents(notional)
            fee_cents, components = fee_schedule.fees(notional, "SELL")
            cash_change = notional_cents - fee_cents

        fill_id = hashlib.sha256(f"fill|{order.order_id}".encode()).hexdigest()
        created_at = f"{trade_date}T00:00:00+08:00"
        conn.execute(
            """
            INSERT INTO paper_fills(
                fill_id, order_id, account_id, code, trade_date,
                side, quantity, price, fee_cents, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill_id,
                order.order_id,
                order.account_id,
                order.code,
                trade_date,
                order.side,
                quantity,
                str(fill_price),
                fee_cents,
                created_at,
            ),
        )
        old_cash = _cash_balance(conn, order.account_id)
        new_cash = old_cash + cash_change
        if new_cash < 0:
            raise ContractError("paper cash cannot become negative")
        conn.execute(
            """
            INSERT INTO paper_cash_ledger(
                entry_id, account_id, event_at, amount_cents,
                balance_cents, reason_code, reference_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"fill-cash:{fill_id}",
                order.account_id,
                created_at,
                cash_change,
                new_cash,
                f"{order.side}_FILL",
                fill_id,
            ),
        )
        if order.side == "BUY":
            metadata = {
                "entry_price": str(fill_price),
                "entry_fee_cents": fee_cents,
                "fee_components": components,
                "source_order_id": order.order_id,
                "planned_stop_price": str(order.planned_stop_price) if order.planned_stop_price is not None else None,
                "sector_id": order.sector_id,
                "realized_pnl_cents": 0,
            }
            lot_id = hashlib.sha256(f"lot|{order.order_id}".encode()).hexdigest()
            conn.execute(
                """
                INSERT INTO paper_lots(
                    lot_id, account_id, code, entry_date, planned_exit_date,
                    quantity, sellable_quantity, cost_cents, status, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'OPEN', ?)
                """,
                (
                    lot_id,
                    order.account_id,
                    order.code,
                    trade_date,
                    _date(order.planned_exit_date) or None,
                    quantity,
                    notional_cents + fee_cents,
                    canonical_json(metadata),
                ),
            )
        else:
            remaining = quantity
            gross_remaining = notional_cents
            fee_remaining = fee_cents
            for index, lot in enumerate(lots):
                if remaining <= 0:
                    break
                current_quantity = int(lot["quantity"])
                consume = min(remaining, int(lot["sellable_quantity"]))
                if consume <= 0:
                    continue
                if consume == remaining or index == len(lots) - 1:
                    gross_alloc = gross_remaining
                    fee_alloc = fee_remaining
                else:
                    gross_alloc = round(notional_cents * consume / quantity)
                    fee_alloc = round(fee_cents * consume / quantity)
                cost_alloc = (
                    int(lot["cost_cents"])
                    if consume == current_quantity
                    else round(int(lot["cost_cents"]) * consume / current_quantity)
                )
                new_quantity = current_quantity - consume
                new_cost = int(lot["cost_cents"]) - cost_alloc
                metadata = json.loads(lot["metadata_json"] or "{}")
                metadata["realized_pnl_cents"] = int(metadata.get("realized_pnl_cents", 0)) + (
                    gross_alloc - fee_alloc - cost_alloc
                )
                if new_quantity == 0:
                    metadata["closed_at"] = trade_date
                conn.execute(
                    """
                    UPDATE paper_lots SET quantity = ?, sellable_quantity = ?,
                        cost_cents = ?, status = ?, metadata_json = ? WHERE lot_id = ?
                    """,
                    (
                        new_quantity,
                        max(0, int(lot["sellable_quantity"]) - consume),
                        new_cost,
                        "CLOSED" if new_quantity == 0 else "OPEN",
                        canonical_json(metadata),
                        lot["lot_id"],
                    ),
                )
                remaining -= consume
                gross_remaining -= gross_alloc
                fee_remaining -= fee_alloc
        status = "FILLED" if quantity == order.quantity else "PARTIAL_FILLED"
        _set_order_status(conn, order.order_id, status, ("OPEN_FILL_APPROXIMATION",))
        return FillResult(
            order.order_id,
            status,
            quantity,
            fill_price,
            fee_cents,
            ("OPEN_FILL_APPROXIMATION",),
        )


def execute_open_orders(
    store: V2Store,
    orders: Sequence[PaperOrder],
    open_market: Mapping[str, MarketOpen],
    *,
    fee_schedule: FeeSchedule,
    security_rules: Mapping[str, SecurityRule],
) -> ExecutionBatchResult:
    ordered = sorted(orders, key=lambda item: (0 if item.side == "SELL" else 1, item.code, item.order_id))
    results = [
        _execute_one(store, order, open_market.get(order.code), fee_schedule, security_rules.get(order.code))
        for order in ordered
    ]
    return ExecutionBatchResult(tuple(results))
