from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
import zipfile
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from config import BASE_DIR, settings
from core.analysis.sector_v2 import build_context
from core.background.snapshot_store import (
    ArtifactMismatch,
    read_latest_manifest,
    write_immutable_json,
)
from core.data.v2_provider import DemoV2Provider, RealV2Provider
from core.data.v2_store import REQUIRED_TABLES, V2Store
from core.data.v2_universe import build_analysis_universe
from core.factors.technical_v2 import (
    DIRECTIONAL_FACTOR_IDS,
    RISK_INDICATOR_IDS,
    apply_adjustment_factors,
    compute_technical_v2,
)
from core.models.jev_client import JevBudget, JevClient
from core.pipeline.evaluation_v2 import materialize_labels, run_fixed_evaluation
from core.pipeline.paper_runtime_v2 import evaluate_matured_predictions, run_paper_cycle
from core.pipeline.technical_v2 import TARGET_DEFINITION_VERSION, TechnicalV2Pipeline
from core.strategies.formula_v2 import FORMULA_CONFIG_IDS, score_sector, score_stock
from core.strategies.intent_v2 import derive_research_intent
from core.strategies.jev_v2 import (
    build_jev_state,
    build_questions,
    pool_probabilities,
    validate_answers,
)
from core.technical_v2.config import TechnicalV2Config
from core.technical_v2.contracts import ContractError, json_safe, sha256_json

REQUIRED_COMMANDS = (
    "doctor",
    "sync",
    "analyze",
    "fit-evaluate",
    "paper",
    "evaluate-matured",
    "export",
    "demo",
    "audit-release",
)
CONFIG_PATH = BASE_DIR / "configs" / "technical_v2.json"
DEFAULT_DEMO_AS_OF = "20260922"


@dataclass(frozen=True)
class CommandOutcome:
    payload: dict[str, Any]
    exit_code: int


class LockUnavailable(ContractError):
    pass


def _compact_date(value: Any) -> str:
    return str(value or "").strip().replace("-", "")[:8]


def _mode_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    mode = str(getattr(args, "mode", "real") or "real")
    db_override = str(getattr(args, "db_path", "") or "")
    artifact_override = str(getattr(args, "artifact_root", "") or "")
    if db_override:
        db_path = Path(db_override)
    elif mode == "real":
        db_path = settings.market_v2_db_path
    else:
        db_path = settings.demo_v2_root / f"{mode}.db"
    if artifact_override:
        artifact_root = Path(artifact_override)
    elif mode == "real":
        artifact_root = settings.technical_v2_artifact_root
    else:
        artifact_root = settings.demo_v2_root / "artifacts"
    return db_path, artifact_root


@contextmanager
def command_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockUnavailable(f"another Technical V2 command holds {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _market_data_hash(store: V2Store, as_of: str) -> str:
    tables = (
        "instrument_versions",
        "calendar",
        "daily_raw",
        "adjustments",
        "trading_status",
        "sector_membership",
        "corporate_actions",
        "sync_audits",
    )
    payload: dict[str, list[dict[str, Any]]] = {}
    with closing(store._connect()) as conn:
        for table in tables:
            columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]
            date_column = "date" if "date" in columns else None
            query = f"SELECT * FROM {table}"
            params: tuple[Any, ...] = ()
            if date_column is not None:
                query += " WHERE date <= ?"
                params = (str(as_of),)
            if columns:
                query += " ORDER BY " + ",".join(columns)
            rows = conn.execute(query, params).fetchall()
            payload[table] = [dict(row) for row in rows]
    return sha256_json(payload)


def _code_hash() -> str:
    paths = sorted(
        path
        for root in (BASE_DIR / "core", BASE_DIR / "scripts")
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(BASE_DIR)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _config_hash() -> str:
    TechnicalV2Config.load(CONFIG_PATH)
    return _file_hash(CONFIG_PATH)


def _database_tables(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        with closing(sqlite3.connect(path)) as conn:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[0]) for row in rows}


def resolve_latest_as_of(
    store: V2Store,
    *,
    now: pd.Timestamp | None = None,
) -> dict[str, Any]:
    current = now or pd.Timestamp.now(tz="Asia/Shanghai")
    if current.tzinfo is None:
        current = current.tz_localize("Asia/Shanghai")
    else:
        current = current.tz_convert("Asia/Shanghai")
    today = current.strftime("%Y%m%d")
    try:
        sessions = store.open_sessions("SSE", today, 10_000)
    except sqlite3.Error:
        sessions = []
    expected = sessions[-1] if sessions else None
    close_ready = (current.hour, current.minute) >= (16, 10)
    if expected == today and not close_ready:
        expected = sessions[-2] if len(sessions) >= 2 else None
    try:
        actual = store.latest_complete_session("SSE", today)
    except sqlite3.Error:
        actual = None
    if expected is None:
        freshness = "CALENDAR_UNAVAILABLE"
    elif actual is None:
        freshness = "DATA_UNAVAILABLE"
    elif actual < expected:
        freshness = "STALE"
    else:
        freshness = "CURRENT"
    return {
        "actual_as_of": actual,
        "expected_as_of": expected,
        "freshness_status": freshness,
        "resolved_at": current.isoformat(),
    }


def _common_payload(command: str, mode: str) -> dict[str, Any]:
    return {
        "schema_version": "technical-v2-cli.v1",
        "command": command,
        "mode": mode,
    }


def _doctor(args: argparse.Namespace) -> CommandOutcome:
    db_path, artifact_root = _mode_paths(args)
    missing: list[str] = []
    errors: list[str] = []
    try:
        config = TechnicalV2Config.load(CONFIG_PATH)
        config_version = str(config.schema_version)
    except ContractError as exc:
        errors.append(str(exc))
        config_version = None
    if args.mode == "real":
        if not os.getenv("TUSHARE_TOKEN", "").strip():
            missing.append("TUSHARE_TOKEN")
        if not os.getenv("TYPESAFE_API_KEY", "").strip():
            missing.append("TYPESAFE_API_KEY")
    tables = _database_tables(db_path)
    if not db_path.exists():
        missing.append("MARKET_V2_DB")
    elif missing_tables := sorted(REQUIRED_TABLES.difference(tables)):
        errors.append(f"missing V2 tables: {', '.join(missing_tables)}")
    payload = {
        **_common_payload("doctor", args.mode),
        "status": "ERROR" if errors else "PREREQUISITE_MISSING" if missing else "OK",
        "missing": sorted(set(missing)),
        "errors": errors,
        "config_version": config_version,
        "db_path": str(db_path),
        "artifact_root": str(artifact_root),
        "live_trading": "NOT_CONNECTED",
    }
    return CommandOutcome(payload, 3 if errors else 2 if missing else 0)


def _demo_universe() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"code": "000001.SZ", "name": "DEMO_MAIN_SZ", "listing_board": "MAIN_SZ"},
            {"code": "000002.SZ", "name": "DEMO_MAIN_SZ_2", "listing_board": "MAIN_SZ"},
            {"code": "300001.SZ", "name": "DEMO_CHINEXT", "listing_board": "CHINEXT"},
            {"code": "600000.SH", "name": "DEMO_MAIN_SH", "listing_board": "MAIN_SH"},
            {"code": "601398.SH", "name": "DEMO_MAIN_SH_2", "listing_board": "MAIN_SH"},
            {"code": "688001.SH", "name": "DEMO_STAR", "listing_board": "STAR"},
        ]
    )


def _demo_predictions(seed: int, as_of: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(int(seed))
    formula_rows: list[dict[str, Any]] = []
    jev_rows: list[dict[str, Any]] = []
    for code in _demo_universe()["code"]:
        features = {f"F{index:02d}": float(rng.uniform(-0.8, 0.8)) for index in range(1, 16)}
        for horizon in (1, 3, 5):
            score = score_stock(features, horizon, "F0_BALANCED")
            formula_rows.append(
                {
                    "entity_type": "stock",
                    "entity_id": code,
                    "as_of_trade_date": as_of,
                    "horizon": horizon,
                    "method": "formula",
                    "prediction_status": score.status,
                    "forecast_class": score.forecast_class,
                    "formula_score": score.formula_score,
                    "probability_validation_status": None,
                    "data_source_mode": "demo",
                }
            )
            probabilities = rng.dirichlet(np.array([4.0, 3.0, 2.0]))
            jev_rows.append(
                {
                    "entity_type": "stock",
                    "entity_id": code,
                    "as_of_trade_date": as_of,
                    "horizon": horizon,
                    "method": "jev",
                    "prediction_status": "OK",
                    "forecast_class": ("up", "flat", "down")[int(np.argmax(probabilities))],
                    "p_raw_up": float(probabilities[0]),
                    "p_raw_flat": float(probabilities[1]),
                    "p_raw_down": float(probabilities[2]),
                    "probability_validation_status": "SYNTHETIC_DEMO_ONLY",
                    "response_source": "SYNTHETIC_DEMO_RESPONSE",
                    "data_source_mode": "demo",
                }
            )
    return pd.DataFrame(formula_rows), pd.DataFrame(jev_rows)


def _demo(args: argparse.Namespace) -> CommandOutcome:
    _, artifact_root = _mode_paths(args)
    as_of = _compact_date(args.as_of or DEFAULT_DEMO_AS_OF)
    universe = _demo_universe()
    formula, jev = _demo_predictions(args.seed, as_of)
    semantic_payload = {
        "seed": int(args.seed),
        "as_of_trade_date": as_of,
        "universe": universe.to_dict(orient="records"),
        "formula": formula.to_dict(orient="records"),
        "jev": jev.to_dict(orient="records"),
    }
    artifact_hash = sha256_json(semantic_payload)
    run_id = f"demo-{as_of}-{artifact_hash[:12]}"
    with command_lock(artifact_root / ".command.lock"):
        result = TechnicalV2Pipeline(artifact_root).run(
            run_id=run_id,
            as_of_trade_date=as_of,
            information_cutoff=f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:]}T16:10:00+08:00",
            mode="EOD_FINAL",
            data_source_mode="demo",
            data_hash=sha256_json({"source": "SYNTHETIC_FIXTURE", "seed": int(args.seed)}),
            config_hash=_config_hash(),
            code_hash=_code_hash(),
            universe=universe,
            formula_predictions=formula,
            jev_predictions=jev,
        )
    payload = {
        **_common_payload("demo", "demo"),
        "status": "DEMO_ONLY",
        "data_source_mode": "demo",
        "run_id": result.run_id,
        "publication_id": result.publication_id,
        "artifact_hash": artifact_hash,
        "coverage_rows": result.coverage_rows,
        "valid_prediction_rows": result.valid_prediction_rows,
        "artifact_root": str(artifact_root),
        "network_calls": 0,
        "paid_calls": 0,
        "warning": "Synthetic demo output is not research evidence.",
    }
    return CommandOutcome(payload, 0)


def _sync_demo(args: argparse.Namespace, store: V2Store) -> dict[str, Any]:
    as_of = _compact_date(args.as_of or DEFAULT_DEMO_AS_OF)
    count = max(1, int(args.history_sessions))
    sessions = [value.strftime("%Y%m%d") for value in pd.bdate_range(end=pd.Timestamp(as_of), periods=count)]
    future_sessions = [
        value.strftime("%Y%m%d")
        for value in pd.bdate_range(start=pd.Timestamp(as_of) + pd.Timedelta(days=1), periods=10)
    ]
    calendar_sessions = sessions + future_sessions
    calendar_rows = []
    for exchange in ("SSE", "SZSE"):
        for index, session in enumerate(calendar_sessions):
            calendar_rows.append(
                {
                    "exchange": exchange,
                    "date": session,
                    "is_open": 1,
                    "previous_session": calendar_sessions[index - 1] if index else None,
                    "next_session": (
                        calendar_sessions[index + 1]
                        if index + 1 < len(calendar_sessions)
                        else None
                    ),
                    "source": "SYNTHETIC_FIXTURE",
                    "source_version": f"demo-calendar-{args.seed}",
                    "retrieved_at": f"{as_of}T16:10:00+08:00",
                }
            )
    store.upsert_calendar(pd.DataFrame(calendar_rows))
    provider = DemoV2Provider(seed=int(args.seed))
    pending_sessions = store.missing_sync_sessions(
        "SSE",
        sessions,
        force_refresh=bool(getattr(args, "force_refresh", False)),
    )
    daily_rows = []
    for session in pending_sessions:
        daily_rows.append(provider.daily(session).frame)
    daily = pd.concat(daily_rows, ignore_index=True) if daily_rows else pd.DataFrame()
    if not daily.empty:
        store.upsert_daily_raw(daily)
    codes = sorted(_demo_universe()["code"].astype(str).unique())
    instruments = pd.DataFrame(
        [
            {
                "code": code,
                "instrument_type": "stock",
                "exchange": "SSE" if code.endswith(".SH") else "SZSE",
                "listing_board": _demo_universe().set_index("code").loc[code, "listing_board"],
                "name": _demo_universe().set_index("code").loc[code, "name"],
                "list_date": "20200101",
                "delist_date": None,
                "listing_status": "L",
                "valid_from": sessions[0],
                "valid_to": None,
                "source": "SYNTHETIC_FIXTURE",
                "source_version": f"demo-instruments-{args.seed}",
                "observed_at": f"{as_of}T16:10:00+08:00",
            }
            for code in codes
        ]
    )
    store.upsert_instrument_versions(instruments)
    memberships = pd.DataFrame(
        [
            {
                "namespace": "STOCK_BASIC_INDUSTRY",
                "sector_id": "DEMO_TECHNICAL",
                "code": code,
                "valid_from": sessions[0],
                "valid_to": None,
                "observed_at": f"{as_of}T16:10:00+08:00",
                "history_mode": "RECONSTRUCTED_PIT",
                "source": "SYNTHETIC_FIXTURE",
                "source_version": f"demo-membership-{args.seed}",
            }
            for code in codes
        ]
    )
    store.upsert_sector_membership(memberships)
    adjustments = pd.DataFrame()
    if not daily.empty:
        adjustments = daily[["code", "date"]].copy()
        adjustments["source_version"] = f"demo-adjustments-{args.seed}"
        adjustments["adj_factor"] = 1.0
        adjustments["source"] = "SYNTHETIC_FIXTURE"
        adjustments["retrieved_at"] = f"{as_of}T16:10:00+08:00"
        store.upsert_adjustments(adjustments)
    audit_rows = []
    for session in pending_sessions:
        observed = int(daily.loc[daily["date"].eq(session), "code"].nunique())
        audit_rows.append(
            {
                "exchange": "SSE",
                "date": session,
                "source_version": f"demo-sync-{args.seed}",
                "expected_instruments": len(codes),
                "observed_rows": observed,
                "known_non_trading_rows": 0,
                "coverage": min(1.0, observed / len(codes)) if codes else 0.0,
                "status": "COMPLETE" if observed == len(codes) else "PARTIAL",
                "details": {"data_source_mode": "demo", "seed": int(args.seed)},
            }
        )
    store.upsert_sync_audits(audit_rows)
    return {
        "as_of_trade_date": as_of,
        "sessions": len(sessions),
        "sessions_fetched": len(pending_sessions),
        "daily_rows": len(daily),
        "adjustment_rows": len(adjustments),
        "instrument_rows": len(instruments),
        "sector_membership_rows": len(memberships),
    }


def _current_industry_memberships(instruments: pd.DataFrame, as_of: str) -> pd.DataFrame:
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
    if instruments.empty or "industry" not in instruments.columns:
        return pd.DataFrame(columns=columns)
    work = instruments.loc[instruments["industry"].notna()].copy()
    work["sector_id"] = work["industry"].astype(str).str.strip()
    work = work.loc[work["sector_id"].ne("")]
    observed_at = pd.Timestamp.now(tz="UTC").isoformat()
    return pd.DataFrame(
        {
            "namespace": "STOCK_BASIC_INDUSTRY",
            "sector_id": work["sector_id"],
            "code": work["code"].astype(str),
            "valid_from": as_of,
            "valid_to": None,
            "observed_at": observed_at,
            "history_mode": "CURRENT_SNAPSHOT_ONLY",
            "source": "TUSHARE_STOCK_BASIC",
            "source_version": "tushare-stock-basic-industry-v1",
        },
        columns=columns,
    )


def _sync(args: argparse.Namespace) -> CommandOutcome:
    db_path, artifact_root = _mode_paths(args)
    if args.mode == "real" and not os.getenv("TUSHARE_TOKEN", "").strip():
        return CommandOutcome(
            {
                **_common_payload("sync", args.mode),
                "status": "PREREQUISITE_MISSING",
                "missing": ["TUSHARE_TOKEN"],
                "rows_written": 0,
            },
            2,
        )
    store = V2Store(db_path, data_mode=args.mode)
    with command_lock(artifact_root / ".command.lock"):
        store.migrate()
        if args.mode in {"demo", "test"}:
            details = _sync_demo(args, store)
        else:
            provider = RealV2Provider(os.getenv("TUSHARE_TOKEN", ""))
            today = pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y%m%d")
            start = (pd.Timestamp(today) - pd.Timedelta(days=max(60, int(args.history_sessions) * 2))).strftime(
                "%Y%m%d"
            )
            calendar_end = (pd.Timestamp(today) + pd.Timedelta(days=45)).strftime("%Y%m%d")
            calendar_results = [
                provider.calendar(exchange, start, calendar_end)
                for exchange in ("SSE", "SZSE")
            ]
            if any(result.status not in {"OK", "PARTIAL"} for result in calendar_results):
                return CommandOutcome(
                    {
                        **_common_payload("sync", args.mode),
                        "status": "PREREQUISITE_MISSING",
                        "provider_status": {
                            exchange: result.status
                            for exchange, result in zip(("SSE", "SZSE"), calendar_results)
                        },
                        "provider_code": {
                            exchange: result.code
                            for exchange, result in zip(("SSE", "SZSE"), calendar_results)
                        },
                        "rows_written": 0,
                    },
                    2,
                )
            for calendar in calendar_results:
                store.upsert_calendar(calendar.frame)
            sessions = store.open_sessions("SSE", today, int(args.history_sessions))
            instruments = provider.instruments(today)
            instrument_rows = (
                store.upsert_instrument_versions(instruments.frame)
                if instruments.status in {"OK", "PARTIAL"}
                else 0
            )
            analysis_universe = (
                build_analysis_universe(instruments.frame, today)
                if instruments.status in {"OK", "PARTIAL"}
                else pd.DataFrame()
            )
            expected_codes = set(analysis_universe.get("ts_code", pd.Series(dtype=str)).astype(str))
            pending_sessions = store.missing_sync_sessions(
                "SSE",
                sessions,
                force_refresh=bool(getattr(args, "force_refresh", False)),
            )
            daily_written = 0
            adjustment_rows = 0
            trading_status_rows = 0
            failures: list[dict[str, Any]] = []
            audit_rows: list[dict[str, Any]] = []
            for session in pending_sessions:
                result = provider.daily(session)
                if result.status in {"OK", "PARTIAL"}:
                    daily_written += store.upsert_daily_raw(result.frame)
                else:
                    failures.append(
                        {"endpoint": "daily", "date": session, "status": result.status, "code": result.code}
                    )
                adjustment = provider.adjustments(session)
                if adjustment.status in {"OK", "PARTIAL"}:
                    adjustment_rows += store.upsert_adjustments(adjustment.frame)
                else:
                    failures.append(
                        {
                            "endpoint": "adj_factor",
                            "date": session,
                            "status": adjustment.status,
                            "code": adjustment.code,
                        }
                    )
                limits = provider.limits(session)
                if limits.status in {"OK", "PARTIAL"}:
                    if not limits.frame.empty:
                        limits.frame["is_suspended"] = False
                    trading_status_rows += store.upsert_trading_status(limits.frame)
                else:
                    failures.append(
                        {
                            "endpoint": "stk_limit",
                            "date": session,
                            "status": limits.status,
                            "code": limits.code,
                        }
                    )
                suspensions = provider.suspensions(session)
                if suspensions.status in {"OK", "PARTIAL"}:
                    trading_status_rows += store.upsert_trading_status(suspensions.frame)
                elif suspensions.status != "NO_DATA":
                    failures.append(
                        {
                            "endpoint": "suspend_d",
                            "date": session,
                            "status": suspensions.status,
                            "code": suspensions.code,
                        }
                    )
                observed_codes = (
                    set(result.frame["code"].astype(str)).intersection(expected_codes)
                    if not result.frame.empty and "code" in result.frame.columns
                    else set()
                )
                suspended_codes = (
                    set(
                        suspensions.frame.loc[
                            suspensions.frame["is_suspended"].fillna(False).astype(bool), "code"
                        ].astype(str)
                    ).intersection(expected_codes)
                    if not suspensions.frame.empty and "is_suspended" in suspensions.frame.columns
                    else set()
                )
                accounted = observed_codes | suspended_codes
                coverage = len(accounted) / len(expected_codes) if expected_codes else 0.0
                endpoint_statuses = {
                    "daily": result.status,
                    "adjustments": adjustment.status,
                    "limits": limits.status,
                    "suspensions": suspensions.status,
                }
                complete = (
                    bool(expected_codes)
                    and coverage >= 0.9
                    and result.status == "OK"
                    and adjustment.status == "OK"
                    and limits.status == "OK"
                    and suspensions.status in {"OK", "NO_DATA"}
                )
                audit_rows.append(
                    {
                        "exchange": "SSE",
                        "date": session,
                        "source_version": "technical-v2-sync-v1",
                        "expected_instruments": len(expected_codes),
                        "observed_rows": len(observed_codes),
                        "known_non_trading_rows": len(suspended_codes - observed_codes),
                        "coverage": min(1.0, coverage),
                        "status": "COMPLETE" if complete else "PARTIAL",
                        "details": {"endpoint_statuses": endpoint_statuses},
                    }
                )
            store.upsert_sync_audits(audit_rows)
            classifications = provider.sector_classifications()
            memberships_result = None
            if classifications.status in {"OK", "PARTIAL"} and not classifications.frame.empty:
                memberships_result = provider.sector_memberships(
                    classifications.frame["sector_id"].astype(str).tolist()
                )
            if memberships_result is not None and memberships_result.status in {"OK", "PARTIAL"}:
                memberships = memberships_result.frame
                sector_history_mode = "RECONSTRUCTED_PIT"
                sector_status = memberships_result.status
            else:
                memberships = (
                    _current_industry_memberships(instruments.frame, today)
                    if instruments.status in {"OK", "PARTIAL"}
                    else pd.DataFrame()
                )
                sector_history_mode = "CURRENT_SNAPSHOT_ONLY"
                sector_status = "FALLBACK"
                failures.append(
                    {
                        "endpoint": "SW2021_L1",
                        "status": (
                            memberships_result.status
                            if memberships_result is not None
                            else classifications.status
                        ),
                        "code": (
                            memberships_result.code
                            if memberships_result is not None
                            else classifications.code
                        ),
                    }
                )
            membership_rows = store.upsert_sector_membership(memberships)
            corporate_action_status = "NOT_REQUESTED"
            corporate_action_rows = 0
            if args.include_corporate_actions and not instruments.frame.empty:
                actions = provider.corporate_actions(instruments.frame["code"].astype(str).tolist())
                corporate_action_status = actions.status
                if actions.status in {"OK", "PARTIAL"}:
                    corporate_action_rows = store.upsert_corporate_actions(actions.frame)
                elif actions.status != "NO_DATA":
                    failures.append(
                        {
                            "endpoint": "dividend",
                            "status": actions.status,
                            "code": actions.code,
                        }
                    )
            details = {
                "as_of_trade_date": sessions[-1] if sessions else None,
                "sessions": len(sessions),
                "sessions_fetched": len(pending_sessions),
                "daily_rows": daily_written,
                "adjustment_rows": adjustment_rows,
                "trading_status_rows": trading_status_rows,
                "instrument_rows": instrument_rows,
                "sector_membership_rows": membership_rows,
                "sector_history_mode": sector_history_mode,
                "sector_status": sector_status,
                "corporate_action_status": corporate_action_status,
                "corporate_action_rows": corporate_action_rows,
                "failed_partitions": failures,
            }
    payload = {
        **_common_payload("sync", args.mode),
        "status": "PARTIAL" if details.get("failed_partitions") else "OK",
        "data_source_mode": args.mode,
        "db_path": str(db_path),
        **details,
    }
    return CommandOutcome(payload, 0)


def _latest_or_requested(args: argparse.Namespace, store: V2Store) -> tuple[str | None, dict[str, Any]]:
    freshness = resolve_latest_as_of(store)
    requested = str(getattr(args, "as_of", "latest") or "latest")
    actual = freshness["actual_as_of"] if requested == "latest" else _compact_date(requested)
    return actual, freshness


def _frame_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in frame.astype(object).where(pd.notna(frame), None).to_dict(orient="records"):
        records.append(json_safe(raw))
    return records


def _compute_feature_snapshots(
    store: V2Store,
    universe: pd.DataFrame,
    as_of: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    sessions = store.open_sessions("SSE", as_of, 120)
    if not sessions:
        return pd.DataFrame(), pd.DataFrame()
    codes = universe["code"].astype(str).tolist()
    raw = store.read_daily_raw(sessions[0], as_of, codes)
    if not raw.empty:
        raw = raw.sort_values(["code", "date", "retrieved_at", "source_version"]).drop_duplicates(
            ["code", "date"], keep="last"
        )
    adjustments = store.read_adjustments(sessions[0], as_of, codes)
    if raw.empty or adjustments.empty:
        return pd.DataFrame(), pd.DataFrame()
    panel = apply_adjustment_factors(raw, adjustments, as_of=as_of)
    panel["mark_only"] = False
    memberships = store.read_sector_membership(as_of)
    context = build_context(
        panel,
        memberships,
        as_of=as_of,
        universe=codes,
        sessions=sessions,
    )
    features = compute_technical_v2(panel, context, as_of=as_of)
    latest = features.loc[features["date"].eq(as_of)].copy()
    columns = [
        "code",
        "date",
        *DIRECTIONAL_FACTOR_IDS,
        *RISK_INDICATOR_IDS,
        *(f"{factor_id}_reason" for factor_id in DIRECTIONAL_FACTOR_IDS + RISK_INDICATOR_IDS),
        "factor_valid_count",
        "feature_status",
        "price_basis",
        "observed_real_bars60",
        "overextended",
        "close",
        "open",
        "amount_cny",
        "namespace",
        "sector_id",
        "market_context_status",
        "market_context_reason",
        "sector_context_status",
        "sector_context_reason",
    ]
    available = [column for column in columns if column in latest.columns]
    snapshot = universe.merge(latest[available], on="code", how="left", validate="one_to_one")
    snapshot["date"] = snapshot.get("date", pd.Series(index=snapshot.index, dtype=object)).fillna(as_of)
    snapshot["factor_valid_count"] = pd.to_numeric(
        snapshot.get("factor_valid_count", pd.Series(index=snapshot.index, dtype=float)),
        errors="coerce",
    ).fillna(0).astype(int)
    missing = snapshot["feature_status"].isna()
    snapshot.loc[missing, "feature_status"] = "DATA_UNAVAILABLE"
    active_assignments = context.assignments.loc[context.assignments["date"].eq(as_of)]
    if not active_assignments.empty:
        mode_by_code = (
            active_assignments.sort_values(["code", "namespace", "sector_id"])
            .drop_duplicates("code", keep="first")
            .set_index("code")["history_mode"]
        )
        snapshot["sector_history_mode"] = snapshot["code"].map(mode_by_code)
    else:
        snapshot["sector_history_mode"] = None
    stock_snapshot = snapshot.sort_values("code").reset_index(drop=True)

    sector_snapshot = pd.DataFrame()
    if context.sectors is not None and not context.sectors.empty:
        sector_snapshot = context.sectors.loc[context.sectors["date"].eq(as_of)].copy()
        if not sector_snapshot.empty:
            market_latest = context.market.loc[context.market["date"].eq(as_of)]
            for source, target in (
                ("log_return20", "market_log_return20"),
                ("breadth20", "market_breadth20"),
                ("sigma20", "market_sigma20"),
            ):
                sector_snapshot[target] = (
                    market_latest.iloc[0].get(source) if not market_latest.empty else None
                )
            sector_snapshot["entity_id"] = (
                sector_snapshot["namespace"].astype(str)
                + ":"
                + sector_snapshot["sector_id"].astype(str)
            )
            sector_snapshot["sector_name"] = sector_snapshot["sector_id"].astype(str)
            history_modes = (
                context.assignments.loc[context.assignments["date"].eq(as_of)]
                .groupby(["namespace", "sector_id"], sort=False)["history_mode"]
                .agg(lambda values: min(set(values.astype(str))))
                .rename("sector_history_mode")
                .reset_index()
            )
            sector_snapshot = sector_snapshot.merge(
                history_modes,
                on=["namespace", "sector_id"],
                how="left",
                validate="one_to_one",
            )
            sector_snapshot["distance_to_20d_high"] = (
                pd.to_numeric(sector_snapshot["index_level"], errors="coerce")
                / pd.to_numeric(sector_snapshot["high20prev"], errors="coerce")
                - 1.0
            )
            sector_snapshot["close_range_position_20"] = sector_snapshot["range_position20"]
            sector_snapshot["sector_breadth20"] = sector_snapshot["breadth20"]
            sector_snapshot["z5"] = sector_snapshot["signed_amount_surprise"]
            sigma = pd.to_numeric(sector_snapshot["sigma20"], errors="coerce").clip(lower=0.005)
            sector_snapshot["z1"] = np.tanh(
                pd.to_numeric(sector_snapshot["log_return20"], errors="coerce")
                / (sigma * np.sqrt(20.0))
            )
            sector_snapshot["z2"] = np.tanh(
                pd.to_numeric(sector_snapshot["log_return60"], errors="coerce")
                / (sigma * np.sqrt(60.0))
            )
            sector_snapshot["z3"] = np.tanh(
                (
                    pd.to_numeric(sector_snapshot["log_return20"], errors="coerce")
                    - pd.to_numeric(sector_snapshot["market_log_return20"], errors="coerce")
                )
                / (sigma * np.sqrt(20.0))
            )
            sector_snapshot["z4"] = 2.0 * pd.to_numeric(
                sector_snapshot["breadth20"], errors="coerce"
            ) - 1.0
            market_scale = pd.to_numeric(
                sector_snapshot["market_sigma20"], errors="coerce"
            ).clip(lower=0.005)
            sector_snapshot["market_trend"] = np.tanh(
                pd.to_numeric(sector_snapshot["market_log_return20"], errors="coerce")
                / (market_scale * np.sqrt(20.0))
            )
            sector_snapshot["amount_ratio20"] = None
            sector_snapshot["feature_valid_count"] = sector_snapshot[
                ["z1", "z2", "z3", "z4", "z5"]
            ].notna().sum(axis=1)
            sector_snapshot["feature_status"] = np.where(
                sector_snapshot["feature_valid_count"].eq(5),
                "OK",
                "CONTEXT_UNAVAILABLE",
            )
            sector_snapshot = sector_snapshot.sort_values("entity_id").reset_index(drop=True)
    return stock_snapshot, sector_snapshot


def _build_formula_predictions(
    features: pd.DataFrame,
    *,
    as_of: str,
) -> tuple[pd.DataFrame, dict[str, dict[int, Any]]]:
    rows: list[dict[str, Any]] = []
    scores: dict[str, dict[int, Any]] = {}
    for feature in features.to_dict(orient="records"):
        code = str(feature["code"])
        scores[code] = {}
        for horizon in (1, 3, 5):
            score = score_stock(feature, horizon, "F0_BALANCED")
            scores[code][horizon] = score
            rows.append(
                {
                    "entity_type": "stock",
                    "entity_id": code,
                    "as_of_trade_date": as_of,
                    "horizon": horizon,
                    "method": "formula",
                    "prediction_status": score.status,
                    "formula_config_id": score.config_id,
                    "formula_score": score.formula_score,
                    "forecast_class": score.forecast_class,
                    "observed_trend": score.observed_trend,
                    "group_scores": score.group_scores,
                    "missing_factor_ids": score.missing_factor_ids,
                    "reason_codes": score.reason_codes,
                    "probability_validation_status": None,
                }
            )
    predictions = pd.DataFrame(rows)
    predictions["research_intent"] = None
    predictions["account_action"] = None
    predictions["order_quantity"] = None
    predictions["intent_reason_codes"] = [() for _ in range(len(predictions))]
    for indexes in predictions.groupby("entity_id", sort=False).groups.values():
        entity_rows = predictions.loc[indexes].to_dict(orient="records")
        for index in indexes:
            decision = derive_research_intent(
                entity_rows,
                None,
                horizon=int(predictions.at[index, "horizon"]),
            )
            predictions.at[index, "research_intent"] = decision.research_intent
            predictions.at[index, "account_action"] = decision.account_action
            predictions.at[index, "order_quantity"] = decision.order_quantity
            predictions.at[index, "intent_reason_codes"] = decision.reason_codes
    return predictions, scores


def _build_sector_formula_predictions(
    features: pd.DataFrame,
    *,
    as_of: str,
) -> tuple[pd.DataFrame, dict[str, dict[int, Any]]]:
    rows: list[dict[str, Any]] = []
    scores: dict[str, dict[int, Any]] = {}
    for feature in features.to_dict(orient="records"):
        entity_id = str(feature["entity_id"])
        scores[entity_id] = {}
        for horizon in (1, 3, 5):
            score = score_sector(feature, horizon)
            scores[entity_id][horizon] = score
            rows.append(
                {
                    "entity_type": "sector",
                    "entity_id": entity_id,
                    "as_of_trade_date": as_of,
                    "horizon": horizon,
                    "method": "formula",
                    "prediction_status": score.status,
                    "formula_config_id": "SECTOR_FIXED_V1",
                    "formula_score": score.formula_score,
                    "forecast_class": score.forecast_class,
                    "observed_trend": score.observed_trend,
                    "factor_values": score.components,
                    "factor_group_scores": score.components,
                    "research_intent": (
                        score.sector_intent if horizon == 5 else "DIAGNOSTIC_ONLY"
                    ),
                    "account_action": None,
                    "order_quantity": None,
                    "reason_codes": score.reason_codes,
                    "intent_reason_codes": (),
                    "probability_validation_status": None,
                }
            )
    return pd.DataFrame(rows), scores


def _unavailable_jev_rows(
    features: pd.DataFrame,
    *,
    as_of: str,
    status: str,
    entity_type: str = "stock",
    entity_id_column: str = "code",
    method: str = "jev",
) -> pd.DataFrame:
    if features.empty or entity_id_column not in features.columns:
        return pd.DataFrame(
            columns=[
                "entity_type",
                "entity_id",
                "as_of_trade_date",
                "horizon",
                "method",
                "prediction_status",
                "forecast_class",
                "probability_validation_status",
                "reason_codes",
            ]
        )
    return pd.DataFrame(
        [
            {
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "as_of_trade_date": as_of,
                "horizon": horizon,
                "method": method,
                "prediction_status": status,
                "forecast_class": "unknown",
                "probability_validation_status": status,
                "reason_codes": (status,),
            }
            for entity_id in features[entity_id_column]
            for horizon in (1, 3, 5)
        ]
    )


def _build_jev_predictions(
    features: pd.DataFrame,
    formula_scores: dict[str, dict[int, Any]],
    *,
    as_of: str,
    db_path: Path,
    entity_type: str = "stock",
    entity_id_column: str = "code",
) -> tuple[pd.DataFrame, str, bool]:
    api_key = os.getenv("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        return (
            _unavailable_jev_rows(
                features,
                as_of=as_of,
                status="UNAVAILABLE_CREDENTIALS",
                entity_type=entity_type,
                entity_id_column=entity_id_column,
            ),
            "UNAVAILABLE_CREDENTIALS",
            True,
        )
    config = TechnicalV2Config.load(CONFIG_PATH)
    budget = JevBudget.from_verified_snapshot(
        development_limit_usd=config.jev.development_budget_usd,
        daily_limit_usd=config.jev.daily_budget_usd,
    )
    client = JevClient(
        api_key=api_key,
        api_base=config.jev.api_base,
        endpoint=config.jev.endpoint,
        model=config.jev.model,
        timeout_seconds=config.jev.timeout_seconds,
        extra_retries=config.jev.extra_retries,
        requests_per_minute=config.jev.requests_per_minute,
        cache_path=db_path.with_name("jev_responses.db"),
        default_budget=budget,
    )
    questions = build_questions(entity_type)
    rows: list[dict[str, Any]] = []
    external_failure = False
    route_statuses: list[str] = []
    for feature in features.to_dict(orient="records"):
        entity_id = str(feature[entity_id_column])
        sigma_key = "Q02" if entity_type == "stock" else "sigma20"
        count_key = "factor_valid_count" if entity_type == "stock" else "feature_valid_count"
        minimum_count = 12 if entity_type == "stock" else 5
        sigma = pd.to_numeric(pd.Series([feature.get(sigma_key)]), errors="coerce").iloc[0]
        if pd.isna(sigma) or int(feature.get(count_key) or 0) < minimum_count:
            rows.extend(
                _unavailable_jev_rows(
                    pd.DataFrame({entity_id_column: [entity_id]}),
                    as_of=as_of,
                    status="INSUFFICIENT_FEATURES",
                    entity_type=entity_type,
                    entity_id_column=entity_id_column,
                ).to_dict(orient="records")
            )
            route_statuses.append("INSUFFICIENT_FEATURES")
            continue
        targets = {
            horizon: max(0.005 if entity_type == "stock" else 0.002, 0.25 * float(sigma) * np.sqrt(horizon))
            for horizon in (1, 3, 5)
        }
        state = build_jev_state(
            entity_type,
            entity_id,
            feature,
            targets=targets,
            group_scores=(
                formula_scores[entity_id][5].group_scores
                if entity_type == "stock"
                else None
            ),
        )
        result = client.predict(state, questions, budget)
        if result.status != "OK" or result.response is None:
            rows.extend(
                _unavailable_jev_rows(
                    pd.DataFrame({entity_id_column: [entity_id]}),
                    as_of=as_of,
                    status=result.status,
                    entity_type=entity_type,
                    entity_id_column=entity_id_column,
                ).to_dict(orient="records")
            )
            route_statuses.append(result.status)
            external_failure = True
            continue
        validated = validate_answers(result.response, tuple(questions))
        if validated.status not in {"OK", "PARTIAL_RESPONSE"}:
            rows.extend(
                _unavailable_jev_rows(
                    pd.DataFrame({entity_id_column: [entity_id]}),
                    as_of=as_of,
                    status=validated.status,
                    entity_type=entity_type,
                    entity_id_column=entity_id_column,
                ).to_dict(orient="records")
            )
            route_statuses.append(validated.status)
            continue
        for horizon in (1, 3, 5):
            pooled = pool_probabilities(validated.answers, "J0_EQUAL_POOL", horizon)
            probabilities = pooled.probabilities or {}
            forecast = max(probabilities, key=probabilities.get) if probabilities else "unknown"
            rows.append(
                {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "as_of_trade_date": as_of,
                    "horizon": horizon,
                    "method": "jev",
                    "prediction_status": pooled.status,
                    "pool_id": pooled.pool_id,
                    "forecast_class": forecast,
                    "p_raw_up": probabilities.get("up"),
                    "p_raw_flat": probabilities.get("flat"),
                    "p_raw_down": probabilities.get("down"),
                    "p_cal_up": None,
                    "p_cal_flat": None,
                    "p_cal_down": None,
                    "probability_validation_status": "JEV_SHADOW_UNVALIDATED",
                    "response_source": "CACHE" if result.from_cache else "LIVE_JEV",
                    "model_returned": validated.model_returned,
                    "reason_codes": pooled.reason_codes,
                }
            )
            route_statuses.append(pooled.status)
    predictions = pd.DataFrame(rows)
    if not predictions.empty:
        predictions["research_intent"] = None
        predictions["account_action"] = None
        predictions["order_quantity"] = None
        predictions["intent_reason_codes"] = [() for _ in range(len(predictions))]
        for indexes in predictions.groupby("entity_id", sort=False).groups.values():
            entity_rows = predictions.loc[indexes].to_dict(orient="records")
            for index in indexes:
                horizon = int(predictions.at[index, "horizon"])
                if entity_type == "stock":
                    decision = derive_research_intent(entity_rows, None, horizon=horizon)
                    predictions.at[index, "research_intent"] = decision.research_intent
                    predictions.at[index, "account_action"] = decision.account_action
                    predictions.at[index, "order_quantity"] = decision.order_quantity
                    predictions.at[index, "intent_reason_codes"] = decision.reason_codes
                else:
                    forecast = str(predictions.at[index, "forecast_class"])
                    predictions.at[index, "research_intent"] = (
                        "DIAGNOSTIC_ONLY"
                        if horizon != 5
                        else {
                            "up": "INCREASE_EXPOSURE",
                            "flat": "MAINTAIN",
                            "down": "REDUCE_EXPOSURE",
                        }.get(forecast, "OBSERVE")
                    )
    status = "OK" if route_statuses and set(route_statuses) == {"OK"} else "PARTIAL"
    return predictions, status, external_failure


PREDICTION_OUTPUT_FIELDS = (
    "schema_version",
    "run_id",
    "entity_type",
    "entity_id",
    "ts_code_local_only",
    "exchange",
    "listing_board",
    "sector_namespace",
    "sector_id",
    "sector_name",
    "as_of_trade_date",
    "information_cutoff",
    "generated_at",
    "earliest_entry_date",
    "reference_exit_date",
    "horizon",
    "target_definition_version",
    "data_source_mode",
    "sector_history_mode",
    "feature_coverage",
    "prediction_status",
    "missing_reason_codes",
    "method",
    "config_id",
    "model_id",
    "calibration_id",
    "evidence_status",
    "factor_values",
    "factor_group_scores",
    "risk_metrics",
    "reason_codes",
    "observed_trend",
    "forecast_class",
    "formula_score",
    "p_raw_up",
    "p_raw_flat",
    "p_raw_down",
    "p_cal_up",
    "p_cal_flat",
    "p_cal_down",
    "expected_gross_return",
    "estimated_round_trip_cost",
    "expected_net_edge",
    "return_estimate_basis",
    "research_intent",
    "account_action",
    "action_blockers",
    "current_quantity",
    "sellable_quantity",
    "target_quantity",
    "order_quantity",
    "reference_price",
    "entry_price_ceiling",
    "planned_stop_price",
    "planned_exit_date",
    "order_status",
)


def _clean_value(value: Any) -> Any:
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return json_safe(value)


def _string_tuple(value: Any) -> tuple[str, ...]:
    clean = _clean_value(value)
    if clean is None:
        return ()
    if isinstance(clean, (list, tuple, set)):
        return tuple(str(item) for item in clean)
    return (str(clean),)


def _first_value(*values: Any) -> Any:
    for value in values:
        clean = _clean_value(value)
        if clean is not None:
            return clean
    return None


def _session_offset(store: V2Store, date: str, offset: int) -> str | None:
    current = str(date)
    for _ in range(max(0, int(offset))):
        current = store.next_session("SSE", current) or ""
        if not current:
            return None
    return current


def _normalize_prediction_rows(
    rows: pd.DataFrame,
    *,
    run_id: str,
    as_of: str,
    information_cutoff: str,
    data_source_mode: str,
    store: V2Store,
    stock_features: pd.DataFrame,
    sector_features: pd.DataFrame,
) -> pd.DataFrame:
    stock_lookup = {
        str(row["code"]): row for row in stock_features.to_dict(orient="records")
    }
    sector_lookup = {
        str(row["entity_id"]): row for row in sector_features.to_dict(orient="records")
    }
    earliest_entry = _session_offset(store, as_of, 1)
    normalized: list[dict[str, Any]] = []
    for raw in rows.to_dict(orient="records"):
        entity_type = str(raw["entity_type"])
        entity_id = str(raw["entity_id"])
        horizon = int(raw["horizon"])
        feature = stock_lookup.get(entity_id, {}) if entity_type == "stock" else sector_lookup.get(entity_id, {})
        reference_exit = (
            _session_offset(store, earliest_entry, horizon) if earliest_entry is not None else None
        )
        factor_ids = DIRECTIONAL_FACTOR_IDS if entity_type == "stock" else tuple(
            f"z{index}" for index in range(1, 6)
        )
        risk_ids = RISK_INDICATOR_IDS if entity_type == "stock" else ()
        factor_values = raw.get("factor_values")
        if not isinstance(factor_values, dict):
            factor_values = {
                factor_id: _clean_value(feature.get(factor_id)) for factor_id in factor_ids
            }
        risk_metrics = {
            risk_id: _clean_value(feature.get(risk_id)) for risk_id in risk_ids
        }
        reason_codes = _string_tuple(raw.get("reason_codes"))
        missing_reasons = tuple(
            dict.fromkeys(
                [
                    *reason_codes,
                    *_string_tuple(raw.get("missing_factor_ids")),
                ]
            )
        )
        if entity_type == "stock":
            valid_count = int(feature.get("factor_valid_count") or 0)
            feature_coverage = valid_count / len(DIRECTIONAL_FACTOR_IDS)
            namespace = _clean_value(feature.get("namespace"))
            sector_id = _clean_value(feature.get("sector_id"))
        else:
            valid_count = int(feature.get("feature_valid_count") or 0)
            feature_coverage = valid_count / 5.0
            namespace = _clean_value(feature.get("namespace"))
            sector_id = _clean_value(feature.get("sector_id"))
        method = str(raw["method"])
        probability_status = _clean_value(raw.get("probability_validation_status"))
        record: dict[str, Any] = {field: None for field in PREDICTION_OUTPUT_FIELDS}
        record.update(
            {
                "schema_version": "standard.technical-v2.prediction.v1",
                "run_id": run_id,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "ts_code_local_only": entity_id if entity_type == "stock" else None,
                "exchange": _clean_value(feature.get("exchange")),
                "listing_board": _clean_value(feature.get("listing_board")),
                "sector_namespace": namespace,
                "sector_id": sector_id,
                "sector_name": (
                    _clean_value(feature.get("sector_name")) if entity_type == "sector" else None
                ),
                "as_of_trade_date": as_of,
                "information_cutoff": information_cutoff,
                "generated_at": information_cutoff,
                "earliest_entry_date": earliest_entry,
                "reference_exit_date": reference_exit,
                "horizon": horizon,
                "target_definition_version": TARGET_DEFINITION_VERSION,
                "data_source_mode": data_source_mode,
                "sector_history_mode": _clean_value(feature.get("sector_history_mode")),
                "feature_coverage": feature_coverage,
                "prediction_status": str(raw["prediction_status"]),
                "missing_reason_codes": missing_reasons,
                "method": method,
                "config_id": _first_value(raw.get("formula_config_id"), raw.get("pool_id")),
                "model_id": _clean_value(raw.get("model_returned")),
                "calibration_id": _clean_value(raw.get("calibration_id")),
                "evidence_status": (
                    probability_status if method == "jev" else "RULE_BASED_UNCALIBRATED"
                ),
                "factor_values": _clean_value(factor_values),
                "factor_group_scores": _clean_value(
                    raw.get("factor_group_scores")
                    if isinstance(raw.get("factor_group_scores"), dict)
                    else raw.get("group_scores")
                    if isinstance(raw.get("group_scores"), dict)
                    else {}
                ),
                "risk_metrics": _clean_value(risk_metrics),
                "reason_codes": reason_codes,
                "observed_trend": _clean_value(raw.get("observed_trend")),
                "forecast_class": str(raw.get("forecast_class") or "unknown").lower(),
                "formula_score": _clean_value(raw.get("formula_score")),
                "p_raw_up": _clean_value(raw.get("p_raw_up")),
                "p_raw_flat": _clean_value(raw.get("p_raw_flat")),
                "p_raw_down": _clean_value(raw.get("p_raw_down")),
                "p_cal_up": _clean_value(raw.get("p_cal_up")),
                "p_cal_flat": _clean_value(raw.get("p_cal_flat")),
                "p_cal_down": _clean_value(raw.get("p_cal_down")),
                "expected_gross_return": _clean_value(raw.get("expected_gross_return")),
                "estimated_round_trip_cost": _clean_value(raw.get("estimated_round_trip_cost")),
                "expected_net_edge": _clean_value(raw.get("expected_net_edge")),
                "return_estimate_basis": _clean_value(raw.get("return_estimate_basis")),
                "research_intent": _clean_value(raw.get("research_intent")),
                "account_action": _clean_value(raw.get("account_action")),
                "action_blockers": _string_tuple(raw.get("intent_reason_codes")),
                "current_quantity": None,
                "sellable_quantity": None,
                "target_quantity": None,
                "order_quantity": _clean_value(raw.get("order_quantity")),
                "reference_price": (
                    _clean_value(feature.get("close")) if entity_type == "stock" else None
                ),
                "entry_price_ceiling": None,
                "planned_stop_price": None,
                "planned_exit_date": reference_exit if horizon == 5 else None,
                "order_status": None,
                "name": _clean_value(feature.get("name")),
                "trade_eligible": (
                    bool(feature.get("trade_eligible")) if entity_type == "stock" else None
                ),
                "data_status": _first_value(feature.get("feature_status"), feature.get("status")),
                "factor_valid_count": valid_count,
                "probability_validation_status": probability_status,
                "account_type": "readonly_user",
            }
        )
        normalized.append(record)
    return pd.DataFrame(normalized)


def _feature_store_rows(
    run_id: str,
    as_of: str,
    stock_features: pd.DataFrame,
    sector_features: pd.DataFrame,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for feature in stock_features.to_dict(orient="records"):
        for factor_id in DIRECTIONAL_FACTOR_IDS + RISK_INDICATOR_IDS:
            value = _clean_value(feature.get(factor_id))
            rows.append(
                {
                    "run_id": run_id,
                    "entity_type": "stock",
                    "entity_id": str(feature["code"]),
                    "as_of_trade_date": as_of,
                    "factor_id": factor_id,
                    "value": value,
                    "status": "OK" if value is not None else "UNAVAILABLE",
                    "reason_code": _clean_value(feature.get(f"{factor_id}_reason")),
                    "metadata": {"price_basis": feature.get("price_basis")},
                }
            )
    for feature in sector_features.to_dict(orient="records"):
        for factor_id in tuple(f"z{index}" for index in range(1, 6)):
            value = _clean_value(feature.get(factor_id))
            rows.append(
                {
                    "run_id": run_id,
                    "entity_type": "sector",
                    "entity_id": str(feature["entity_id"]),
                    "as_of_trade_date": as_of,
                    "factor_id": factor_id,
                    "value": value,
                    "status": "OK" if value is not None else "UNAVAILABLE",
                    "reason_code": None if value is not None else "SECTOR_CONTEXT_UNAVAILABLE",
                    "metadata": {"namespace": feature.get("namespace")},
                }
            )
    return rows


def _analyze(args: argparse.Namespace) -> CommandOutcome:
    db_path, artifact_root = _mode_paths(args)
    store = V2Store(db_path, data_mode=args.mode)
    if not REQUIRED_TABLES.issubset(_database_tables(db_path)):
        return CommandOutcome(
            {
                **_common_payload("analyze", args.mode),
                "status": "PREREQUISITE_MISSING",
                "missing": ["MIGRATED_V2_DATABASE"],
            },
            2,
        )
    as_of, freshness = _latest_or_requested(args, store)
    if as_of is None:
        return CommandOutcome(
            {
                **_common_payload("analyze", args.mode),
                "status": "PREREQUISITE_MISSING",
                "missing": ["COMPLETE_DAILY_DATA"],
                **freshness,
            },
            2,
        )
    instruments = store.read_instrument_versions(as_of)
    if instruments.empty:
        return CommandOutcome(
            {
                **_common_payload("analyze", args.mode),
                "status": "PREREQUISITE_MISSING",
                "missing": ["DATED_INSTRUMENT_UNIVERSE"],
                **freshness,
            },
            2,
        )
    universe = build_analysis_universe(instruments, as_of).rename(columns={"ts_code": "code"})
    methods = tuple(value.strip() for value in str(args.methods).split(",") if value.strip())
    if not methods:
        raise ContractError("at least one analysis method is required")
    unknown = sorted(set(methods).difference({"formula", "jev"}))
    if unknown:
        raise ContractError(f"unsupported methods: {', '.join(unknown)}")
    features, sector_features = _compute_feature_snapshots(store, universe, as_of)
    if features.empty:
        return CommandOutcome(
            {
                **_common_payload("analyze", args.mode),
                "status": "PREREQUISITE_MISSING",
                "missing": ["ADJUSTED_FEATURE_INPUTS"],
                **freshness,
            },
            2,
        )
    stock_formula, stock_formula_scores = _build_formula_predictions(features, as_of=as_of)
    sector_formula, sector_formula_scores = _build_sector_formula_predictions(
        sector_features,
        as_of=as_of,
    )
    computed_formula = pd.concat([stock_formula, sector_formula], ignore_index=True, sort=False)
    if "formula" in methods:
        formula = computed_formula
    else:
        formula = _unavailable_jev_rows(
            features.iloc[0:0],
            as_of=as_of,
            status="NOT_REQUESTED",
            method="formula",
        )
    jev: pd.DataFrame
    jev_status = "NOT_REQUESTED"
    external_failure = False
    if "jev" in methods:
        if args.mode != "real":
            jev_status = "DEMO_ANALYZE_NETWORK_DISABLED"
            jev = pd.concat(
                [
                    _unavailable_jev_rows(features, as_of=as_of, status=jev_status),
                    _unavailable_jev_rows(
                        sector_features,
                        as_of=as_of,
                        status=jev_status,
                        entity_type="sector",
                        entity_id_column="entity_id",
                    ),
                ],
                ignore_index=True,
            )
        else:
            stock_jev, stock_jev_status, stock_failure = _build_jev_predictions(
                features,
                stock_formula_scores,
                as_of=as_of,
                db_path=db_path,
            )
            sector_jev, sector_jev_status, sector_failure = _build_jev_predictions(
                sector_features,
                sector_formula_scores,
                as_of=as_of,
                db_path=db_path,
                entity_type="sector",
                entity_id_column="entity_id",
            )
            jev = pd.concat([stock_jev, sector_jev], ignore_index=True, sort=False)
            jev_status = (
                "OK"
                if stock_jev_status == "OK" and sector_jev_status == "OK"
                else "PARTIAL"
            )
            external_failure = stock_failure or sector_failure
    else:
        jev = _unavailable_jev_rows(
            features.iloc[0:0],
            as_of=as_of,
            status="NOT_REQUESTED",
        )
    data_hash = _market_data_hash(store, as_of)
    config_hash = _config_hash()
    code_hash = _code_hash()
    run_id = "run-" + sha256_json(
        {
            "as_of": as_of,
            "mode": args.mode,
            "data_hash": data_hash,
            "config_hash": config_hash,
            "code_hash": code_hash,
        }
    )[:20]
    information_cutoff = f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:]}T20:10:00+08:00"
    combined = pd.concat([formula, jev], ignore_index=True, sort=False)
    normalized = _normalize_prediction_rows(
        combined,
        run_id=run_id,
        as_of=as_of,
        information_cutoff=information_cutoff,
        data_source_mode=args.mode,
        store=store,
        stock_features=features,
        sector_features=sector_features,
    )
    formula = normalized.loc[normalized["method"].eq("formula")].reset_index(drop=True)
    jev = normalized.loc[normalized["method"].eq("jev")].reset_index(drop=True)
    sector_universe_columns = [
        "entity_id",
        "namespace",
        "sector_id",
        "sector_name",
        "sector_history_mode",
    ]
    sector_universe = sector_features[
        [column for column in sector_universe_columns if column in sector_features.columns]
    ].drop_duplicates("entity_id") if not sector_features.empty else pd.DataFrame(columns=sector_universe_columns)
    with command_lock(artifact_root / ".command.lock"):
        store.write_analysis_run(
            {
                "run_id": run_id,
                "as_of_trade_date": as_of,
                "information_cutoff": information_cutoff,
                "generated_at": information_cutoff,
                "mode": "EOD_FINAL",
                "data_source_mode": args.mode,
                "code_hash": code_hash,
                "config_hash": config_hash,
                "data_hash": data_hash,
                "status": "BUILDING",
                "coverage": 0.0,
                "details": {"methods_requested": methods},
            }
        )
        store.upsert_feature_rows(
            _feature_store_rows(run_id, as_of, features, sector_features)
        )
        store.write_prediction_rows(
            [
                {
                    "run_id": run_id,
                    "entity_type": row["entity_type"],
                    "entity_id": row["entity_id"],
                    "horizon": row["horizon"],
                    "method": row["method"],
                    "prediction_status": row["prediction_status"],
                    "payload": row,
                    "first_recorded_at": information_cutoff,
                }
                for row in _frame_records(normalized)
            ]
        )
        registry_rows = []
        for entity_type, formula_id in (
            ("stock", "F0_BALANCED"),
            ("sector", "SECTOR_FIXED_V1"),
        ):
            for horizon in (1, 3, 5):
                registry_rows.extend(
                    [
                        {
                            "method": "formula",
                            "config_id": formula_id,
                            "entity_type": entity_type,
                            "horizon": horizon,
                            "status": "ACTIVE" if "formula" in methods else "NOT_REQUESTED",
                            "payload": {"protocol": "fixed-v1"},
                        },
                        {
                            "method": "jev",
                            "config_id": "J0_EQUAL_POOL",
                            "entity_type": entity_type,
                            "horizon": horizon,
                            "status": jev_status,
                            "payload": {"model_requested": "jev-1.13.0"},
                        },
                    ]
                )
        store.upsert_model_registry(registry_rows)
        write_immutable_json(
            artifact_root / run_id / "features" / "latest.json",
            {
                "schema_version": "technical-v2-features.v1",
                "run_id": run_id,
                "as_of_trade_date": as_of,
                "stock_rows": _frame_records(features),
                "sector_rows": _frame_records(sector_features),
            },
        )
        result = TechnicalV2Pipeline(artifact_root).run(
            run_id=run_id,
            as_of_trade_date=as_of,
            information_cutoff=information_cutoff,
            mode="EOD_FINAL",
            data_source_mode=args.mode,
            data_hash=data_hash,
            config_hash=config_hash,
            code_hash=code_hash,
            universe=universe,
            sector_universe=sector_universe,
            formula_predictions=formula,
            jev_predictions=jev,
        )
        coverage_ratio = (
            int(normalized["prediction_status"].eq("OK").sum()) / len(normalized)
            if len(normalized)
            else 0.0
        )
        store.write_analysis_run(
            {
                "run_id": run_id,
                "as_of_trade_date": as_of,
                "information_cutoff": information_cutoff,
                "generated_at": information_cutoff,
                "mode": "EOD_FINAL",
                "data_source_mode": args.mode,
                "code_hash": code_hash,
                "config_hash": config_hash,
                "data_hash": data_hash,
                "status": "OK" if result.route_statuses["formula"] == "OK" else "PARTIAL",
                "coverage": coverage_ratio,
                "details": {
                    "publication_id": result.publication_id,
                    "stock_entities": len(features),
                    "sector_entities": len(sector_features),
                },
            }
        )
    payload = {
        **_common_payload("analyze", args.mode),
        "status": (
            "OK"
            if result.route_statuses["formula"] == "OK" and jev_status in {"NOT_REQUESTED", "OK"}
            else "PARTIAL"
        ),
        "run_id": run_id,
        "publication_id": result.publication_id,
        "formula_status": result.route_statuses["formula"],
        "formula_config_id": FORMULA_CONFIG_IDS[0],
        "formula_valid_rows": int(formula["prediction_status"].eq("OK").sum()) if not formula.empty else 0,
        "jev_status": jev_status,
        "feature_rows": len(features),
        "sector_feature_rows": len(sector_features),
        "coverage_rows": result.coverage_rows,
        **freshness,
    }
    return CommandOutcome(payload, 2 if external_failure else 0)


def _paper(args: argparse.Namespace) -> CommandOutcome:
    db_path, artifact_root = _mode_paths(args)
    if not REQUIRED_TABLES.issubset(_database_tables(db_path)):
        return CommandOutcome(
            {
                **_common_payload("paper", args.mode),
                "status": "PREREQUISITE_MISSING",
                "missing": ["MIGRATED_V2_DATABASE"],
                "orders_created": 0,
            },
            2,
        )
    store = V2Store(db_path, data_mode=args.mode)
    freshness = resolve_latest_as_of(store)
    if args.mode in {"demo", "test"} and freshness["actual_as_of"]:
        freshness = {
            **freshness,
            "expected_as_of": freshness["actual_as_of"],
            "freshness_status": "CURRENT",
        }
    latest = read_latest_manifest(artifact_root)
    if latest is None:
        return CommandOutcome(
            {
                **_common_payload("paper", args.mode),
                "status": "PREREQUISITE_MISSING",
                "missing": ["PUBLISHED_ANALYSIS"],
                "orders_created": 0,
                **freshness,
            },
            2,
        )
    if freshness["freshness_status"] != "CURRENT" or latest["as_of_trade_date"] != freshness["actual_as_of"]:
        return CommandOutcome(
            {
                **_common_payload("paper", args.mode),
                "status": "STALE",
                "orders_created": 0,
                "run_id": latest["run_id"],
                **freshness,
            },
            2,
        )
    methods = tuple(
        item.strip().lower()
        for item in str(args.methods).split(",")
        if item.strip()
    )
    with command_lock(artifact_root / ".command.lock"):
        result = run_paper_cycle(
            store,
            run_id=str(latest["run_id"]),
            as_of_trade_date=str(latest["as_of_trade_date"]),
            methods=methods,
        )
    status = "OK" if result.orders_created or result.orders_executed else "NO_EXECUTABLE_ORDERS"
    return CommandOutcome(
        {
            **_common_payload("paper", args.mode),
            "status": status,
            "orders_created": result.orders_created,
            "orders_executed": result.orders_executed,
            "decision_rows": result.decision_rows,
            "next_trade_date": result.next_trade_date,
            "account_statuses": result.account_statuses,
            "run_id": latest["run_id"],
            **freshness,
        },
        0,
    )


def _fit_evaluate(args: argparse.Namespace) -> CommandOutcome:
    db_path, artifact_root = _mode_paths(args)
    if not REQUIRED_TABLES.issubset(_database_tables(db_path)):
        return CommandOutcome(
            {
                **_common_payload("fit-evaluate", args.mode),
                "status": "PREREQUISITE_MISSING",
                "protocol": args.protocol,
                "missing": ["MIGRATED_V2_DATABASE"],
                "final_test_opened": False,
            },
            2,
        )
    store = V2Store(db_path, data_mode=args.mode)
    with command_lock(artifact_root / ".command.lock"):
        result = run_fixed_evaluation(store, artifact_root, protocol=args.protocol)
    payload = {
        **_common_payload("fit-evaluate", args.mode),
        "status": result.status,
        "protocol": args.protocol,
        "run_id": result.run_id,
        "run_dir": str(result.run_dir),
        "selection_status": result.selection_status,
        "split_status": result.split_status,
        "final_test_opened": result.final_test_opened,
        "label_rows": result.label_rows,
        "mature_signal_dates": result.mature_signal_dates,
        "artifact_hashes": result.artifact_hashes,
        "missing": (
            ["504_MATURE_SIGNAL_DATES"]
            if result.split_status == "INSUFFICIENT_HISTORY"
            else ["120_SESSION_WARMUP"]
            if result.split_status == "INSUFFICIENT_WARMUP"
            else []
        ),
    }
    return CommandOutcome(payload, 2 if result.split_status != "OK" else 0)


def _evaluate_matured(args: argparse.Namespace) -> CommandOutcome:
    db_path, artifact_root = _mode_paths(args)
    latest = read_latest_manifest(artifact_root)
    missing = []
    if not REQUIRED_TABLES.issubset(_database_tables(db_path)):
        missing.append("MIGRATED_V2_DATABASE")
    if latest is None:
        missing.append("PUBLISHED_ANALYSIS")
    if missing:
        return CommandOutcome(
            {
                **_common_payload("evaluate-matured", args.mode),
                "status": "PREREQUISITE_MISSING",
                "missing": missing,
                "evaluated_rows": 0,
            },
            2,
        )
    store = V2Store(db_path, data_mode=args.mode)
    with command_lock(artifact_root / ".command.lock"):
        label_rows = materialize_labels(store)
        result = evaluate_matured_predictions(store)
    return CommandOutcome(
        {
            **_common_payload("evaluate-matured", args.mode),
            "status": "OK" if result.evaluated_rows else "NO_NEW_MATURE_LABELS",
            "missing": [],
            "label_rows": label_rows,
            "evaluated_rows": result.evaluated_rows,
            "total_evaluated_rows": result.total_evaluated_rows,
            "mature_signal_dates": result.mature_signal_dates,
            "forward_statuses": result.forward_statuses,
        },
        0,
    )


def _export(args: argparse.Namespace) -> CommandOutcome:
    _, artifact_root = _mode_paths(args)
    run_dir = artifact_root / str(args.run_id)
    if not run_dir.is_dir():
        return CommandOutcome(
            {
                **_common_payload("export", args.mode),
                "status": "PREREQUISITE_MISSING",
                "missing": ["RUN_ARTIFACTS"],
                "run_id": args.run_id,
            },
            2,
        )
    output = Path(args.output) if args.output else artifact_root / "exports" / f"{args.run_id}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    with (
        command_lock(artifact_root / ".command.lock"),
        zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive,
    ):
        for path in sorted(run_dir.rglob("*")):
            if path.is_file():
                archive.write(path, arcname=str(path.relative_to(artifact_root)))
    return CommandOutcome(
        {
            **_common_payload("export", args.mode),
            "status": "OK",
            "run_id": args.run_id,
            "output": str(output),
            "sha256": _file_hash(output),
        },
        0,
    )


def verify_artifact_manifest(run_dir: Path, run_id: str) -> list[str]:
    manifest_path = run_dir / "artifact_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"artifact_manifest.json: {type(exc).__name__}"]
    if not isinstance(manifest, dict):
        return ["artifact_manifest.json: root must be an object"]
    errors: list[str] = []
    if manifest.get("schema_version") != "technical-v2-artifact-manifest.v1":
        errors.append("artifact_manifest.json: unsupported schema_version")
    if manifest.get("run_id") != run_id:
        errors.append("artifact_manifest.json: run_id mismatch")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        errors.append("artifact_manifest.json: files must be a non-empty list")
        return errors
    seen: set[str] = set()
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            errors.append(f"artifact_manifest.json: files[{index}] is not an object")
            continue
        relative = str(item.get("path") or "")
        relative_path = Path(relative)
        if (
            not relative
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative in seen
            or relative == "artifact_manifest.json"
        ):
            errors.append(f"artifact_manifest.json: invalid path {relative!r}")
            continue
        seen.add(relative)
        target = run_dir / relative_path
        if not target.is_file():
            errors.append(f"{relative}: missing")
            continue
        expected_size = item.get("size_bytes")
        if not isinstance(expected_size, int) or expected_size != target.stat().st_size:
            errors.append(f"{relative}: size mismatch")
        expected_hash = str(item.get("sha256") or "")
        if len(expected_hash) != 64 or expected_hash != _file_hash(target):
            errors.append(f"{relative}: sha256 mismatch")
    return errors


def run_worker_task(
    task_name: str,
    params: dict[str, Any],
    force_refresh: bool,
) -> dict[str, Any]:
    mode = str(params.get("mode") or "real")
    common = {
        "mode": mode,
        "db_path": str(params.get("db_path") or ""),
        "artifact_root": str(params.get("artifact_root") or ""),
    }

    def finish(
        outcome: CommandOutcome,
        *,
        completed: int,
        total: int,
        runtime_status: str | None = None,
    ) -> dict[str, Any]:
        payload = dict(outcome.payload)
        status = str(payload.pop("status", "ERROR"))
        if outcome.exit_code != 0 and status not in {
            "NO_NEW_MATURE_LABELS",
            "NO_EXECUTABLE_ORDERS",
        }:
            task_status = status
        else:
            task_status = "OK"
        return {
            "status": task_status,
            "runtime_status": runtime_status or status,
            "code": str(payload.pop("code", "")),
            "message": str(payload.pop("message", "")),
            "completed_entities": int(completed),
            "total_entities": int(total),
            **payload,
        }

    if task_name == "data_v2_sync":
        outcome = _sync(
            argparse.Namespace(
                command="sync",
                **common,
                history_sessions=int(params.get("history_sessions", 800) or 800),
                as_of=str(params.get("as_of") or ""),
                seed=int(params.get("seed", 20260923) or 20260923),
                include_corporate_actions=bool(params.get("include_corporate_actions", False)),
                force_refresh=bool(force_refresh),
            )
        )
        if outcome.exit_code != 0:
            return finish(outcome, completed=0, total=1)
        db_path, _ = _mode_paths(argparse.Namespace(**common))
        store = V2Store(db_path, data_mode=mode)
        as_of = store.latest_complete_session("SSE", "99991231")
        if as_of is None:
            raise ContractError("data_v2_sync completed without an audited session")
        data_hash = _market_data_hash(store, as_of)
        run_id = "run-" + sha256_json(
            {
                "as_of": as_of,
                "mode": mode,
                "data_hash": data_hash,
                "config_hash": _config_hash(),
                "code_hash": _code_hash(),
            }
        )[:20]
        total = int(outcome.payload.get("sessions", 1) or 1)
        result = finish(outcome, completed=total, total=total)
        result.update(
            {
                "run_id": run_id,
                "as_of_trade_date": as_of,
                "data_hash": data_hash,
            }
        )
        return result

    binding = {
        key: str(params.get(key) or "")
        for key in ("run_id", "as_of_trade_date", "data_hash")
    }
    if task_name in {"features_v2", "formula_v2", "jev_v2"}:
        methods = "formula,jev" if task_name == "jev_v2" else "formula"
        outcome = _analyze(
            argparse.Namespace(
                command="analyze",
                **common,
                as_of=binding["as_of_trade_date"],
                methods=methods,
            )
        )
        if outcome.payload.get("run_id") and outcome.payload["run_id"] != binding["run_id"]:
            raise ContractError("analysis result does not match worker run binding")
        if task_name == "features_v2":
            total = int(outcome.payload.get("feature_rows", 0)) + int(
                outcome.payload.get("sector_feature_rows", 0)
            )
        else:
            total = int(outcome.payload.get("coverage_rows", 0))
        result = finish(outcome, completed=total, total=total or 1)
    elif task_name == "paper_v2":
        outcome = _paper(
            argparse.Namespace(command="paper", **common, methods="formula,jev")
        )
        total = int(outcome.payload.get("decision_rows", 0))
        result = finish(outcome, completed=total, total=total or 1)
    elif task_name == "evaluate_matured_v2":
        outcome = _evaluate_matured(
            argparse.Namespace(command="evaluate-matured", **common)
        )
        total = int(outcome.payload.get("total_evaluated_rows", 0))
        result = finish(
            outcome,
            completed=total if total else int(outcome.exit_code == 0),
            total=total or 1,
        )
    elif task_name == "publish_technical_v2":
        _, artifact_root = _mode_paths(argparse.Namespace(**common))
        latest = read_latest_manifest(artifact_root)
        if latest is None:
            raise ContractError("publish_technical_v2 requires a publication manifest")
        for key, value in binding.items():
            if str(latest.get(key) or "") != value:
                raise ContractError(f"publication binding mismatch: {key}")
        result = {
            "status": "OK",
            "runtime_status": "OK",
            "code": "",
            "message": "",
            "completed_entities": int(latest.get("coverage_rows", 0) or 0),
            "total_entities": int(latest.get("coverage_rows", 0) or 0),
            "publication_id": latest["publication_id"],
            "manifest_hash": latest["manifest_hash"],
        }
    else:
        raise ContractError(f"unknown Technical V2 worker task: {task_name}")
    result.update(binding)
    return result


def _audit_release(args: argparse.Namespace) -> CommandOutcome:
    _, artifact_root = _mode_paths(args)
    run_dir = artifact_root / str(args.run_id)
    required = (
        "run_manifest.json",
        "selection.json",
        "summary_metrics.csv",
        "coverage_summary.csv",
        "TEST_REPORT.md",
        "artifact_manifest.json",
    )
    missing = [name for name in required if not (run_dir / name).is_file()]
    latest = read_latest_manifest(artifact_root)
    if latest is None or latest.get("run_id") != args.run_id:
        missing.append("validated_latest_publication")
    errors = [] if missing else verify_artifact_manifest(run_dir, str(args.run_id))
    return CommandOutcome(
        {
            **_common_payload("audit-release", args.mode),
            "status": "PREREQUISITE_MISSING" if missing else "ERROR" if errors else "OK",
            "run_id": args.run_id,
            "missing": sorted(set(missing)),
            "errors": errors,
        },
        2 if missing else 3 if errors else 0,
    )


def _add_common(parser: argparse.ArgumentParser, *, default_mode: str = "real") -> None:
    parser.add_argument("--mode", choices=("real", "demo", "test"), default=default_mode)
    parser.add_argument("--db-path", default="")
    parser.add_argument("--artifact-root", default="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Technical V2 command line")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor")
    _add_common(doctor)

    sync = subparsers.add_parser("sync")
    _add_common(sync)
    sync.add_argument("--history-sessions", type=int, default=800)
    sync.add_argument("--as-of", default="")
    sync.add_argument("--seed", type=int, default=20260923)
    sync.add_argument("--include-corporate-actions", action="store_true")
    sync.add_argument("--force-refresh", action="store_true")

    analyze = subparsers.add_parser("analyze")
    _add_common(analyze)
    analyze.add_argument("--as-of", default="latest")
    analyze.add_argument("--methods", default="formula,jev")

    fit = subparsers.add_parser("fit-evaluate")
    _add_common(fit)
    fit.add_argument("--protocol", choices=("fixed-v1",), default="fixed-v1")

    paper = subparsers.add_parser("paper")
    _add_common(paper)
    paper.add_argument("--methods", default="formula,jev")

    matured = subparsers.add_parser("evaluate-matured")
    _add_common(matured)

    export = subparsers.add_parser("export")
    _add_common(export)
    export.add_argument("--run-id", required=True)
    export.add_argument("--output", default="")

    demo = subparsers.add_parser("demo")
    _add_common(demo, default_mode="demo")
    demo.add_argument("--seed", type=int, default=20260923)
    demo.add_argument("--as-of", default=DEFAULT_DEMO_AS_OF)

    audit = subparsers.add_parser("audit-release")
    _add_common(audit)
    audit.add_argument("--run-id", required=True)
    return parser


def dispatch(args: argparse.Namespace) -> CommandOutcome:
    handlers = {
        "doctor": _doctor,
        "sync": _sync,
        "analyze": _analyze,
        "fit-evaluate": _fit_evaluate,
        "paper": _paper,
        "evaluate-matured": _evaluate_matured,
        "export": _export,
        "demo": _demo,
        "audit-release": _audit_release,
    }
    return handlers[args.command](args)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        outcome = dispatch(args)
        payload = json_safe(outcome.payload)
        exit_code = outcome.exit_code
    except LockUnavailable as exc:
        payload = {
            "schema_version": "technical-v2-cli.v1",
            "status": "PREREQUISITE_MISSING",
            "code": "COMMAND_LOCKED",
            "message": str(exc),
        }
        exit_code = 2
    except (ArtifactMismatch, ContractError, OSError, sqlite3.Error, ValueError) as exc:
        payload = {
            "schema_version": "technical-v2-cli.v1",
            "status": "ERROR",
            "code": type(exc).__name__,
            "message": str(exc),
        }
        exit_code = 3
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
