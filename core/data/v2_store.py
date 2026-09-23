from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import pandas as pd

from core.technical_v2.contracts import ContractError, canonical_json

SCHEMA_VERSION = 3
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
    "label_rows",
    "evaluation_rows",
    "paper_valuations",
    "paper_decisions",
    "paper_order_details",
    "sync_audits",
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


_MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS label_rows (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    as_of_trade_date TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    target_definition_version TEXT NOT NULL,
    label_end_date TEXT,
    label_status TEXT NOT NULL,
    target_class TEXT,
    realized_return REAL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (
        entity_type, entity_id, as_of_trade_date, horizon,
        target_definition_version
    )
);

CREATE TABLE IF NOT EXISTS evaluation_rows (
    run_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    as_of_trade_date TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    method TEXT NOT NULL,
    label_end_date TEXT,
    evaluation_status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    first_evaluated_at TEXT NOT NULL,
    PRIMARY KEY (
        run_id, entity_type, entity_id, as_of_trade_date, horizon, method
    )
);

CREATE TABLE IF NOT EXISTS paper_valuations (
    account_id TEXT NOT NULL,
    date TEXT NOT NULL,
    nav_cents INTEGER,
    cash_cents INTEGER NOT NULL,
    market_value_cents INTEGER,
    status TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (account_id, date)
);

CREATE TABLE IF NOT EXISTS sync_audits (
    exchange TEXT NOT NULL,
    date TEXT NOT NULL,
    source_version TEXT NOT NULL,
    expected_instruments INTEGER NOT NULL,
    observed_rows INTEGER NOT NULL,
    known_non_trading_rows INTEGER NOT NULL,
    coverage REAL NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (exchange, date, source_version)
);

CREATE INDEX IF NOT EXISTS idx_labels_maturity
    ON label_rows(label_status, label_end_date, horizon);
CREATE INDEX IF NOT EXISTS idx_evaluations_run
    ON evaluation_rows(run_id, method, horizon);
CREATE INDEX IF NOT EXISTS idx_valuations_account
    ON paper_valuations(account_id, date);
CREATE INDEX IF NOT EXISTS idx_sync_audits_complete
    ON sync_audits(exchange, status, date);
"""


_MIGRATION_3 = """
CREATE TABLE IF NOT EXISTS paper_decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    as_of_trade_date TEXT NOT NULL,
    method TEXT NOT NULL,
    status TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    first_recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_order_details (
    order_id TEXT PRIMARY KEY,
    reference_price TEXT NOT NULL,
    planned_exit_date TEXT,
    planned_stop_price TEXT,
    sector_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_paper_decisions_account_date
    ON paper_decisions(account_id, as_of_trade_date);
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
                "VALUES (1, CURRENT_TIMESTAMP, 'Initial Technical V2 schema');\n"
                + _MIGRATION_2
                + "\nINSERT OR IGNORE INTO schema_migrations(version, applied_at, summary) "
                "VALUES (2, CURRENT_TIMESTAMP, 'Durable labels, evaluations, valuations, and sync audits');\n"
                + _MIGRATION_3
                + "\nINSERT OR IGNORE INTO schema_migrations(version, applied_at, summary) "
                "VALUES (3, CURRENT_TIMESTAMP, 'Auditable paper decisions and reconstructable order details');\n"
                "COMMIT;"
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

    def read_trading_status(
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
        with closing(self._connect()) as conn:
            frame = pd.read_sql_query(
                "SELECT * FROM trading_status WHERE date >= ? AND date <= ?"
                f"{code_clause} ORDER BY code, date, retrieved_at, source_version",
                conn,
                params=params,
            )
        if frame.empty:
            return frame

        def latest_value(values: pd.Series) -> Any:
            available = values.dropna()
            return available.iloc[-1] if not available.empty else None

        value_columns = [
            "is_suspended",
            "is_risk_warning",
            "up_limit",
            "down_limit",
            "no_price_limit",
            "rule_version",
            "source",
            "source_version",
            "retrieved_at",
        ]
        return (
            frame.groupby(["code", "date"], as_index=False, sort=True)[value_columns]
            .agg(latest_value)
            .sort_values(["code", "date"])
            .reset_index(drop=True)
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

    def upsert_feature_rows(self, rows: Iterable[Mapping[str, Any]]) -> int:
        values = []
        for item in rows:
            values.append(
                (
                    str(item["run_id"]),
                    str(item["entity_type"]),
                    str(item["entity_id"]),
                    str(item["as_of_trade_date"]).replace("-", "")[:8],
                    str(item["factor_id"]),
                    item.get("value"),
                    str(item["status"]),
                    item.get("reason_code"),
                    canonical_json(item.get("metadata", {})),
                )
            )
        if not values:
            return 0
        with self._write_connection() as conn:
            conn.executemany(
                """
                INSERT INTO feature_rows(
                    run_id, entity_type, entity_id, as_of_trade_date, factor_id,
                    value, status, reason_code, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, entity_type, entity_id, factor_id) DO UPDATE SET
                    value=excluded.value, status=excluded.status,
                    reason_code=excluded.reason_code,
                    metadata_json=excluded.metadata_json
                """,
                values,
            )
        return len(values)

    def read_feature_rows(self, run_id: str | None = None) -> pd.DataFrame:
        query = "SELECT * FROM feature_rows"
        params: list[Any] = []
        if run_id is not None:
            query += " WHERE run_id = ?"
            params.append(str(run_id))
        query += " ORDER BY run_id, entity_type, entity_id, factor_id"
        with closing(self._connect()) as conn:
            return pd.read_sql_query(query, conn, params=params)

    def write_prediction_rows(self, rows: Iterable[Mapping[str, Any]]) -> int:
        inserted = 0
        now = pd.Timestamp.now(tz="UTC").isoformat()
        with self._write_connection() as conn:
            for item in rows:
                key = (
                    str(item["run_id"]),
                    str(item["entity_type"]),
                    str(item["entity_id"]),
                    int(item["horizon"]),
                    str(item["method"]),
                )
                prediction_status = str(item["prediction_status"])
                payload_json = canonical_json(item.get("payload", {}))
                existing = conn.execute(
                    """
                    SELECT prediction_status, payload_json FROM prediction_rows
                    WHERE run_id = ? AND entity_type = ? AND entity_id = ?
                      AND horizon = ? AND method = ?
                    """,
                    key,
                ).fetchone()
                if existing is not None:
                    if tuple(existing) != (prediction_status, payload_json):
                        raise ContractError("prediction row is immutable and conflicts with first record")
                    continue
                conn.execute(
                    """
                    INSERT INTO prediction_rows(
                        run_id, entity_type, entity_id, horizon, method,
                        prediction_status, payload_json, first_recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*key, prediction_status, payload_json, str(item.get("first_recorded_at") or now)),
                )
                inserted += 1
        return inserted

    def read_prediction_rows(
        self,
        run_id: str | None = None,
        *,
        method: str | None = None,
        horizon: int | None = None,
    ) -> pd.DataFrame:
        clauses: list[str] = []
        params: list[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(str(run_id))
        if method is not None:
            clauses.append("method = ?")
            params.append(str(method))
        if horizon is not None:
            clauses.append("horizon = ?")
            params.append(int(horizon))
        query = "SELECT * FROM prediction_rows"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY run_id, entity_type, entity_id, horizon, method"
        with closing(self._connect()) as conn:
            return pd.read_sql_query(query, conn, params=params)

    def upsert_model_registry(self, rows: Iterable[Mapping[str, Any]]) -> int:
        values = []
        for item in rows:
            values.append(
                (
                    str(item["method"]),
                    str(item["config_id"]),
                    str(item["entity_type"]),
                    int(item["horizon"]),
                    item.get("model_id"),
                    item.get("calibration_id"),
                    item.get("train_start"),
                    item.get("train_end"),
                    item.get("calibration_start"),
                    item.get("calibration_end"),
                    str(item["status"]),
                    canonical_json(item.get("payload", {})),
                )
            )
        if not values:
            return 0
        with self._write_connection() as conn:
            conn.executemany(
                """
                INSERT INTO model_registry(
                    method, config_id, entity_type, horizon, model_id,
                    calibration_id, train_start, train_end, calibration_start,
                    calibration_end, status, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(method, config_id, entity_type, horizon) DO UPDATE SET
                    model_id=excluded.model_id,
                    calibration_id=excluded.calibration_id,
                    train_start=excluded.train_start,
                    train_end=excluded.train_end,
                    calibration_start=excluded.calibration_start,
                    calibration_end=excluded.calibration_end,
                    status=excluded.status,
                    payload_json=excluded.payload_json
                """,
                values,
            )
        return len(values)

    def read_model_registry(self) -> pd.DataFrame:
        with closing(self._connect()) as conn:
            return pd.read_sql_query(
                "SELECT * FROM model_registry ORDER BY method, config_id, entity_type, horizon",
                conn,
            )

    def upsert_label_rows(self, rows: Iterable[Mapping[str, Any]]) -> int:
        now = pd.Timestamp.now(tz="UTC").isoformat()
        values = []
        for item in rows:
            values.append(
                (
                    str(item["entity_type"]),
                    str(item["entity_id"]),
                    str(item["as_of_trade_date"]).replace("-", "")[:8],
                    int(item["horizon"]),
                    str(item.get("target_definition_version") or "adjusted-open-t1-to-t1-plus-h.v1"),
                    item.get("label_end_date"),
                    str(item["label_status"]),
                    item.get("target_class"),
                    item.get("realized_return"),
                    canonical_json(item.get("payload", {})),
                    str(item.get("updated_at") or now),
                )
            )
        if not values:
            return 0
        with self._write_connection() as conn:
            conn.executemany(
                """
                INSERT INTO label_rows(
                    entity_type, entity_id, as_of_trade_date, horizon,
                    target_definition_version, label_end_date, label_status,
                    target_class, realized_return, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    entity_type, entity_id, as_of_trade_date, horizon,
                    target_definition_version
                ) DO UPDATE SET
                    label_end_date=excluded.label_end_date,
                    label_status=excluded.label_status,
                    target_class=excluded.target_class,
                    realized_return=excluded.realized_return,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                values,
            )
        return len(values)

    def read_label_rows(self, *, matured_only: bool = False) -> pd.DataFrame:
        query = "SELECT * FROM label_rows"
        if matured_only:
            query += " WHERE label_status = 'OK'"
        query += " ORDER BY entity_type, entity_id, as_of_trade_date, horizon"
        with closing(self._connect()) as conn:
            return pd.read_sql_query(query, conn)

    def write_evaluation_rows(self, rows: Iterable[Mapping[str, Any]]) -> int:
        inserted = 0
        now = pd.Timestamp.now(tz="UTC").isoformat()
        with self._write_connection() as conn:
            for item in rows:
                key = (
                    str(item["run_id"]),
                    str(item["entity_type"]),
                    str(item["entity_id"]),
                    str(item["as_of_trade_date"]).replace("-", "")[:8],
                    int(item["horizon"]),
                    str(item["method"]),
                )
                label_end_date = item.get("label_end_date")
                status = str(item["evaluation_status"])
                payload_json = canonical_json(item.get("payload", {}))
                existing = conn.execute(
                    """
                    SELECT label_end_date, evaluation_status, payload_json
                    FROM evaluation_rows
                    WHERE run_id = ? AND entity_type = ? AND entity_id = ?
                      AND as_of_trade_date = ? AND horizon = ? AND method = ?
                    """,
                    key,
                ).fetchone()
                expected = (label_end_date, status, payload_json)
                if existing is not None:
                    if tuple(existing) != expected:
                        raise ContractError("evaluation row is immutable and conflicts with first record")
                    continue
                conn.execute(
                    """
                    INSERT INTO evaluation_rows(
                        run_id, entity_type, entity_id, as_of_trade_date,
                        horizon, method, label_end_date, evaluation_status,
                        payload_json, first_evaluated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*key, label_end_date, status, payload_json, str(item.get("first_evaluated_at") or now)),
                )
                inserted += 1
        return inserted

    def read_evaluation_rows(self, run_id: str | None = None) -> pd.DataFrame:
        query = "SELECT * FROM evaluation_rows"
        params: list[Any] = []
        if run_id is not None:
            query += " WHERE run_id = ?"
            params.append(str(run_id))
        query += " ORDER BY run_id, entity_type, entity_id, as_of_trade_date, horizon, method"
        with closing(self._connect()) as conn:
            return pd.read_sql_query(query, conn, params=params)

    def upsert_paper_valuations(self, rows: Iterable[Mapping[str, Any]]) -> int:
        now = pd.Timestamp.now(tz="UTC").isoformat()
        values = []
        for item in rows:
            values.append(
                (
                    str(item["account_id"]),
                    str(item["date"]).replace("-", "")[:8],
                    item.get("nav_cents"),
                    int(item["cash_cents"]),
                    item.get("market_value_cents"),
                    str(item["status"]),
                    canonical_json(item.get("reason_codes", [])),
                    canonical_json(item.get("payload", {})),
                    str(item.get("recorded_at") or now),
                )
            )
        if not values:
            return 0
        with self._write_connection() as conn:
            conn.executemany(
                """
                INSERT INTO paper_valuations(
                    account_id, date, nav_cents, cash_cents, market_value_cents,
                    status, reason_codes_json, payload_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, date) DO UPDATE SET
                    nav_cents=excluded.nav_cents,
                    cash_cents=excluded.cash_cents,
                    market_value_cents=excluded.market_value_cents,
                    status=excluded.status,
                    reason_codes_json=excluded.reason_codes_json,
                    payload_json=excluded.payload_json,
                    recorded_at=excluded.recorded_at
                """,
                values,
            )
        return len(values)

    def read_paper_valuations(self, account_id: str | None = None) -> pd.DataFrame:
        query = "SELECT * FROM paper_valuations"
        params: list[Any] = []
        if account_id is not None:
            query += " WHERE account_id = ?"
            params.append(str(account_id))
        query += " ORDER BY account_id, date"
        with closing(self._connect()) as conn:
            return pd.read_sql_query(query, conn, params=params)

    def write_paper_decisions(self, rows: Iterable[Mapping[str, Any]]) -> int:
        inserted = 0
        now = pd.Timestamp.now(tz="UTC").isoformat()
        with self._write_connection() as conn:
            for item in rows:
                decision_id = str(item["decision_id"])
                immutable = (
                    str(item["run_id"]),
                    str(item["account_id"]),
                    str(item["entity_id"]),
                    str(item["as_of_trade_date"]).replace("-", "")[:8],
                    str(item["method"]),
                    str(item["status"]),
                    canonical_json(item.get("reason_codes", [])),
                    canonical_json(item.get("payload", {})),
                )
                existing = conn.execute(
                    """
                    SELECT run_id, account_id, entity_id, as_of_trade_date,
                           method, status, reason_codes_json, payload_json
                    FROM paper_decisions WHERE decision_id = ?
                    """,
                    (decision_id,),
                ).fetchone()
                if existing is not None:
                    if tuple(existing) != immutable:
                        raise ContractError("paper decision is immutable and conflicts with first record")
                    continue
                conn.execute(
                    """
                    INSERT INTO paper_decisions(
                        decision_id, run_id, account_id, entity_id,
                        as_of_trade_date, method, status, reason_codes_json,
                        payload_json, first_recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (decision_id, *immutable, str(item.get("first_recorded_at") or now)),
                )
                inserted += 1
        return inserted

    def read_paper_decisions(self, account_id: str | None = None) -> pd.DataFrame:
        query = "SELECT * FROM paper_decisions"
        params: list[Any] = []
        if account_id is not None:
            query += " WHERE account_id = ?"
            params.append(str(account_id))
        query += " ORDER BY as_of_trade_date, account_id, entity_id, decision_id"
        with closing(self._connect()) as conn:
            return pd.read_sql_query(query, conn, params=params)

    def upsert_sync_audits(self, rows: Iterable[Mapping[str, Any]]) -> int:
        now = pd.Timestamp.now(tz="UTC").isoformat()
        values = []
        for item in rows:
            expected = int(item["expected_instruments"])
            observed = int(item["observed_rows"])
            non_trading = int(item.get("known_non_trading_rows", 0))
            coverage = float(item["coverage"])
            if min(expected, observed, non_trading) < 0 or not 0.0 <= coverage <= 1.0:
                raise ContractError("sync audit counts or coverage are invalid")
            values.append(
                (
                    str(item["exchange"]),
                    str(item["date"]).replace("-", "")[:8],
                    str(item["source_version"]),
                    expected,
                    observed,
                    non_trading,
                    coverage,
                    str(item["status"]),
                    canonical_json(item.get("details", {})),
                    str(item.get("recorded_at") or now),
                )
            )
        if not values:
            return 0
        with self._write_connection() as conn:
            conn.executemany(
                """
                INSERT INTO sync_audits(
                    exchange, date, source_version, expected_instruments,
                    observed_rows, known_non_trading_rows, coverage, status,
                    details_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(exchange, date, source_version) DO UPDATE SET
                    expected_instruments=excluded.expected_instruments,
                    observed_rows=excluded.observed_rows,
                    known_non_trading_rows=excluded.known_non_trading_rows,
                    coverage=excluded.coverage,
                    status=excluded.status,
                    details_json=excluded.details_json,
                    recorded_at=excluded.recorded_at
                """,
                values,
            )
        return len(values)

    def read_sync_audits(self, exchange: str | None = None) -> pd.DataFrame:
        query = "SELECT * FROM sync_audits"
        params: list[Any] = []
        if exchange is not None:
            query += " WHERE exchange = ?"
            params.append(str(exchange))
        query += " ORDER BY exchange, date, recorded_at, source_version"
        with closing(self._connect()) as conn:
            return pd.read_sql_query(query, conn, params=params)

    def latest_complete_session(self, exchange: str, end_date: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT MAX(date) FROM sync_audits
                WHERE exchange = ? AND date <= ? AND status = 'COMPLETE'
                """,
                (str(exchange), str(end_date).replace("-", "")[:8]),
            ).fetchone()
        return str(row[0]) if row and row[0] else None

    def missing_sync_sessions(
        self,
        exchange: str,
        sessions: Iterable[str],
        *,
        force_refresh: bool = False,
    ) -> list[str]:
        normalized = sorted({str(value).replace("-", "")[:8] for value in sessions})
        if force_refresh or not normalized:
            return normalized
        placeholders = ",".join("?" for _ in normalized)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT date FROM sync_audits
                WHERE exchange = ? AND status = 'COMPLETE'
                  AND date IN ({placeholders})
                """,
                (str(exchange), *normalized),
            ).fetchall()
        complete = {str(row[0]) for row in rows}
        return [value for value in normalized if value not in complete]

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
