from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import pandas as pd

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


def _money_cents(value_cny: Decimal) -> int:
    return int((value_cny * Decimal(100)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class PaperLot:
    lot_id: str
    account_id: str
    code: str
    entry_date: str
    planned_exit_date: str | None
    quantity: int
    stored_sellable_quantity: int
    cost_cents: int
    status: str
    metadata: dict[str, Any]

    def sellable_quantity(self, session: str) -> int:
        if self.status != "OPEN" or self.quantity <= 0:
            return 0
        if _date(session) > _date(self.entry_date):
            return self.quantity
        return min(self.quantity, self.stored_sellable_quantity)


@dataclass(frozen=True)
class CorporateActionResult:
    applied_events: int
    unresolved_events: int
    statuses: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class PortfolioMark:
    account_id: str
    trade_date: str
    status: str
    nav_cents: int | None
    known_value_cents: int
    cash_cents: int
    position_value_cents: int
    unresolved_codes: tuple[str, ...]
    sector_exposure: dict[str, float]
    receivable_cents: int = 0
    share_receivable_cents: int = 0


class PaperPortfolio:
    def __init__(self, store: V2Store, account_id: str) -> None:
        self.store = store
        self.account_id = str(account_id)

    def open_account(
        self,
        *,
        method: str,
        initial_cash_cents: int,
        account_type: str = "paper",
        initial_event_at: str | None = None,
    ) -> None:
        self.store.create_paper_account(
            self.account_id,
            method=method,
            initial_cash_cents=initial_cash_cents,
            account_type=account_type,
            initial_event_at=initial_event_at,
        )

    def cash_cents(self) -> int:
        return self.store.paper_cash_balance(self.account_id)

    def lots(self) -> list[PaperLot]:
        rows = self.store.read_paper_lots(self.account_id)
        result: list[PaperLot] = []
        for row in rows.to_dict(orient="records"):
            try:
                metadata = json.loads(row.get("metadata_json") or "{}")
            except json.JSONDecodeError as exc:
                raise ContractError(f"invalid paper lot metadata: {row.get('lot_id')}") from exc
            result.append(
                PaperLot(
                    lot_id=str(row["lot_id"]),
                    account_id=str(row["account_id"]),
                    code=str(row["code"]),
                    entry_date=str(row["entry_date"]),
                    planned_exit_date=row.get("planned_exit_date"),
                    quantity=int(row["quantity"]),
                    stored_sellable_quantity=int(row["sellable_quantity"]),
                    cost_cents=int(row["cost_cents"]),
                    status=str(row["status"]),
                    metadata=metadata,
                )
            )
        return result

    def closed_trades(self) -> pd.DataFrame:
        rows, groups = [], defaultdict(list)
        for lot in self.lots():
            groups[str(lot.metadata.get("economic_lot_id") or lot.lot_id)].append(lot)
        for root_id, members in groups.items():
            if any(lot.status != "CLOSED" for lot in members) or pending_share_quantities(members):
                continue
            root = next((lot for lot in members if lot.lot_id == root_id), members[0])
            last = max(members, key=lambda lot: (str(lot.metadata.get("closed_at") or ""), lot.lot_id))
            dividends = sum(int(ent.get("cash_cents", 0)) for lot in members
                            for ent in lot.metadata.get("corporate_action_entitlements", {}).values())
            rows.append({"lot_id": root_id, "code": root.code, "status": "CLOSED",
                "entry_date": root.entry_date, "closed_at": last.metadata.get("closed_at"),
                "realized_pnl_cents": sum(int(lot.metadata.get("realized_pnl_cents", 0)) for lot in members) + dividends,
                "entry_quantity": root.metadata.get("entry_quantity"), "entry_cost_cents": root.metadata.get("entry_cost_cents"),
                "sector_id": root.metadata.get("entry_sector_id", root.metadata.get("sector_id")),
                "entry_regime": root.metadata.get("entry_regime"), "exit_reason": last.metadata.get("exit_reason"),
                "confirmed_cash_dividend_cents": dividends, "member_lot_ids": tuple(lot.lot_id for lot in members)})
        return pd.DataFrame(rows, columns=["lot_id", "code", "status", "entry_date", "closed_at",
            "realized_pnl_cents", "entry_quantity", "entry_cost_cents", "sector_id", "entry_regime",
            "exit_reason", "confirmed_cash_dividend_cents", "member_lot_ids"])

    def corporate_action_ledger(self) -> pd.DataFrame:
        return self.store.read_paper_corporate_action_ledger(self.account_id)


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


def _event_id(event: Mapping[str, Any]) -> str:
    supplied = str(event.get("event_id") or "").strip()
    if supplied:
        return supplied
    identity = canonical_json(
        {
            key: event.get(key)
            for key in (
                "code",
                "record_date",
                "ex_date",
                "pay_date",
                "list_date",
                "cash_per_share",
                "share_ratio",
                "split_ratio",
            )
        }
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def pending_share_quantities(lots: Sequence[PaperLot], as_of_date: str | None = None) -> dict[str, int]:
    quantities: dict[str, int] = defaultdict(int)
    for lot in lots:
        for entitlement in lot.metadata.get("corporate_action_entitlements", {}).values():
            if not entitlement["shares_listed"] and (as_of_date is None or _date(as_of_date) >= entitlement["ex_date"]):
                quantities[lot.code] += int(entitlement["share_quantity"])
    return {code: quantity for code, quantity in quantities.items() if quantity > 0}


def _record_quantity(lot: Any, record_date: str) -> int | None:
    metadata = json.loads(lot["metadata_json"] or "{}")
    if metadata.get("quantity_history_complete"):
        return sum(int(event["quantity_delta"]) for event in metadata["quantity_events"] if event["date"] <= record_date)
    if int(metadata.get("realized_pnl_cents", 0)) != 0 or lot["status"] == "CLOSED":
        return None
    return int(lot["quantity"])


def _allocate_entitlement(total: int, quantities: list[int]) -> list[int]:
    denominator = sum(quantities)
    amounts = [total * quantity // denominator for quantity in quantities]
    priority = sorted(range(len(quantities)), key=lambda index: (-(total * quantities[index] % denominator), index))
    for index in priority[:total - sum(amounts)]:
        amounts[index] += 1
    return amounts


def apply_corporate_actions(
    store: V2Store,
    account_id: str,
    events: Sequence[Mapping[str, Any]],
    *,
    as_of_date: str,
) -> CorporateActionResult:
    cutoff = _date(as_of_date)
    applied = 0
    unresolved = 0
    statuses: list[dict[str, Any]] = []
    for event in sorted(events, key=lambda item: (_date(item.get("record_date")), _event_id(item))):
        event_id = _event_id(event)
        code = str(event.get("code") or "")
        record_date = _date(event.get("record_date"))
        pay_date = _date(event.get("pay_date"))
        list_date = _date(event.get("list_date"))
        cash_per_share = _decimal(event.get("cash_per_share"))
        share_ratio = _decimal(event.get("share_ratio"))
        split_ratio = _decimal(event.get("split_ratio"))
        cash_component = cash_per_share is not None and cash_per_share > 0
        share_component = (share_ratio is not None and share_ratio > 0) or (
            split_ratio is not None and split_ratio > 1
        )
        missing_contract = (
            not code
            or not record_date
            or record_date > cutoff
            or (not cash_component and not share_component)
            or (cash_component and not pay_date)
            or (share_component and not list_date)
        )
        ledger_id = hashlib.sha256(f"action|{account_id}|{event_id}".encode()).hexdigest()
        with store._write_connection() as conn:
            existing = conn.execute(
                "SELECT * FROM paper_corporate_action_ledger WHERE ledger_id = ?",
                (ledger_id,),
            ).fetchone()
            if existing is not None and str(existing["status"]) == "SETTLED":
                statuses.append({"event_id": event_id, "status": "ALREADY_SETTLED"})
                continue
            if existing is not None and str(existing["status"]) == "UNRESOLVED":
                unresolved += 1
                statuses.append({"event_id": event_id, "status": "UNRESOLVED"})
                continue
            lots = conn.execute(
                """
                SELECT * FROM paper_lots WHERE account_id = ? AND code = ?
                  AND entry_date <= ?
                ORDER BY entry_date, lot_id
                """,
                (str(account_id), code, record_date),
            ).fetchall()
            quantities = [_record_quantity(lot, record_date) for lot in lots]
            uncertain_history = existing is None and any(quantity is None for quantity in quantities)
            eligible_quantity = sum(quantity or 0 for quantity in quantities)
            if (missing_contract or uncertain_history) and record_date <= cutoff and (eligible_quantity > 0 or uncertain_history):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO paper_corporate_action_ledger(
                        ledger_id, account_id, event_id, code, status,
                        receivable_cash_cents, received_cash_cents, quantity_delta,
                        effective_date, settled_at
                    ) VALUES (?, ?, ?, ?, 'UNRESOLVED', NULL, NULL, NULL, ?, NULL)
                    """,
                    (ledger_id, str(account_id), event_id, code, record_date),
                )
                unresolved += 1
                statuses.append({"event_id": event_id, "status": "UNRESOLVED",
                                 "reason": "LEGACY_LOT_ENTITLEMENT_HISTORY_UNKNOWN" if uncertain_history else "ACTION_CONTRACT_INCOMPLETE"})
                continue
            if record_date > cutoff or (eligible_quantity <= 0 and existing is None):
                statuses.append({"event_id": event_id, "status": "NOT_APPLICABLE"})
                continue
            if existing is None:
                receivable = _money_cents(cash_per_share * eligible_quantity) if cash_component else None
                if split_ratio is not None and split_ratio > 1:
                    quantity_delta = int(
                        (Decimal(eligible_quantity) * (split_ratio - Decimal(1))).to_integral_value(
                            rounding=ROUND_FLOOR
                        )
                    )
                    adjustment_factor = split_ratio
                elif share_ratio is not None:
                    quantity_delta = int(
                        (Decimal(eligible_quantity) * share_ratio).to_integral_value(rounding=ROUND_FLOOR)
                    )
                    adjustment_factor = Decimal(1) + share_ratio
                else:
                    quantity_delta = None
                    adjustment_factor = Decimal(1)
                conn.execute(
                    """
                    INSERT INTO paper_corporate_action_ledger(
                        ledger_id, account_id, event_id, code, status,
                        receivable_cash_cents, received_cash_cents, quantity_delta,
                        effective_date, settled_at
                    ) VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, NULL)
                    """,
                    (
                        ledger_id,
                        str(account_id),
                        event_id,
                        code,
                        receivable,
                        0 if cash_component else None,
                        quantity_delta,
                        record_date,
                    ),
                )
                existing = conn.execute(
                    "SELECT * FROM paper_corporate_action_ledger WHERE ledger_id = ?", (ledger_id,)
                ).fetchone()
                shares = _allocate_entitlement(int(quantity_delta or 0), [quantity or 0 for quantity in quantities])
                cash = _allocate_entitlement(int(receivable or 0), [quantity or 0 for quantity in quantities])
                for lot, quantity, cash_amount, share_amount in zip(lots, quantities, cash, shares):
                    if not quantity:
                        continue
                    metadata = json.loads(lot["metadata_json"] or "{}")
                    metadata.setdefault("corporate_action_entitlements", {})[event_id] = {
                        "record_quantity": quantity, "cash_cents": cash_amount, "share_quantity": share_amount,
                        "ex_date": _date(event.get("ex_date")) or list_date or pay_date or record_date,
                        "list_date": list_date, "shares_listed": not share_component or share_amount == 0,
                    }
                    conn.execute("UPDATE paper_lots SET metadata_json=? WHERE lot_id=?",
                                 (canonical_json(metadata), lot["lot_id"]))
            else:
                quantity_delta = int(existing["quantity_delta"]) if existing["quantity_delta"] is not None else None
                adjustment_factor = (
                    split_ratio
                    if split_ratio is not None and split_ratio > 1
                    else Decimal(1) + share_ratio
                    if share_ratio is not None
                    else Decimal(1)
                )
            changed = False
            receivable = existing["receivable_cash_cents"]
            received = existing["received_cash_cents"]
            if cash_component and pay_date <= cutoff and int(received or 0) < int(receivable or 0):
                cash_entry_id = f"corp-cash:{ledger_id}"
                inserted = conn.execute(
                    """
                    INSERT OR IGNORE INTO paper_cash_ledger(
                        entry_id, account_id, event_at, amount_cents,
                        balance_cents, reason_code, reference_id
                    ) VALUES (?, ?, ?, ?, ?, 'CASH_DIVIDEND', ?)
                    """,
                    (
                        cash_entry_id,
                        str(account_id),
                        f"{pay_date}T00:00:00+08:00",
                        int(receivable),
                        _cash_balance(conn, str(account_id)) + int(receivable),
                        event_id,
                    ),
                ).rowcount
                if inserted:
                    changed = True
                conn.execute(
                    "UPDATE paper_corporate_action_ledger SET received_cash_cents = ? WHERE ledger_id = ?",
                    (int(receivable), ledger_id),
                )
                received = receivable
            shares_done = not share_component
            if share_component:
                shares_done = list_date <= cutoff
            for lot in lots:
                current = conn.execute("SELECT metadata_json FROM paper_lots WHERE lot_id=?", (lot["lot_id"],)).fetchone()
                metadata = json.loads(current[0] or "{}")
                entitlement = metadata.get("corporate_action_entitlements", {}).get(event_id)
                if entitlement is None:
                    continue
                applied_prices = metadata.setdefault("applied_price_actions", {})
                if entitlement["ex_date"] <= cutoff and event_id not in applied_prices:
                    stop = _decimal(metadata.get("planned_stop_price"))
                    if stop is not None:
                        adjusted = (stop - (cash_per_share if cash_component else Decimal(0))) / adjustment_factor
                        metadata["planned_stop_price"] = str(adjusted) if adjusted > 0 else None
                    # Comparison stops/highs already use raw * contemporaneous adj_factor.
                    applied_prices[event_id] = True
                share_quantity = int(entitlement["share_quantity"])
                if share_component and list_date <= cutoff and share_quantity > 0 and not entitlement["shares_listed"]:
                    child_id = hashlib.sha256(f"corp-lot|{account_id}|{event_id}|{lot['lot_id']}".encode()).hexdigest()
                    excluded = {"closed_at", "realized_pnl_cents", "corporate_action_entitlements", "exit_reason",
                                "quantity_events", "entry_quantity", "entry_cost_cents", "entry_fee_cents"}
                    child_metadata = {key: value for key, value in metadata.items() if key not in excluded}
                    child_metadata.update(corporate_action_event_id=event_id, entitlement_record_date=record_date,
                        economic_parent_lot_id=lot["lot_id"], economic_lot_id=metadata.get("economic_lot_id", lot["lot_id"]),
                        realized_pnl_cents=0, entry_quantity=share_quantity, entry_cost_cents=0,
                        quantity_events=[{"date": list_date, "quantity_delta": share_quantity}], quantity_history_complete=True)
                    inserted = conn.execute(
                        "INSERT OR IGNORE INTO paper_lots(lot_id,account_id,code,entry_date,planned_exit_date,"
                        "quantity,sellable_quantity,cost_cents,status,metadata_json) VALUES (?,?,?,?,?,?,?,0,'OPEN',?)",
                        (child_id, str(account_id), code, list_date, lot["planned_exit_date"], share_quantity,
                         share_quantity, canonical_json(child_metadata))).rowcount
                    entitlement["shares_listed"] = True
                    changed |= bool(inserted)
                if share_component and not entitlement["shares_listed"]:
                    shares_done = False
                conn.execute("UPDATE paper_lots SET metadata_json=? WHERE lot_id=?",
                             (canonical_json(metadata), lot["lot_id"]))
            cash_done = not cash_component or (pay_date <= cutoff and int(received or 0) == int(receivable or 0))
            status = "SETTLED" if cash_done and shares_done else "PENDING"
            conn.execute(
                """
                UPDATE paper_corporate_action_ledger
                SET status = ?, settled_at = ? WHERE ledger_id = ?
                """,
                (status, f"{cutoff}T00:00:00+08:00" if status == "SETTLED" else None, ledger_id),
            )
            if changed:
                applied += 1
            statuses.append({"event_id": event_id, "status": status})
    return CorporateActionResult(applied, unresolved, tuple(statuses))


def mark_portfolio(
    store: V2Store,
    account_id: str,
    trade_date: str,
    market_prices: Mapping[str, Mapping[str, Any] | Decimal | str | float],
) -> PortfolioMark:
    cash = store.paper_cash_balance(account_id)
    lots = store.read_paper_lots(account_id)
    open_lots = lots.loc[lots["status"].eq("OPEN") & pd.to_numeric(lots["quantity"], errors="coerce").gt(0)]
    by_code = open_lots.groupby("code", sort=True)["quantity"].sum() if not open_lots.empty else pd.Series(dtype=float)
    position_value = 0
    unresolved_codes: list[str] = []
    sector_values: dict[str, int] = {}
    for code, quantity_raw in by_code.items():
        quote = market_prices.get(str(code))
        if isinstance(quote, Mapping):
            price = _decimal(quote.get("price"))
        else:
            price = _decimal(quote)
        if price is None or price <= 0:
            unresolved_codes.append(str(code))
            continue
        value = _money_cents(price * int(quantity_raw))
        position_value += value
        code_lots = open_lots.loc[open_lots["code"].eq(code)]
        sectors = []
        for raw in code_lots["metadata_json"]:
            metadata = json.loads(raw or "{}")
            sectors.append(str(metadata.get("sector_id") or "UNKNOWN"))
        sector = min(sectors) if sectors else "UNKNOWN"
        sector_values[sector] = sector_values.get(sector, 0) + value
    action_ledger = store.read_paper_corporate_action_ledger(account_id)
    unresolved_actions = (
        not action_ledger.empty and action_ledger["status"].astype(str).eq("UNRESOLVED").any()
    )
    receivable_cents = 0
    if not action_ledger.empty:
        pending = action_ledger.loc[action_ledger["status"].eq("PENDING")]
        for row in pending.to_dict(orient="records"):
            confirmed = row.get("receivable_cash_cents")
            received = row.get("received_cash_cents")
            if pd.notna(confirmed):
                receivable_cents += max(0, int(confirmed) - (int(received) if pd.notna(received) else 0))
    share_receivable = 0
    for code, quantity in pending_share_quantities(PaperPortfolio(store, account_id).lots(), _date(trade_date)).items():
        quote = market_prices.get(code)
        price = _decimal(quote.get("price") if isinstance(quote, Mapping) else quote)
        if price is None or price <= 0:
            unresolved_codes.append(code)
        else:
            value = _money_cents(price * quantity)
            share_receivable += value
            code_lots = lots.loc[lots.code.eq(code)]
            sector = next((str(json.loads(raw).get("sector_id") or "UNKNOWN") for raw in code_lots.metadata_json), "UNKNOWN")
            sector_values[sector] = sector_values.get(sector, 0) + value
    receivable_cents += share_receivable
    known_value = cash + position_value + receivable_cents
    if unresolved_codes:
        status = "NAV_UNRESOLVED_VALUATION"
        nav = None
    elif unresolved_actions:
        status = "NAV_UNRESOLVED_CORPORATE_ACTION"
        nav = None
    else:
        status = "OK"
        nav = known_value
    exposure = {
        sector: value / known_value
        for sector, value in sorted(sector_values.items())
        if known_value > 0
    }
    return PortfolioMark(
        account_id=str(account_id),
        trade_date=_date(trade_date),
        status=status,
        nav_cents=nav,
        known_value_cents=known_value,
        cash_cents=cash,
        position_value_cents=position_value,
        unresolved_codes=tuple(unresolved_codes),
        sector_exposure=exposure,
        receivable_cents=receivable_cents,
        share_receivable_cents=share_receivable,
    )
