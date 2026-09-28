from __future__ import annotations

import json
import fcntl
import sqlite3
import uuid
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from core.factors.technical_v2 import apply_adjustment_factors
from core.pipeline.prism_compare_config import file_sha256, write_json
from core.pipeline.technical_v2 import purge_cross_boundary
from core.strategies.formula_v2 import ReturnBinModel, fit_return_bins
from core.technical_v2.contracts import ContractError

_VERIFIED_SNAPSHOTS: set[tuple[str, int, int, str]] = set()


@dataclass(frozen=True)
class SnapshotAudit:
    audit: dict[str, Any]
    sessions: list[str]
    roster: pd.DataFrame
    memberships: pd.DataFrame
    actions: pd.DataFrame


def readonly_connection(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


@contextmanager
def experiment_lock(experiment_root: str | Path):
    root = Path(experiment_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".compare.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ContractError("experiment is already running; preserve the active lock and process") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def create_snapshot(source_db: str | Path, experiment_root: str | Path) -> dict[str, Any]:
    with experiment_lock(experiment_root):
        return _create_snapshot(source_db, experiment_root)


def _create_snapshot(source_db: str | Path, experiment_root: str | Path) -> dict[str, Any]:
    source, root = Path(source_db).resolve(), Path(experiment_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / "market_snapshot.db"
    manifest_path = root / "snapshot_manifest.json"
    if source == target:
        raise ContractError("source must not be the experiment snapshot")
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest["source_db"] != str(source):
            raise ContractError("existing experiment has a different source database")
        stat = target.stat()
        if (stat.st_size != manifest["snapshot_size_bytes"]
                or stat.st_mtime_ns != manifest["snapshot_mtime_ns"]):
            raise ContractError("frozen snapshot metadata changed; do not overwrite the experiment")
        identity = (str(target), stat.st_size, stat.st_mtime_ns, manifest["snapshot_sha256"])
        if identity not in _VERIFIED_SNAPSHOTS:
            if file_sha256(target) != manifest["snapshot_sha256"]:
                raise ContractError("frozen snapshot SHA256 mismatch")
            _VERIFIED_SNAPSHOTS.add(identity)
        return manifest
    if target.exists():
        raise ContractError("unbound snapshot exists without its manifest; preserve it for investigation")
    temporary = root / f"backup-{uuid.uuid4().hex}.db"
    started_at = datetime.now(timezone.utc).isoformat()
    source_connection = readonly_connection(source)
    destination = sqlite3.connect(temporary)
    try:
        source_connection.backup(destination, pages=4096, sleep=.01)
        destination.execute("PRAGMA journal_mode=DELETE")
        if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ContractError("SQLite backup failed integrity verification")
        versions = pd.read_sql_query("SELECT * FROM schema_migrations ORDER BY version", destination).to_dict("records")
    finally:
        destination.close()
        source_connection.close()
    temporary.replace(target)
    target.chmod(0o444)
    stat = target.stat()
    manifest = {
        "snapshot_id": root.name, "source_db": str(source), "snapshot_db": str(target),
        "snapshot_sha256": file_sha256(target), "snapshot_size_bytes": stat.st_size,
        "snapshot_mtime_ns": stat.st_mtime_ns, "backup_started_at": started_at,
        "backup_finished_at": datetime.now(timezone.utc).isoformat(), "schema_versions": versions,
        "backup_method": "sqlite3.Connection.backup", "source_connection": "mode=ro/query_only",
        "snapshot_reuse_validation": "immutable_file_size_and_mtime_bound_to_initial_streamed_sha256",
    }
    write_json(manifest_path, manifest, immutable=True)
    _VERIFIED_SNAPSHOTS.add((str(target), stat.st_size, stat.st_mtime_ns, manifest["snapshot_sha256"]))
    return manifest


def align_panel(raw: pd.DataFrame, adjustments: pd.DataFrame, sessions: list[str],
                roster: pd.DataFrame) -> pd.DataFrame:
    raw = raw.sort_values([column for column in ("code", "date", "retrieved_at", "source_version") if column in raw])
    raw = raw.drop_duplicates(["code", "date"], keep="last")
    grid = pd.MultiIndex.from_product([roster.code.astype(str), sessions], names=["code", "date"]).to_frame(index=False)
    panel = grid.merge(raw, on=["code", "date"], how="left", validate="one_to_one")
    metadata = roster[[column for column in ("code", "list_date", "delist_date") if column in roster]]
    panel = panel.merge(metadata, on="code", how="left", validate="many_to_one")
    listed = panel.list_date.fillna("").astype(str)
    delisted = panel.delist_date.fillna("").astype(str)
    panel["roster_active"] = (listed.eq("") | panel.date.ge(listed)) & (delisted.eq("") | panel.date.lt(delisted))
    prices = panel[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    valid = (np.isfinite(prices).all(axis=1) & prices.gt(0).all(axis=1)
             & prices.high.ge(prices[["open", "close", "low"]].max(axis=1))
             & prices.low.le(prices[["open", "close", "high"]].min(axis=1)))
    valid &= panel.data_source_mode.eq("real") & panel.completeness.eq("COMPLETE") & panel.roster_active
    valid &= pd.to_numeric(panel.amount_cny, errors="coerce").gt(0) & pd.to_numeric(panel.volume_shares, errors="coerce").gt(0)
    panel["real_bar"] = valid
    panel["input_status"] = np.where(valid, "OK", "MISSING_OR_INVALID_REAL_BAR")
    for column in ("open", "high", "low", "close", "volume_shares", "amount_cny"):
        panel.loc[~valid, column] = np.nan
    panel = apply_adjustment_factors(panel, adjustments, as_of=sessions[-1])
    # A constant per-code comparison scale avoids later as_of normalization rescaling old stops.
    for column in ("open", "high", "low", "close"):
        panel[f"comparison_{column}"] = pd.to_numeric(panel[column], errors="coerce") * panel.adj_factor
        panel[f"adjusted_{column}"] = panel[f"comparison_{column}"]
    panel["mark_only"] = False
    return panel.sort_values(["code", "date"]).reset_index(drop=True)


def fit_common_bins(rows: pd.DataFrame, calibration_start: str) -> ReturnBinModel:
    training = rows.loc[rows.split.eq("train") & rows.label_status.eq("OK") & rows.formula_score.notna()].copy()
    training = purge_cross_boundary(training, calibration_start)
    return fit_return_bins(training, entity_type="stock", horizon=5)


def save_common_bins(path: str | Path, model: ReturnBinModel) -> str:
    return write_json(path, asdict(model), immutable=True)


def audit_snapshot(path: str | Path, config: dict[str, Any]) -> SnapshotAudit:
    with closing(readonly_connection(path)) as conn:
        complete = conn.execute("SELECT MAX(date) FROM sync_audits WHERE status='COMPLETE' AND exchange IN ('SSE','SZSE')").fetchone()[0]
        if not complete:
            raise ContractError("snapshot has no audited COMPLETE exchange session")
        first = conn.execute("SELECT MIN(date) FROM daily_raw WHERE data_source_mode='real'").fetchone()[0]
        if not first:
            raise ContractError("snapshot has no real historical prices")
        calendar = pd.read_sql_query(
            "SELECT * FROM calendar WHERE exchange IN ('SSE','SZSE') AND date>=? AND date<=? ORDER BY retrieved_at,source_version",
            conn, params=[first, complete],
        ).drop_duplicates(["exchange", "date"], keep="last")
        dates = {exchange: set(calendar.loc[calendar.exchange.eq(exchange) & calendar.is_open.eq(1), "date"])
                 for exchange in ("SSE", "SZSE")}
        if dates["SSE"] != dates["SZSE"] or complete not in dates["SSE"]:
            raise ContractError("SSE/SZSE calendars disagree or the audit cutoff is not an open session")
        sessions = sorted(dates["SSE"])
        versions = pd.read_sql_query(
            "SELECT * FROM instrument_versions WHERE exchange IN ('SSE','SZSE') AND valid_from<=? ORDER BY code,valid_from,observed_at,source_version",
            conn, params=[complete],
        ).drop_duplicates("code", keep="last")
        bar_codes = pd.read_sql_query(
            "SELECT code,MIN(date) AS first_observed_date FROM daily_raw WHERE date<=? AND data_source_mode='real' AND (code LIKE '%.SH' OR code LIKE '%.SZ') GROUP BY code",
            conn, params=[complete],
        )
        columns = ["code", "instrument_type", "exchange", "listing_board", "list_date", "delist_date", "listing_status", "observed_at"]
        roster = pd.DataFrame({"code": sorted(set(versions.code) | set(bar_codes.code))}).merge(
            versions[columns], on="code", how="left", validate="one_to_one",
        ).merge(bar_codes, on="code", how="left", validate="one_to_one")
        roster["metadata_status"] = np.where(roster.instrument_type.eq("STOCK") & roster.list_date.notna(), "AVAILABLE_LIST_DATE_HISTORY_LIMITED", "INSTRUMENT_METADATA_UNKNOWN")
        # Keep unavailable roster rows. Unknown list dates are not used to backdate new entries.
        roster["context_valid_from"] = roster.list_date.fillna(roster.first_observed_date).fillna(complete)
        roster["context_valid_to"] = roster.delist_date
        memberships = pd.read_sql_query("SELECT * FROM sector_membership WHERE valid_from<=?", conn, params=[complete])
        memberships = memberships.loc[memberships.code.isin(roster.code)].copy()
        actions = pd.read_sql_query("SELECT * FROM corporate_actions ORDER BY retrieved_at,source_version", conn).drop_duplicates("event_id", keep="last")
        tables: dict[str, Any] = {}
        for table in ("daily_raw", "adjustments", "trading_status"):
            count, minimum, maximum = conn.execute(f"SELECT COUNT(*),MIN(date),MAX(date) FROM {table} WHERE date<=?", (complete,)).fetchone()
            source_versions = conn.execute(f"SELECT source_version,source,COUNT(*) FROM {table} WHERE date<=? GROUP BY source_version,source", (complete,)).fetchall()
            tables[table] = {"rows": count, "first_date": minimum, "last_date": maximum,
                             "source_versions": source_versions}
        mode_counts = conn.execute("SELECT data_source_mode,COUNT(*) FROM daily_raw WHERE date<=? GROUP BY data_source_mode", (complete,)).fetchall()
        risk_known = conn.execute("SELECT COUNT(*) FROM trading_status WHERE date<=? AND is_risk_warning IS NOT NULL", (complete,)).fetchone()[0]
        coverage = pd.read_sql_query(
            "SELECT date,COUNT(DISTINCT code) AS real_price_codes FROM daily_raw WHERE date<=? AND data_source_mode='real' AND (code LIKE '%.SH' OR code LIKE '%.SZ') GROUP BY date ORDER BY date",
            conn, params=[complete],
        )
        audit_exchanges = [row[0] for row in conn.execute("SELECT DISTINCT exchange FROM sync_audits WHERE status='COMPLETE'")]
    flags = ["UNIVERSE_HISTORY_LIMITED", "HISTORICAL_DATA_REVISIONS_NOT_POINT_IN_TIME_VINTAGES", "REUSED_HOLDOUT"]
    if not risk_known:
        flags.append("HISTORICAL_RISK_WARNING_UNKNOWN")
    if actions.empty:
        flags.append("RAW_PRICE_LEDGER_CORPORATE_ACTIONS_INCOMPLETE")
    if memberships.empty or not memberships.history_mode.eq("RECONSTRUCTED_PIT").all():
        flags.append("SECTOR_HISTORY_LIMITED")
    audit = {
        "as_of": complete, "first_price_date": first, "session_count": len(sessions),
        "calendar_agreement": "SSE_SZSE_MATCH", "complete_audit_exchanges": audit_exchanges,
        "audit_scope": "SOURCE_COMPLETE_AUDIT_PLUS_BOTH_EXCHANGE_CALENDARS",
        "tables": tables, "price_mode_counts": mode_counts, "roster_code_count": len(roster),
        "listed_instrument_snapshot_count": len(versions), "historical_universe_complete": False,
        "corporate_action_rows": len(actions), "corporate_action_coverage_proven": False,
        "known_risk_warning_rows": risk_known, "risk_warning_unknown_policy": config["data"]["risk_warning_unknown"],
        "sector_history_modes": sorted(memberships.history_mode.unique().tolist()) if not memberships.empty else [],
        "daily_real_price_coverage": coverage.to_dict("records"), "scope_flags": flags,
        "old_portfolio_test_executed": "UNKNOWN_FOR_REAL_DATA; LOCATED_DEMO_ARTIFACTS_FALSE",
        "old_test_summary_exposure": "UNKNOWN_FOR_REAL_DATA; ALL_SPLIT_FACTOR_IC_IN_LOCATED_DEMO_EVIDENCE",
        "old_split_reused": False, "old_split_reason": "LOCATED_FIXED_DATASET_MANIFESTS_ARE_DEMO_NOT_CURRENT_REAL_SNAPSHOT",
        "no_production_mutation": "ALL_SOURCE_ACCESS_READ_ONLY; BACKUP_AND_OUTPUTS_EXPERIMENT_ONLY",
    }
    return SnapshotAudit(audit, sessions, roster, memberships, actions)
