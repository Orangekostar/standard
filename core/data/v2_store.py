from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import pandas as pd

from core.technical_v2.contracts import ContractError, canonical_json

SCHEMA_VERSION = 1
REQUIRED_TABLES = {
    "schema_migrations",
    "instrument_versions",
    "calendar",
    "daily_raw",
    "adjustments",
    "trading_status",
    "sector_membership",
    "corporate_actions",
    "analysis_runs",
    "feature_rows",
    "prediction_rows",
    "model_registry",
    "paper_accounts",
    "paper_cash_ledger",
    "paper_lots",
    "paper_orders",
    "paper_fills",
    "paper_corporate_action_ledger",
}


_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    summary TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instrument_versions (
    code TEXT NOT NULL,
    instrument_type TEXT NOT NULL,
    exchange TEXT NOT NULL,
    listing_board TEXT,
    name TEXT,
    list_date TEXT,
    delist_date TEXT,
    listing_status TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    source TEXT NOT NULL,
    source_version TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (code, valid_from, source_version)
);

CREATE TABLE IF NOT EXISTS calendar (
    exchange TEXT NOT NULL,
    date TEXT NOT NULL,
    is_open INTEGER NOT NULL CHECK (is_open IN (0, 1)),
    previous_session TEXT,
    next_session TEXT,
    source TEXT NOT NULL,
    source_version TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (exchange, date, source_version)
);

CREATE TABLE IF NOT EXISTS daily_raw (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    source_version TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    pre_close REAL,
    pct_chg REAL,
    volume_raw REAL,
    volume_raw_unit TEXT,
    amount_raw REAL,
    amount_raw_unit TEXT,
    volume_shares REAL,
    amount_cny REAL,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    completeness TEXT NOT NULL,
    data_source_mode TEXT NOT NULL,
    PRIMARY KEY (code, date, source_version)
);

CREATE TABLE IF NOT EXISTS adjustments (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    source_version TEXT NOT NULL,
    adj_factor REAL,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (code, date, source_version)
);

CREATE TABLE IF NOT EXISTS trading_status (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    source_version TEXT NOT NULL,
    is_suspended INTEGER,
    is_risk_warning INTEGER,
    up_limit REAL,
    down_limit REAL,
    no_price_limit INTEGER,
    rule_version TEXT,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (code, date, source_version)
);

CREATE TABLE IF NOT EXISTS sector_membership (
    namespace TEXT NOT NULL,
    sector_id TEXT NOT NULL,
    code TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    observed_at TEXT NOT NULL,
    history_mode TEXT NOT NULL,
    source TEXT NOT NULL,
    source_version TEXT NOT NULL,
    PRIMARY KEY (namespace, sector_id, code, valid_from, source_version)
);

CREATE TABLE IF NOT EXISTS corporate_actions (
    event_id TEXT NOT NULL,
    code TEXT NOT NULL,
    event_type TEXT NOT NULL,
    record_date TEXT,
    ex_date TEXT,
    pay_date TEXT,
    list_date TEXT,
    cash_per_share TEXT,
    share_ratio TEXT,
    split_ratio TEXT,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    source_version TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (event_id, source_version)
);

CREATE TABLE IF NOT EXISTS analysis_runs (
    run_id TEXT PRIMARY KEY,
    as_of_trade_date TEXT NOT NULL,
    information_cutoff TEXT,
    generated_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    data_source_mode TEXT NOT NULL,
    code_hash TEXT,
    config_hash TEXT,
    data_hash TEXT,
    status TEXT NOT NULL,
    coverage REAL,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS feature_rows (
    run_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    as_of_trade_date TEXT NOT NULL,
    factor_id TEXT NOT NULL,
    value REAL,
    status TEXT NOT NULL,
    reason_code TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, entity_type, entity_id, factor_id)
);

CREATE TABLE IF NOT EXISTS prediction_rows (
    run_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    method TEXT NOT NULL,
    prediction_status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    first_recorded_at TEXT NOT NULL,
    PRIMARY KEY (run_id, entity_type, entity_id, horizon, method)
);

CREATE TABLE IF NOT EXISTS model_registry (
    method TEXT NOT NULL,
    config_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    model_id TEXT,
    calibration_id TEXT,
    train_start TEXT,
    train_end TEXT,
    calibration_start TEXT,
    calibration_end TEXT,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (method, config_id, entity_type, horizon)
);

CREATE TABLE IF NOT EXISTS paper_accounts (
    account_id TEXT PRIMARY KEY,
    method TEXT NOT NULL,
    account_type TEXT NOT NULL,
    initial_cash_cents INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_cash_ledger (
    entry_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    event_at TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    balance_cents INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    reference_id TEXT
);

CREATE TABLE IF NOT EXISTS paper_lots (
    lot_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    code TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    planned_exit_date TEXT,
    quantity INTEGER NOT NULL,
    sellable_quantity INTEGER NOT NULL,
    cost_cents INTEGER NOT NULL,
    status TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    code TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    earliest_trade_date TEXT NOT NULL,
    price_ceiling_floor TEXT,
    status TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price TEXT NOT NULL,
    fee_cents INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_corporate_action_ledger (
    ledger_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    code TEXT NOT NULL,
    status TEXT NOT NULL,
    receivable_cash_cents INTEGER,
    received_cash_cents INTEGER,
    quantity_delta INTEGER,
    effective_date TEXT,
    settled_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_daily_raw_date ON daily_raw(date, code);
CREATE INDEX IF NOT EXISTS idx_calendar_open ON calendar(exchange, is_open, date);
CREATE INDEX IF NOT EXISTS idx_sector_membership_code ON sector_membership(code, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_prediction_run ON prediction_rows(run_id, method, horizon);
"""


class V2Store:
    def __init__(self, path: str | Path, data_mode: str = "real") -> None:
        mode = str(data_mode or "").strip().lower()
        if mode not in {"real", "demo", "test"}:
            raise ContractError(f"unsupported data mode: {data_mode}")
        self.path = Path(path)
        self.data_mode = mode

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def migrate(self) -> int:
        with closing(self._connect()) as conn:
            conn.executescript(
                "BEGIN IMMEDIATE;\n"
                + _MIGRATION_1
                + "\nINSERT OR IGNORE INTO schema_migrations(version, applied_at, summary) "
                "VALUES (1, CURRENT_TIMESTAMP, 'Initial Technical V2 schema');\nCOMMIT;"
            )
        return SCHEMA_VERSION

    @contextmanager
    def _write_connection(self):
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def upsert_daily_raw(self, frame: pd.DataFrame) -> int:
        columns = [
            "code",
            "date",
            "source_version",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "pct_chg",
            "volume_raw",
            "volume_raw_unit",
            "amount_raw",
            "amount_raw_unit",
            "volume_shares",
            "amount_cny",
            "source",
            "retrieved_at",
            "completeness",
            "data_source_mode",
        ]
        if frame is not None and not frame.empty:
            modes = set(frame["data_source_mode"].dropna().astype(str).str.lower()) if "data_source_mode" in frame.columns else set()
            if modes != {self.data_mode}:
                raise ContractError(
                    f"daily rows data mode {sorted(modes)} does not match store mode {self.data_mode}"
                )
        rows = _frame_records(frame, columns)
        if not rows:
            return 0
        sql = f"""
            INSERT INTO daily_raw ({','.join(columns)})
            VALUES ({','.join('?' for _ in columns)})
            ON CONFLICT(code, date, source_version) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, pre_close=excluded.pre_close,
                pct_chg=excluded.pct_chg, volume_raw=excluded.volume_raw,
                volume_raw_unit=excluded.volume_raw_unit,
                amount_raw=excluded.amount_raw, amount_raw_unit=excluded.amount_raw_unit,
                volume_shares=excluded.volume_shares, amount_cny=excluded.amount_cny,
                source=excluded.source, retrieved_at=excluded.retrieved_at,
                completeness=excluded.completeness, data_source_mode=excluded.data_source_mode
        """
        with self._write_connection() as conn:
            conn.executemany(sql, rows)
        return len(rows)

    def upsert_instrument_versions(self, frame: pd.DataFrame) -> int:
        columns = [
            "code",
            "instrument_type",
            "exchange",
            "listing_board",
            "name",
            "list_date",
            "delist_date",
            "listing_status",
            "valid_from",
            "valid_to",
            "source",
            "source_version",
            "observed_at",
        ]
        update_columns = [column for column in columns if column not in {"code", "valid_from", "source_version"}]
        return self._upsert_frame(
            "instrument_versions",
            frame,
            columns,
            conflict_columns=("code", "valid_from", "source_version"),
            update_columns=update_columns,
        )

    def read_instrument_versions(self, as_of: str) -> pd.DataFrame:
        target = str(as_of).replace("-", "")
        with closing(self._connect()) as conn:
            frame = pd.read_sql_query(
                """
                SELECT * FROM instrument_versions
                WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to = '' OR valid_to >= ?)
                ORDER BY code, observed_at, source_version
                """,
                conn,
                params=[target, target],
            )
        if frame.empty:
            return frame
        return frame.drop_duplicates("code", keep="last").reset_index(drop=True)

    def upsert_sector_membership(self, frame: pd.DataFrame) -> int:
        columns = [
            "namespace",
            "sector_id",
            "code",
            "valid_from",
            "valid_to",
            "observed_at",
            "history_mode",
            "source",
            "source_version",
        ]
        update_columns = [column for column in columns if column not in {"namespace", "sector_id", "code", "valid_from", "source_version"}]
        return self._upsert_frame(
            "sector_membership",
            frame,
            columns,
            conflict_columns=("namespace", "sector_id", "code", "valid_from", "source_version"),
            update_columns=update_columns,
        )

    def read_sector_membership(self, as_of: str, namespace: str | None = None) -> pd.DataFrame:
        target = str(as_of).replace("-", "")
        params: list[Any] = [target, target]
        namespace_clause = ""
        if namespace:
            namespace_clause = " AND namespace = ?"
            params.append(str(namespace))
        with closing(self._connect()) as conn:
            return pd.read_sql_query(
                f"""
                SELECT * FROM sector_membership
                WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to = '' OR valid_to >= ?)
                {namespace_clause}
                ORDER BY namespace, sector_id, code
                """,
                conn,
                params=params,
            )

    def upsert_adjustments(self, frame: pd.DataFrame) -> int:
        columns = ["code", "date", "source_version", "adj_factor", "source", "retrieved_at"]
        return self._upsert_frame(
            "adjustments",
            frame,
            columns,
            conflict_columns=("code", "date", "source_version"),
            update_columns=("adj_factor", "source", "retrieved_at"),
        )

    def read_adjustments(
        self,
        start_date: str,
        end_date: str,
        codes: Iterable[str] | None = None,
    ) -> pd.DataFrame:
        params: list[Any] = [str(start_date), str(end_date)]
        code_values = [str(code) for code in (codes or [])]
        code_clause = ""
        if code_values:
            code_clause = f" AND code IN ({','.join('?' for _ in code_values)})"
            params.extend(code_values)
        query = (
            "SELECT * FROM adjustments WHERE date >= ? AND date <= ?"
            f"{code_clause} ORDER BY code, date, retrieved_at, source_version"
        )
        with closing(self._connect()) as conn:
            frame = pd.read_sql_query(query, conn, params=params)
        if frame.empty:
            return frame
        return frame.drop_duplicates(["code", "date"], keep="last").reset_index(drop=True)

    def upsert_trading_status(self, frame: pd.DataFrame) -> int:
        columns = [
            "code",
            "date",
            "source_version",
            "is_suspended",
            "is_risk_warning",
            "up_limit",
            "down_limit",
            "no_price_limit",
            "rule_version",
            "source",
            "retrieved_at",
        ]
        return self._upsert_frame(
            "trading_status",
            frame,
            columns,
            conflict_columns=("code", "date", "source_version"),
            update_columns=tuple(column for column in columns if column not in {"code", "date", "source_version"}),
        )

    def upsert_corporate_actions(self, frame: pd.DataFrame) -> int:
        columns = [
            "event_id",
            "code",
            "event_type",
            "record_date",
            "ex_date",
            "pay_date",
            "list_date",
            "cash_per_share",
            "share_ratio",
            "split_ratio",
            "status",
            "source",
            "source_version",
            "retrieved_at",
        ]
        return self._upsert_frame(
            "corporate_actions",
            frame,
            columns,
            conflict_columns=("event_id", "source_version"),
            update_columns=tuple(column for column in columns if column not in {"event_id", "source_version"}),
        )

    def read_corporate_actions(self, code: str | None = None) -> pd.DataFrame:
        query = "SELECT * FROM corporate_actions"
        params: list[Any] = []
        if code:
            query += " WHERE code = ?"
            params.append(str(code))
        query += " ORDER BY code, COALESCE(ex_date, record_date), event_id"
        with closing(self._connect()) as conn:
            return pd.read_sql_query(query, conn, params=params)

    def read_daily_raw(self, start_date: str, end_date: str, codes: Iterable[str] | None = None) -> pd.DataFrame:
        params: list[Any] = [str(start_date), str(end_date)]
        code_values = [str(code) for code in (codes or [])]
        code_clause = ""
        if code_values:
            code_clause = f" AND code IN ({','.join('?' for _ in code_values)})"
            params.extend(code_values)
        query = f"SELECT * FROM daily_raw WHERE date >= ? AND date <= ?{code_clause} ORDER BY code, date"
        with closing(self._connect()) as conn:
            return pd.read_sql_query(query, conn, params=params)

    def upsert_calendar(self, frame: pd.DataFrame) -> int:
        if frame is None or frame.empty:
            return 0
        work = frame.copy()
        work["date"] = work["date"].astype(str).str.replace("-", "", regex=False)
        work["is_open"] = pd.to_numeric(work["is_open"], errors="raise").astype(int)
        for column, default in {
            "previous_session": None,
            "next_session": None,
            "retrieved_at": pd.Timestamp.now(tz="UTC").isoformat(),
        }.items():
            if column not in work.columns:
                work[column] = default
        columns = [
            "exchange",
            "date",
            "is_open",
            "previous_session",
            "next_session",
            "source",
            "source_version",
            "retrieved_at",
        ]
        rows = _frame_records(work, columns)
        sql = f"""
            INSERT INTO calendar ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})
            ON CONFLICT(exchange, date, source_version) DO UPDATE SET
                is_open=excluded.is_open, previous_session=excluded.previous_session,
                next_session=excluded.next_session, source=excluded.source,
                retrieved_at=excluded.retrieved_at
        """
        with self._write_connection() as conn:
            conn.executemany(sql, rows)
        return len(rows)

    def open_sessions(self, exchange: str, end_date: str, limit: int) -> list[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT date FROM calendar
                WHERE exchange = ? AND is_open = 1 AND date <= ?
                ORDER BY date DESC LIMIT ?
                """,
                (str(exchange), str(end_date).replace("-", ""), max(1, int(limit))),
            ).fetchall()
        return sorted(str(row[0]) for row in rows)

    def next_session(self, exchange: str, date: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT MIN(date) FROM calendar
                WHERE exchange = ? AND is_open = 1 AND date > ?
                """,
                (str(exchange), str(date).replace("-", "")),
            ).fetchone()
        return str(row[0]) if row and row[0] else None

    def write_analysis_run(self, values: dict[str, Any]) -> None:
        now = pd.Timestamp.now(tz="UTC").isoformat()
        details = values.get("details", {})
        row = (
            str(values["run_id"]),
            str(values["as_of_trade_date"]),
            values.get("information_cutoff"),
            str(values.get("generated_at") or now),
            str(values["mode"]),
            str(values.get("data_source_mode") or self.data_mode),
            values.get("code_hash"),
            values.get("config_hash"),
            values.get("data_hash"),
            str(values["status"]),
            values.get("coverage"),
            canonical_json(details),
        )
        with self._write_connection() as conn:
            conn.execute(
                """
                INSERT INTO analysis_runs(
                    run_id, as_of_trade_date, information_cutoff, generated_at,
                    mode, data_source_mode, code_hash, config_hash, data_hash,
                    status, coverage, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status, coverage=excluded.coverage,
                    details_json=excluded.details_json
                """,
                row,
            )

    def read_analysis_runs(self) -> pd.DataFrame:
        with closing(self._connect()) as conn:
            return pd.read_sql_query("SELECT * FROM analysis_runs ORDER BY generated_at, run_id", conn)

    def create_paper_account(
        self,
        account_id: str,
        *,
        method: str,
        initial_cash_cents: int,
        account_type: str = "paper",
    ) -> None:
        if int(initial_cash_cents) < 0:
            raise ContractError("initial paper cash cannot be negative")
        created_at = pd.Timestamp.now(tz="UTC").isoformat()
        with self._write_connection() as conn:
            existing = conn.execute(
                "SELECT method, account_type, initial_cash_cents FROM paper_accounts WHERE account_id = ?",
                (str(account_id),),
            ).fetchone()
            expected = (str(method), str(account_type), int(initial_cash_cents))
            if existing is not None and tuple(existing) != expected:
                raise ContractError("paper account definition is immutable")
            conn.execute(
                """
                INSERT OR IGNORE INTO paper_accounts(
                    account_id, method, account_type, initial_cash_cents, created_at, status
                ) VALUES (?, ?, ?, ?, ?, 'ACTIVE')
                """,
                (str(account_id), str(method), str(account_type), int(initial_cash_cents), created_at),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO paper_cash_ledger(
                    entry_id, account_id, event_at, amount_cents,
                    balance_cents, reason_code, reference_id
                ) VALUES (?, ?, ?, ?, ?, 'INITIAL_CAPITAL', ?)
                """,
                (
                    f"initial:{account_id}",
                    str(account_id),
                    created_at,
                    int(initial_cash_cents),
                    int(initial_cash_cents),
                    str(account_id),
                ),
            )

    def paper_cash_balance(self, account_id: str) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT balance_cents FROM paper_cash_ledger
                WHERE account_id = ? ORDER BY event_at DESC, rowid DESC LIMIT 1
                """,
                (str(account_id),),
            ).fetchone()
        if row is None:
            raise ContractError(f"paper account has no cash ledger: {account_id}")
        return int(row[0])

    def read_paper_lots(self, account_id: str) -> pd.DataFrame:
        with closing(self._connect()) as conn:
            return pd.read_sql_query(
                "SELECT * FROM paper_lots WHERE account_id = ? ORDER BY entry_date, lot_id",
                conn,
                params=[str(account_id)],
            )

    def read_paper_orders(self, account_id: str | None = None) -> pd.DataFrame:
        query = "SELECT * FROM paper_orders"
        params: list[Any] = []
        if account_id is not None:
            query += " WHERE account_id = ?"
            params.append(str(account_id))
        query += " ORDER BY earliest_trade_date, order_id"
        with closing(self._connect()) as conn:
            return pd.read_sql_query(query, conn, params=params)

    def read_paper_fills(self, account_id: str | None = None) -> pd.DataFrame:
        query = "SELECT * FROM paper_fills"
        params: list[Any] = []
        if account_id is not None:
            query += " WHERE account_id = ?"
            params.append(str(account_id))
        query += " ORDER BY trade_date, fill_id"
        with closing(self._connect()) as conn:
            return pd.read_sql_query(query, conn, params=params)

    def count_fills(self, order_id: str | None = None) -> int:
        query = "SELECT COUNT(*) FROM paper_fills"
        params: tuple[Any, ...] = ()
        if order_id is not None:
            query += " WHERE order_id = ?"
            params = (str(order_id),)
        with closing(self._connect()) as conn:
            row = conn.execute(query, params).fetchone()
        return int(row[0]) if row else 0

    def read_paper_corporate_action_ledger(self, account_id: str) -> pd.DataFrame:
        with closing(self._connect()) as conn:
            return pd.read_sql_query(
                """
                SELECT * FROM paper_corporate_action_ledger
                WHERE account_id = ? ORDER BY effective_date, ledger_id
                """,
                conn,
                params=[str(account_id)],
            )

    def _upsert_frame(
        self,
        table: str,
        frame: pd.DataFrame,
        columns: list[str],
        *,
        conflict_columns: tuple[str, ...],
        update_columns: Iterable[str],
    ) -> int:
        if table not in REQUIRED_TABLES:
            raise ContractError(f"unsupported V2 table: {table}")
        rows = _frame_records(frame, columns)
        if not rows:
            return 0
        assignments = ", ".join(f"{column}=excluded.{column}" for column in update_columns)
        sql = f"""
            INSERT INTO {table} ({','.join(columns)})
            VALUES ({','.join('?' for _ in columns)})
            ON CONFLICT({','.join(conflict_columns)}) DO UPDATE SET {assignments}
        """
        with self._write_connection() as conn:
            conn.executemany(sql, rows)
        return len(rows)


def _frame_records(frame: pd.DataFrame, columns: list[str]) -> list[tuple[Any, ...]]:
    if frame is None or frame.empty:
        return []
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ContractError(f"missing required columns: {', '.join(missing)}")
    work = frame[columns].copy()
    work = work.where(pd.notna(work), None)
    return [tuple(row) for row in work.itertuples(index=False, name=None)]
