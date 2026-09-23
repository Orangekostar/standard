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
from core.pipeline.technical_v2 import TechnicalV2Pipeline
from core.strategies.formula_v2 import FORMULA_CONFIG_IDS, score_stock
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
        with sqlite3.connect(path) as conn:
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
    actual = None
    if store.path.exists():
        try:
            with closing(store._connect()) as conn:
                row = conn.execute(
                    """
                    SELECT MAX(date) FROM daily_raw
                    WHERE completeness = 'COMPLETE' AND data_source_mode = ?
                    """,
                    (store.data_mode,),
                ).fetchone()
            actual = str(row[0]) if row and row[0] else None
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
            {"code": "300001.SZ", "name": "DEMO_CHINEXT", "listing_board": "CHINEXT"},
            {"code": "600000.SH", "name": "DEMO_MAIN_SH", "listing_board": "MAIN_SH"},
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
    calendar_rows = []
    for exchange in ("SSE", "SZSE"):
        for index, session in enumerate(sessions):
            calendar_rows.append(
                {
                    "exchange": exchange,
                    "date": session,
                    "is_open": 1,
                    "previous_session": sessions[index - 1] if index else None,
                    "next_session": sessions[index + 1] if index + 1 < len(sessions) else None,
                    "source": "SYNTHETIC_FIXTURE",
                    "source_version": f"demo-calendar-{args.seed}",
                    "retrieved_at": f"{as_of}T16:10:00+08:00",
                }
            )
    store.upsert_calendar(pd.DataFrame(calendar_rows))
    provider = DemoV2Provider(seed=int(args.seed))
    daily_rows = []
    for session in sessions:
        daily_rows.append(provider.daily(session).frame)
    daily = pd.concat(daily_rows, ignore_index=True)
    store.upsert_daily_raw(daily)
    codes = sorted(daily["code"].unique())
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
                "valid_from": as_of,
                "valid_to": None,
                "observed_at": f"{as_of}T16:10:00+08:00",
                "history_mode": "CURRENT_SNAPSHOT_ONLY",
                "source": "SYNTHETIC_FIXTURE",
                "source_version": f"demo-membership-{args.seed}",
            }
            for code in codes
        ]
    )
    store.upsert_sector_membership(memberships)
    adjustments = daily[["code", "date"]].copy()
    adjustments["source_version"] = f"demo-adjustments-{args.seed}"
    adjustments["adj_factor"] = 1.0
    adjustments["source"] = "SYNTHETIC_FIXTURE"
    adjustments["retrieved_at"] = f"{as_of}T16:10:00+08:00"
    store.upsert_adjustments(adjustments)
    return {
        "as_of_trade_date": as_of,
        "sessions": len(sessions),
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
            calendar_results = [provider.calendar(exchange, start, today) for exchange in ("SSE", "SZSE")]
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
            daily_written = 0
            adjustment_rows = 0
            trading_status_rows = 0
            failures: list[dict[str, Any]] = []
            for session in sessions:
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
            instruments = provider.instruments(today)
            instrument_rows = (
                store.upsert_instrument_versions(instruments.frame)
                if instruments.status in {"OK", "PARTIAL"}
                else 0
            )
            memberships = (
                _current_industry_memberships(instruments.frame, today)
                if instruments.status in {"OK", "PARTIAL"}
                else pd.DataFrame()
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
                "daily_rows": daily_written,
                "adjustment_rows": adjustment_rows,
                "trading_status_rows": trading_status_rows,
                "instrument_rows": instrument_rows,
                "sector_membership_rows": membership_rows,
                "sector_history_mode": "CURRENT_SNAPSHOT_ONLY",
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


def _compute_feature_snapshot(
    store: V2Store,
    universe: pd.DataFrame,
    as_of: str,
) -> pd.DataFrame:
    sessions = store.open_sessions("SSE", as_of, 120)
    if not sessions:
        return pd.DataFrame()
    codes = universe["code"].astype(str).tolist()
    raw = store.read_daily_raw(sessions[0], as_of, codes)
    if not raw.empty:
        raw = raw.sort_values(["code", "date", "retrieved_at", "source_version"]).drop_duplicates(
            ["code", "date"], keep="last"
        )
    adjustments = store.read_adjustments(sessions[0], as_of, codes)
    if raw.empty or adjustments.empty:
        return pd.DataFrame()
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
    return snapshot.sort_values("code").reset_index(drop=True)


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


def _unavailable_jev_rows(
    features: pd.DataFrame,
    *,
    as_of: str,
    status: str,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "entity_type": "stock",
                "entity_id": str(code),
                "as_of_trade_date": as_of,
                "horizon": horizon,
                "method": "jev",
                "prediction_status": status,
                "forecast_class": "unknown",
                "probability_validation_status": status,
                "reason_codes": (status,),
            }
            for code in features["code"]
            for horizon in (1, 3, 5)
        ]
    )


def _build_jev_predictions(
    features: pd.DataFrame,
    formula_scores: dict[str, dict[int, Any]],
    *,
    as_of: str,
    db_path: Path,
) -> tuple[pd.DataFrame, str, bool]:
    api_key = os.getenv("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        return (
            _unavailable_jev_rows(features, as_of=as_of, status="UNAVAILABLE_CREDENTIALS"),
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
    questions = build_questions("stock")
    rows: list[dict[str, Any]] = []
    external_failure = False
    route_statuses: list[str] = []
    for feature in features.to_dict(orient="records"):
        code = str(feature["code"])
        sigma = pd.to_numeric(pd.Series([feature.get("Q02")]), errors="coerce").iloc[0]
        if pd.isna(sigma) or int(feature.get("factor_valid_count") or 0) < 12:
            rows.extend(
                _unavailable_jev_rows(
                    pd.DataFrame({"code": [code]}),
                    as_of=as_of,
                    status="INSUFFICIENT_FEATURES",
                ).to_dict(orient="records")
            )
            route_statuses.append("INSUFFICIENT_FEATURES")
            continue
        targets = {
            horizon: max(0.005, 0.25 * float(sigma) * np.sqrt(horizon))
            for horizon in (1, 3, 5)
        }
        state = build_jev_state(
            "stock",
            code,
            feature,
            targets=targets,
            group_scores=formula_scores[code][5].group_scores,
        )
        result = client.predict(state, questions, budget)
        if result.status != "OK" or result.response is None:
            rows.extend(
                _unavailable_jev_rows(
                    pd.DataFrame({"code": [code]}),
                    as_of=as_of,
                    status=result.status,
                ).to_dict(orient="records")
            )
            route_statuses.append(result.status)
            external_failure = True
            continue
        validated = validate_answers(result.response, tuple(questions))
        if validated.status not in {"OK", "PARTIAL_RESPONSE"}:
            rows.extend(
                _unavailable_jev_rows(
                    pd.DataFrame({"code": [code]}),
                    as_of=as_of,
                    status=validated.status,
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
                    "entity_type": "stock",
                    "entity_id": code,
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
                decision = derive_research_intent(
                    entity_rows,
                    None,
                    horizon=int(predictions.at[index, "horizon"]),
                )
                predictions.at[index, "research_intent"] = decision.research_intent
                predictions.at[index, "account_action"] = decision.account_action
                predictions.at[index, "order_quantity"] = decision.order_quantity
                predictions.at[index, "intent_reason_codes"] = decision.reason_codes
    status = "OK" if route_statuses and set(route_statuses) == {"OK"} else "PARTIAL"
    return predictions, status, external_failure


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
    unknown = sorted(set(methods).difference({"formula", "jev"}))
    if unknown:
        raise ContractError(f"unsupported methods: {', '.join(unknown)}")
    features = _compute_feature_snapshot(store, universe, as_of)
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
    all_formula, formula_scores = _build_formula_predictions(features, as_of=as_of)
    formula = all_formula if "formula" in methods else pd.DataFrame()
    jev: pd.DataFrame | None = None
    jev_status = "NOT_REQUESTED"
    external_failure = False
    if "jev" in methods:
        if args.mode != "real":
            jev_status = "DEMO_ANALYZE_NETWORK_DISABLED"
            jev = _unavailable_jev_rows(features, as_of=as_of, status=jev_status)
        else:
            jev, jev_status, external_failure = _build_jev_predictions(
                features,
                formula_scores,
                as_of=as_of,
                db_path=db_path,
            )
    data_hash = _file_hash(db_path)
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
    with command_lock(artifact_root / ".command.lock"):
        write_immutable_json(
            artifact_root / run_id / "features" / "latest.json",
            {
                "schema_version": "technical-v2-features.v1",
                "run_id": run_id,
                "as_of_trade_date": as_of,
                "rows": _frame_records(features),
            },
        )
        result = TechnicalV2Pipeline(artifact_root).run(
            run_id=run_id,
            as_of_trade_date=as_of,
            information_cutoff=(
                f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:]}T20:10:00+08:00"
            ),
            mode="EOD_FINAL",
            data_source_mode=args.mode,
            data_hash=data_hash,
            config_hash=config_hash,
            code_hash=code_hash,
            universe=universe,
            formula_predictions=formula,
            jev_predictions=jev,
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
    return CommandOutcome(
        {
            **_common_payload("paper", args.mode),
            "status": "NO_EXECUTABLE_ORDERS",
            "orders_created": 0,
            "run_id": latest["run_id"],
            **freshness,
        },
        0,
    )


def _fit_evaluate(args: argparse.Namespace) -> CommandOutcome:
    db_path, _ = _mode_paths(args)
    if not REQUIRED_TABLES.issubset(_database_tables(db_path)):
        missing = ["MIGRATED_V2_DATABASE"]
    else:
        store = V2Store(db_path, data_mode=args.mode)
        sessions = store.open_sessions("SSE", "99991231", 504)
        missing = [] if len(sessions) >= 504 else ["504_MATURE_SIGNAL_DATES"]
    return CommandOutcome(
        {
            **_common_payload("fit-evaluate", args.mode),
            "status": "PREREQUISITE_MISSING" if missing else "READY_FOR_FIXED_PROTOCOL",
            "protocol": args.protocol,
            "missing": missing,
            "final_test_opened": False,
        },
        2 if missing else 0,
    )


def _evaluate_matured(args: argparse.Namespace) -> CommandOutcome:
    db_path, artifact_root = _mode_paths(args)
    latest = read_latest_manifest(artifact_root)
    missing = []
    if not REQUIRED_TABLES.issubset(_database_tables(db_path)):
        missing.append("MIGRATED_V2_DATABASE")
    if latest is None:
        missing.append("PUBLISHED_ANALYSIS")
    return CommandOutcome(
        {
            **_common_payload("evaluate-matured", args.mode),
            "status": "PREREQUISITE_MISSING" if missing else "NO_MATURE_LABELS",
            "missing": missing,
            "evaluated_rows": 0,
        },
        2 if missing else 0,
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
    return CommandOutcome(
        {
            **_common_payload("audit-release", args.mode),
            "status": "PREREQUISITE_MISSING" if missing else "OK",
            "run_id": args.run_id,
            "missing": sorted(set(missing)),
        },
        2 if missing else 0,
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
