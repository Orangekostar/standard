from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from core.analysis.sector_v2 import build_context
from core.background.snapshot_store import ArtifactMismatch, write_immutable_json
from core.backtest.execution_v2 import (
    FeeSchedule,
    MarketOpen,
    PaperOrder,
    SecurityRule,
    allocate_orders,
    execute_open_orders,
)
from core.backtest.metrics_v2 import calculate_portfolio_metrics
from core.backtest.portfolio_v2 import PaperPortfolio, mark_portfolio
from core.data.v2_store import V2Store
from core.data.v2_universe import build_analysis_universe
from core.factors.technical_v2 import (
    DIRECTIONAL_FACTOR_IDS,
    RISK_INDICATOR_IDS,
    apply_adjustment_factors,
    compute_technical_v2,
)
from core.models.calibration_v2 import (
    FormulaSelection,
    JevSelection,
    select_formula_candidate,
    write_selection_artifact,
)
from core.pipeline.technical_v2 import (
    TARGET_DEFINITION_VERSION,
    build_fixed_split,
    build_labels,
    build_sector_labels,
    purge_cross_boundary,
    select_cohort,
)
from core.strategies.formula_v2 import (
    FORMULA_CONFIG_IDS,
    ReturnBinModel,
    estimate_formula_return,
    fit_return_bins,
    score_stock,
)
from core.strategies.intent_v2 import derive_research_intent
from core.technical_v2.contracts import ContractError, json_safe, sha256_json

REQUIRED_RESULT_FILES = (
    "dataset_manifest.json",
    "data_audit.json",
    "coverage_by_date.csv",
    "coverage_by_sector.csv",
    "factor_ic.csv",
    "factor_missingness.csv",
    "candidate_comparison.csv",
    "probability_metrics.csv",
    "calibration_bins.csv",
    "daily_nav.csv",
    "trades.csv",
    "orders.csv",
    "selection.json",
    "run_manifest.json",
)


@dataclass(frozen=True)
class EvaluationRuntimeResult:
    run_id: str
    run_dir: Path
    status: str
    selection_status: str
    split_status: str
    final_test_opened: bool
    label_rows: int
    mature_signal_dates: int
    artifact_hashes: dict[str, str]


def _clean(value: Any) -> Any:
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return json_safe(value)


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {str(key): _clean(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _write_immutable_csv(path: Path, frame: pd.DataFrame) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = frame.to_csv(index=False, lineterminator="\n", na_rep="")
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    if path.exists():
        if path.read_text(encoding="utf-8") != rendered:
            raise ArtifactMismatch(f"immutable artifact conflict: {path}")
        return digest
    with tempfile.NamedTemporaryFile(
        "w",
        dir=str(path.parent),
        delete=False,
        encoding="utf-8",
    ) as handle:
        handle.write(rendered)
        temporary = Path(handle.name)
    try:
        os.link(temporary, path)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != rendered:
            raise ArtifactMismatch(f"immutable artifact conflict: {path}")
    finally:
        temporary.unlink(missing_ok=True)
    return digest


def _completed_result(
    run_dir: Path,
    *,
    run_id: str,
    dataset_hash: str,
    protocol: str,
) -> EvaluationRuntimeResult | None:
    manifest_path = run_dir / "run_manifest.json"
    if not manifest_path.is_file():
        return None
    if not run_dir.is_dir() or {path.name for path in run_dir.iterdir()} != set(REQUIRED_RESULT_FILES):
        raise ContractError("completed evaluation result package is incomplete")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        audit = json.loads((run_dir / "data_audit.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("completed evaluation metadata is unreadable") from exc
    if (
        manifest.get("run_id") != run_id
        or manifest.get("dataset_hash") != dataset_hash
        or manifest.get("protocol") != protocol
    ):
        raise ArtifactMismatch("completed evaluation binding changed")
    recorded_hashes = manifest.get("artifact_hashes")
    if not isinstance(recorded_hashes, dict):
        raise ContractError("completed evaluation manifest has no artifact hashes")
    hashes: dict[str, str] = {}
    for name in REQUIRED_RESULT_FILES:
        path = run_dir / name
        byte_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.suffix == ".json":
            try:
                canonical_digest = sha256_json(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as exc:
                raise ContractError(f"completed evaluation JSON is unreadable: {name}") from exc
        else:
            canonical_digest = byte_digest
        if name == "run_manifest.json":
            hashes[name] = canonical_digest
            continue
        recorded = str(recorded_hashes.get(name) or "")
        if recorded not in {byte_digest, canonical_digest}:
            raise ArtifactMismatch(f"completed evaluation artifact hash mismatch: {name}")
        hashes[name] = recorded
    return EvaluationRuntimeResult(
        run_id=run_id,
        run_dir=run_dir,
        status=str(manifest["status"]),
        selection_status=str(manifest["selection_status"]),
        split_status=str(manifest["split_status"]),
        final_test_opened=bool(manifest["final_test_opened"]),
        label_rows=int(audit.get("label_rows", 0) or 0),
        mature_signal_dates=int(audit.get("mature_signal_dates", 0) or 0),
        artifact_hashes=hashes,
    )


def _load_dataset(store: V2Store) -> dict[str, Any]:
    latest = store.latest_complete_session("SSE", "99991231")
    if latest is None:
        raise ContractError("fixed evaluation requires a completely audited session")
    sessions = store.open_sessions("SSE", latest, 10_000)
    if not sessions:
        raise ContractError("fixed evaluation requires an exchange calendar")
    instruments = store.read_instrument_versions(latest)
    universe = build_analysis_universe(instruments, latest).rename(columns={"ts_code": "code"})
    if universe.empty:
        raise ContractError("fixed evaluation requires a dated analysis universe")
    codes = universe["code"].astype(str).tolist()
    raw = store.read_daily_raw(sessions[0], latest, codes)
    adjustments = store.read_adjustments(sessions[0], latest, codes)
    if raw.empty or adjustments.empty:
        raise ContractError("fixed evaluation requires raw prices and adjustment factors")
    raw = raw.sort_values(["code", "date", "retrieved_at", "source_version"]).drop_duplicates(
        ["code", "date"],
        keep="last",
    )
    panel = apply_adjustment_factors(raw, adjustments, as_of=latest)
    panel["mark_only"] = False
    memberships = store.read_sector_membership(latest)
    context = build_context(
        panel,
        memberships,
        as_of=latest,
        universe=codes,
        sessions=sessions,
    )
    features = compute_technical_v2(panel, context, as_of=latest)
    metadata_columns = [
        column
        for column in ("code", "name", "exchange", "listing_board", "trade_eligible")
        if column in universe.columns
    ]
    features = features.merge(
        universe[metadata_columns],
        on="code",
        how="left",
        validate="many_to_one",
    )
    stock_labels = build_labels(
        features[["code", "date", "adjusted_open", "Q02"]],
        sessions,
    )
    sector_labels = (
        build_sector_labels(stock_labels, context.assignments, context.sectors)
        if context.sectors is not None and not context.sectors.empty
        else pd.DataFrame()
    )
    labels = pd.concat([stock_labels, sector_labels], ignore_index=True, sort=False)
    return {
        "data_source_mode": store.data_mode,
        "latest": latest,
        "sessions": sessions,
        "instruments": instruments,
        "universe": universe,
        "raw": raw,
        "panel": panel,
        "memberships": memberships,
        "context": context,
        "features": features,
        "stock_labels": stock_labels,
        "sector_labels": sector_labels,
        "labels": labels,
        "trading_status": store.read_trading_status(sessions[0], latest, codes),
    }


def _persist_labels(store: V2Store, labels: pd.DataFrame) -> int:
    rows = []
    for row in _records(labels):
        rows.append(
            {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "as_of_trade_date": row["as_of_trade_date"],
                "horizon": row["horizon"],
                "target_definition_version": row["target_definition_version"],
                "label_end_date": row.get("label_end_date"),
                "label_status": row["label_status"],
                "target_class": row.get("target_class"),
                "realized_return": row.get("realized_return"),
                "payload": row,
            }
        )
    return store.upsert_label_rows(rows)


def materialize_labels(store: V2Store) -> int:
    dataset = _load_dataset(store)
    return _persist_labels(store, dataset["labels"])


def _coverage_by_date(features: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    feature_rows = (
        features.groupby("date", sort=True)
        .agg(
            expected_entities=("code", "nunique"),
            feature_rows=("code", "size"),
            valid_feature_rows=("feature_status", lambda values: int(values.astype(str).eq("OK").sum())),
        )
        .reset_index()
        .rename(columns={"date": "signal_date"})
    )
    mature = (
        labels.groupby("as_of_trade_date", sort=True)
        .agg(
            label_rows=("entity_id", "size"),
            observed_labels=("label_status", lambda values: int(values.astype(str).eq("OK").sum())),
        )
        .reset_index()
        .rename(columns={"as_of_trade_date": "signal_date"})
    )
    out = feature_rows.merge(mature, on="signal_date", how="outer").fillna(0)
    out["feature_coverage"] = np.where(
        out["feature_rows"].gt(0),
        out["valid_feature_rows"] / out["feature_rows"],
        0.0,
    )
    out["label_coverage"] = np.where(
        out["label_rows"].gt(0),
        out["observed_labels"] / out["label_rows"],
        0.0,
    )
    return out.sort_values("signal_date").reset_index(drop=True)


def _coverage_by_sector(context: Any, sector_labels: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "signal_date",
        "namespace",
        "sector_id",
        "member_count",
        "covered_count",
        "member_coverage",
        "context_status",
        "observed_labels",
    ]
    if context.sectors is None or context.sectors.empty:
        return pd.DataFrame(columns=columns)
    out = context.sectors[
        ["date", "namespace", "sector_id", "member_count", "covered_count", "coverage", "status"]
    ].rename(
        columns={
            "date": "signal_date",
            "coverage": "member_coverage",
            "status": "context_status",
        }
    )
    if sector_labels.empty:
        out["observed_labels"] = 0
    else:
        observed = (
            sector_labels.loc[sector_labels["label_status"].astype(str).eq("OK")]
            .groupby(["as_of_trade_date", "namespace", "sector_id"], sort=True)
            .size()
            .rename("observed_labels")
            .reset_index()
            .rename(columns={"as_of_trade_date": "signal_date"})
        )
        out = out.merge(
            observed,
            on=["signal_date", "namespace", "sector_id"],
            how="left",
        )
        out["observed_labels"] = out["observed_labels"].fillna(0).astype(int)
    return out[columns].sort_values(["signal_date", "namespace", "sector_id"]).reset_index(drop=True)


def _factor_missingness(features: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for factor_id in DIRECTIONAL_FACTOR_IDS + RISK_INDICATOR_IDS:
        available = int(pd.to_numeric(features[factor_id], errors="coerce").notna().sum())
        rows.append(
            {
                "entity_type": "stock",
                "factor_id": factor_id,
                "rows": len(features),
                "available_rows": available,
                "missing_rate": 1.0 - available / len(features) if len(features) else None,
            }
        )
    return pd.DataFrame(rows)


def _factor_ic(features: pd.DataFrame, labels: pd.DataFrame, split: pd.DataFrame) -> pd.DataFrame:
    stock = labels.loc[
        labels["entity_type"].astype(str).eq("stock")
        & labels["label_status"].astype(str).eq("OK")
    ][["entity_id", "as_of_trade_date", "horizon", "realized_return", "label_end_date"]]
    joined = features.merge(
        stock,
        left_on=["code", "date"],
        right_on=["entity_id", "as_of_trade_date"],
        how="inner",
    ).merge(split, left_on="date", right_on="signal_date", how="inner")
    rows = []
    for factor_id in DIRECTIONAL_FACTOR_IDS:
        for horizon in (1, 3, 5):
            selected = joined.loc[joined["horizon"].eq(horizon)]
            daily = []
            for _, dated in selected.groupby("date", sort=True):
                valid = dated[[factor_id, "realized_return"]].apply(pd.to_numeric, errors="coerce").dropna()
                if (
                    len(valid) >= 3
                    and valid[factor_id].nunique() > 1
                    and valid["realized_return"].nunique() > 1
                ):
                    daily.append(valid[factor_id].rank().corr(valid["realized_return"].rank()))
            finite = [float(value) for value in daily if pd.notna(value) and math.isfinite(float(value))]
            rows.append(
                {
                    "factor_id": factor_id,
                    "horizon": horizon,
                    "mean_rank_ic": float(np.mean(finite)) if finite else None,
                    "signal_dates": len(finite),
                }
            )
    return pd.DataFrame(rows)


def _formula_rows(features: pd.DataFrame, labels: pd.DataFrame, split: pd.DataFrame) -> pd.DataFrame:
    selected_dates = set(split["signal_date"].astype(str))
    rows = []
    for feature in features.loc[features["date"].astype(str).isin(selected_dates)].to_dict(orient="records"):
        for config_id in FORMULA_CONFIG_IDS:
            for horizon in (1, 3, 5):
                score = score_stock(feature, horizon, config_id)
                rows.append(
                    {
                        "entity_type": "stock",
                        "entity_id": str(feature["code"]),
                        "as_of_trade_date": str(feature["date"]),
                        "horizon": horizon,
                        "method": "formula",
                        "config_id": config_id,
                        "prediction_status": score.status,
                        "formula_score": score.formula_score,
                        "forecast_class": score.forecast_class,
                        "overextended": _clean(feature.get("overextended")),
                    }
                )
    predictions = pd.DataFrame(rows)
    label_columns = [
        "entity_type",
        "entity_id",
        "as_of_trade_date",
        "horizon",
        "realized_return",
        "target_class",
        "label_status",
        "label_end_date",
    ]
    predictions = predictions.merge(
        labels[label_columns],
        on=["entity_type", "entity_id", "as_of_trade_date", "horizon"],
        how="left",
        validate="many_to_one",
    )
    return predictions.merge(
        split,
        left_on="as_of_trade_date",
        right_on="signal_date",
        how="inner",
        validate="many_to_one",
    )


def _baseline_rows(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    split: pd.DataFrame,
    baseline_id: str,
) -> pd.DataFrame:
    if baseline_id not in {"M0_MOMENTUM", "LEGACY_SIGNAL_EXEC_V2"}:
        raise ContractError(f"unknown evaluation baseline: {baseline_id}")
    selected_dates = set(split["signal_date"].astype(str))
    rows = []
    for feature in features.loc[features["date"].astype(str).isin(selected_dates)].to_dict(orient="records"):
        momentum = pd.to_numeric(pd.Series([feature.get("F01")]), errors="coerce").iloc[0]
        interval = pd.to_numeric(pd.Series([feature.get("F07")]), errors="coerce").iloc[0]
        if baseline_id == "M0_MOMENTUM":
            valid = pd.notna(momentum)
            active = valid and float(momentum) > 0.0
        else:
            valid = pd.notna(momentum) and pd.notna(interval)
            active = valid and (float(momentum) + float(interval)) / 2.0 > 0.1
        for horizon in (1, 3, 5):
            rows.append(
                {
                    "entity_type": "stock",
                    "entity_id": str(feature["code"]),
                    "as_of_trade_date": str(feature["date"]),
                    "horizon": horizon,
                    "method": "formula",
                    "config_id": baseline_id,
                    "prediction_status": "OK" if valid else "INSUFFICIENT_FEATURES",
                    "formula_score": 100.0 if active else 0.0 if valid else None,
                    "forecast_class": "up" if active else "down" if valid else "unknown",
                    "overextended": False,
                }
            )
    predictions = pd.DataFrame(rows)
    label_columns = [
        "entity_type",
        "entity_id",
        "as_of_trade_date",
        "horizon",
        "realized_return",
        "target_class",
        "label_status",
        "label_end_date",
    ]
    return (
        predictions.merge(
            labels[label_columns],
            on=["entity_type", "entity_id", "as_of_trade_date", "horizon"],
            how="left",
            validate="many_to_one",
        )
        .merge(
            split,
            left_on="as_of_trade_date",
            right_on="signal_date",
            how="inner",
            validate="many_to_one",
        )
    )


def _session_offset(sessions: list[str], date: str, offset: int) -> str | None:
    try:
        position = sessions.index(str(date))
    except ValueError:
        return None
    target = position + int(offset)
    return sessions[target] if 0 <= target < len(sessions) else None


def _market_open(
    row: dict[str, Any],
    status: dict[str, Any] | None,
    *,
    data_source_mode: str,
    listing_board: str,
) -> MarketOpen:
    pre_close = Decimal(str(row["pre_close"])) if pd.notna(row.get("pre_close")) else None
    if data_source_mode == "demo":
        limit = Decimal("0.20") if listing_board in {"STAR", "CHINEXT"} else Decimal("0.10")
        up_limit = pre_close * (Decimal(1) + limit) if pre_close is not None else None
        down_limit = pre_close * (Decimal(1) - limit) if pre_close is not None else None
        suspended = False
        no_price_limit = False
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


def _simulate_formula_portfolio(
    dataset: dict[str, Any],
    formula_rows: pd.DataFrame,
    config_id: str,
    split_name: str,
    return_model: ReturnBinModel | None,
    *,
    candidate_type: str = "formula",
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    signal_dates = sorted(
        formula_rows.loc[
            formula_rows["config_id"].eq(config_id)
            & formula_rows["split"].eq(split_name),
            "as_of_trade_date",
        ].astype(str).unique()
    )
    empty_nav = pd.DataFrame(
        columns=["date", "nav_cents", "cash_cents", "status", "sector_exposure", "split", "candidate_id"]
    )
    if not signal_dates:
        metrics = {
            "split": split_name,
            "candidate_type": candidate_type,
            "config_id": config_id,
            "status": "NO_SIGNAL_DATES",
            "net_sharpe": 0.0,
            "turnover": 0.0,
            "closed_trades": 0,
            "trade_dates": 0,
            "max_drawdown": 0.0,
            "unresolved": True,
            "net_return": None,
            "reason_codes": "NO_SIGNAL_DATES",
        }
        return metrics, empty_nav, pd.DataFrame(), pd.DataFrame()

    sessions = list(dataset["sessions"])
    start = signal_dates[0]
    end = _session_offset(sessions, signal_dates[-1], 6) or signal_dates[-1]
    simulation_sessions = [date for date in sessions if start <= date <= end]
    raw = dataset["raw"].loc[
        dataset["raw"]["date"].astype(str).isin(simulation_sessions)
    ].copy()
    codes = sorted(dataset["universe"]["code"].astype(str).unique())
    statuses = dataset.get("trading_status", pd.DataFrame())
    status_lookup = {
        (str(row["code"]), str(row["date"])): row
        for row in statuses.to_dict(orient="records")
    }
    metadata = dataset["universe"].set_index("code").to_dict(orient="index")
    features = dataset["features"].set_index(["code", "date"], drop=False)
    score_rows = formula_rows.loc[formula_rows["config_id"].eq(config_id)]
    score_lookup = {
        (str(entity_id), str(date)): group.sort_values("horizon").to_dict(orient="records")
        for (entity_id, date), group in score_rows.groupby(
            ["entity_id", "as_of_trade_date"],
            sort=False,
        )
    }
    fee_schedule = FeeSchedule.research_defaults(
        simulation_sessions[0],
        source="standard-v2-research-fees-v1",
    )
    security_rules = {
        code: SecurityRule(
            lot_size=100,
            tick_size=Decimal("0.01"),
            effective_from=simulation_sessions[0],
            effective_to=None,
            source="standard-v2-cn-equity-rule-v1",
        )
        for code in codes
    }
    nav_rows: list[dict[str, Any]] = []
    allocation_rows: list[dict[str, Any]] = []
    pending_buys: list[PaperOrder] = []
    pending_exits: dict[str, PaperOrder] = {}
    with tempfile.TemporaryDirectory() as tmpdir:
        simulation_store = V2Store(Path(tmpdir) / "simulation.db", data_mode="test")
        simulation_store.migrate()
        account_id = f"evaluation-{config_id}-{split_name}"
        portfolio = PaperPortfolio(simulation_store, account_id)
        portfolio.open_account(method="formula", initial_cash_cents=100_000_000)
        for date in simulation_sessions:
            dated_raw = raw.loc[raw["date"].astype(str).eq(date)]
            raw_lookup = {
                str(row["code"]): row for row in dated_raw.to_dict(orient="records")
            }
            market = {
                code: _market_open(
                    row,
                    status_lookup.get((code, date)),
                    data_source_mode=str(dataset["data_source_mode"]),
                    listing_board=str(metadata.get(code, {}).get("listing_board") or ""),
                )
                for code, row in raw_lookup.items()
            }

            matured_by_code: dict[str, int] = {}
            for lot in portfolio.lots():
                if (
                    lot.status == "OPEN"
                    and lot.planned_exit_date
                    and str(lot.planned_exit_date) <= date
                ):
                    matured_by_code[lot.code] = matured_by_code.get(lot.code, 0) + lot.quantity
            for code, quantity in matured_by_code.items():
                if code in pending_exits or code not in raw_lookup:
                    continue
                reference = Decimal(str(raw_lookup[code]["pre_close"]))
                order_id = hashlib.sha256(
                    f"evaluation-exit|{account_id}|{code}|{date}".encode()
                ).hexdigest()
                pending_exits[code] = PaperOrder(
                    order_id=order_id,
                    run_id=f"{config_id}-{split_name}",
                    account_id=account_id,
                    code=code,
                    side="SELL",
                    quantity=quantity,
                    earliest_trade_date=date,
                    reference_price=reference,
                    price_ceiling_floor=None,
                )
            exit_orders = list(pending_exits.values())
            if exit_orders:
                exit_result = execute_open_orders(
                    simulation_store,
                    exit_orders,
                    market,
                    fee_schedule=fee_schedule,
                    security_rules=security_rules,
                )
                for result in exit_result.results:
                    order = next(item for item in exit_orders if item.order_id == result.order_id)
                    if not result.status.startswith("PENDING"):
                        pending_exits.pop(order.code, None)
            if pending_buys:
                execute_open_orders(
                    simulation_store,
                    pending_buys,
                    market,
                    fee_schedule=fee_schedule,
                    security_rules=security_rules,
                )
                pending_buys = []

            prices = {
                code: {"price": row.get("close"), "mark_only": False}
                for code, row in raw_lookup.items()
            }
            mark = mark_portfolio(simulation_store, account_id, date, prices)
            nav_rows.append(
                {
                    "date": date,
                    "nav_cents": mark.nav_cents,
                    "cash_cents": mark.cash_cents,
                    "status": mark.status,
                    "sector_exposure": mark.sector_exposure,
                    "split": split_name,
                    "candidate_id": config_id,
                }
            )
            if date not in signal_dates:
                continue
            entry_date = _session_offset(sessions, date, 1)
            planned_exit = _session_offset(sessions, date, 6)
            if entry_date is None or planned_exit is None or mark.nav_cents is None:
                continue
            lots = [lot for lot in portfolio.lots() if lot.status == "OPEN"]
            holdings = []
            for code in sorted({lot.code for lot in lots}):
                code_lots = [lot for lot in lots if lot.code == code]
                quantity = sum(lot.quantity for lot in code_lots)
                sellable = sum(lot.sellable_quantity(date) for lot in code_lots)
                reference = raw_lookup.get(code, {}).get("close")
                holdings.append(
                    {
                        "code": code,
                        "quantity": quantity,
                        "sellable_quantity": sellable,
                        "market_value_cny": float(reference) * quantity if reference is not None else 0.0,
                        "sector_id": (
                            features.loc[(code, date)].get("sector_id")
                            if (code, date) in features.index
                            else "UNKNOWN"
                        ),
                    }
                )
            holdings_by_code = {str(item["code"]): item for item in holdings}
            candidates = []
            for code in codes:
                key = (code, date)
                if key not in score_lookup or key not in features.index or code not in raw_lookup:
                    continue
                feature = features.loc[key]
                if isinstance(feature, pd.DataFrame):
                    feature = feature.iloc[-1]
                prediction_rows = score_lookup[key]
                h5 = next(
                    (item for item in prediction_rows if int(item["horizon"]) == 5),
                    None,
                )
                h3 = next(
                    (item for item in prediction_rows if int(item["horizon"]) == 3),
                    None,
                )
                if h5 is None or h3 is None:
                    continue
                score = pd.to_numeric(pd.Series([h5.get("formula_score")]), errors="coerce").iloc[0]
                if pd.isna(score) or return_model is None:
                    expected = None
                else:
                    expected = estimate_formula_return(
                        return_model,
                        score=float(score),
                        estimated_round_trip_cost=0.00212,
                    )
                enriched = []
                for item in prediction_rows:
                    current = dict(item)
                    current["overextended"] = _clean(feature.get("overextended"))
                    if int(item["horizon"]) == 5 and expected is not None:
                        current["expected_net_edge"] = expected.expected_net_edge
                    enriched.append(current)
                holding = holdings_by_code.get(code)
                decision = derive_research_intent(
                    enriched,
                    {
                        "current_quantity": int(holding.get("quantity", 0)) if holding else 0,
                        "sellable_quantity": int(holding.get("sellable_quantity", 0)) if holding else 0,
                        "account_available": True,
                    },
                    horizon=5,
                )
                candidates.append(
                    {
                        "code": code,
                        "horizon": 5,
                        "account_action": decision.account_action,
                        "prediction_status": h5["prediction_status"],
                        "trade_eligible": bool(metadata.get(code, {}).get("trade_eligible")),
                        "expected_net_edge": expected.expected_net_edge if expected is not None else None,
                        "allow_without_return_estimate": candidate_type == "benchmark",
                        "formula_score": h5.get("formula_score"),
                        "reference_price": raw_lookup[code].get("close"),
                        "atr_pct14": _clean(feature.get("Q01")),
                        "adv20_cny": _clean(feature.get("Q03")),
                        "sector_id": _clean(feature.get("sector_id")) or "UNKNOWN",
                        "planned_exit_date": planned_exit,
                    }
                )
            dated_features = dataset["features"].loc[dataset["features"]["date"].astype(str).eq(date)]
            breadth = pd.to_numeric(dated_features.get("market_breadth20"), errors="coerce").dropna()
            if not breadth.empty:
                allocation = allocate_orders(
                    candidates,
                    run_id=f"{config_id}-{split_name}-{date}",
                    account_id=account_id,
                    as_of_trade_date=date,
                    earliest_trade_date=entry_date,
                    nav_cents=int(mark.nav_cents),
                    cash_cents=portfolio.cash_cents(),
                    market_breadth20=float(breadth.iloc[0]),
                    holdings=holdings,
                    security_rules=security_rules,
                )
                pending_buys.extend(allocation.orders)
                allocation_rows.extend(
                    {
                        **row,
                        "candidate_id": config_id,
                        "split": split_name,
                        "signal_date": date,
                    }
                    for row in allocation.status_rows
                )

        nav = pd.DataFrame(nav_rows)
        fills = simulation_store.read_paper_fills(account_id)
        order_frame = simulation_store.read_paper_orders(account_id)
        closed = portfolio.closed_trades()
        portfolio_metrics = calculate_portfolio_metrics(
            nav,
            fills=fills,
            orders=order_frame,
            closed_trades=closed,
        )
        unresolved = portfolio_metrics.status != "OK"
        metrics = {
            "split": split_name,
            "candidate_type": candidate_type,
            "config_id": config_id,
            "status": portfolio_metrics.status,
            "net_sharpe": portfolio_metrics.sharpe if portfolio_metrics.sharpe is not None else 0.0,
            "turnover": portfolio_metrics.turnover if portfolio_metrics.turnover is not None else 0.0,
            "closed_trades": len(closed),
            "trade_dates": int(fills["trade_date"].nunique()) if not fills.empty else 0,
            "max_drawdown": portfolio_metrics.max_drawdown if portfolio_metrics.max_drawdown is not None else 0.0,
            "unresolved": unresolved,
            "net_return": portfolio_metrics.net_return,
            "reason_codes": "" if not unresolved else portfolio_metrics.status,
        }
        if not order_frame.empty:
            order_frame = order_frame.assign(candidate_id=config_id, split=split_name)
        if not closed.empty:
            closed = closed.assign(candidate_id=config_id, split=split_name)
        if allocation_rows:
            allocation_frame = pd.DataFrame(allocation_rows)
            order_frame = pd.concat([order_frame, allocation_frame], ignore_index=True, sort=False)
        return metrics, nav, closed, order_frame


def _prior_metrics(labels: pd.DataFrame, split: pd.DataFrame) -> pd.DataFrame:
    observed = labels.loc[
        labels["label_status"].astype(str).eq("OK")
        & labels["target_class"].astype(str).isin(("up", "flat", "down"))
    ].merge(split, left_on="as_of_trade_date", right_on="signal_date", how="inner")
    rows = []
    for entity_type in ("stock", "sector"):
        for horizon in (1, 3, 5):
            train = observed.loc[
                observed["entity_type"].eq(entity_type)
                & observed["horizon"].eq(horizon)
                & observed["split"].eq("train")
            ]
            counts = train["target_class"].value_counts()
            total = int(counts.sum())
            for label in ("up", "flat", "down"):
                rows.append(
                    {
                        "method": "class_frequency_prior",
                        "entity_type": entity_type,
                        "horizon": horizon,
                        "metric": f"p_{label}",
                        "value": float(counts.get(label, 0) / total) if total else None,
                        "records": total,
                        "status": "OK" if total else "INSUFFICIENT_HISTORY",
                    }
                )
    rows.append(
        {
            "method": "jev",
            "entity_type": "all",
            "horizon": 0,
            "metric": "historical_probability_evaluation",
            "value": None,
            "records": 0,
            "status": "UNAVAILABLE_CACHED_HISTORY",
        }
    )
    return pd.DataFrame(rows)


def run_fixed_evaluation(
    store: V2Store,
    artifact_root: str | Path,
    *,
    protocol: str = "fixed-v1",
) -> EvaluationRuntimeResult:
    if protocol != "fixed-v1":
        raise ContractError(f"unsupported evaluation protocol: {protocol}")
    dataset = _load_dataset(store)
    labels = dataset["labels"]
    _persist_labels(store, labels)
    mature_h5 = labels.loc[
        labels["horizon"].eq(5)
        & labels["label_status"].eq("OK")
        & labels["label_end_date"].astype(str).le(dataset["latest"])
    ]
    mature_dates = sorted(mature_h5["as_of_trade_date"].astype(str).unique())
    split = build_fixed_split(mature_dates, all_sessions=dataset["sessions"])
    split_frame = split.assignments
    calibration_start = split.block_starts.get("calibration", dataset["latest"])
    stock_cohort = select_cohort(
        dataset["universe"],
        dataset["memberships"],
        entity_type="stock",
        as_of=calibration_start,
        max_size=32,
    )
    sector_universe = (
        dataset["memberships"][["sector_id", "valid_from", "valid_to"]]
        .drop_duplicates()
        .reset_index(drop=True)
        if not dataset["memberships"].empty
        else pd.DataFrame(columns=["sector_id", "valid_from", "valid_to"])
    )
    sector_cohort = select_cohort(
        sector_universe,
        None,
        entity_type="sector",
        as_of=calibration_start,
        max_size=32,
    ) if not sector_universe.empty else pd.DataFrame()
    dataset_identity = {
        "protocol": protocol,
        "evaluation_implementation_version": "technical-v2-fixed-runtime-v2",
        "data_source_mode": store.data_mode,
        "latest_complete_date": dataset["latest"],
        "sessions": dataset["sessions"],
        "codes": sorted(dataset["universe"]["code"].astype(str)),
        "membership_rows": _records(dataset["memberships"]),
        "raw_partition_hash": sha256_json(_records(dataset["raw"])),
        "adjusted_panel_hash": sha256_json(_records(dataset["panel"])),
        "target_definition_version": TARGET_DEFINITION_VERSION,
        "mature_signal_dates": mature_dates,
        "split_status": split.status,
        "split_assignments": _records(split_frame),
        "stock_cohort": _records(stock_cohort),
        "sector_cohort": _records(sector_cohort),
    }
    dataset_hash = sha256_json(dataset_identity)
    run_id = f"evaluation-{dataset['latest']}-{dataset_hash[:16]}"
    run_dir = Path(artifact_root) / run_id
    completed = _completed_result(
        run_dir,
        run_id=run_id,
        dataset_hash=dataset_hash,
        protocol=protocol,
    )
    if completed is not None:
        return completed
    hashes: dict[str, str] = {}
    hashes["dataset_manifest.json"] = write_immutable_json(
        run_dir / "dataset_manifest.json",
        {
            "schema_version": "technical-v2-dataset-manifest.v1",
            "run_id": run_id,
            "dataset_hash": dataset_hash,
            **dataset_identity,
        },
    )

    coverage_date = _coverage_by_date(dataset["features"], labels)
    coverage_sector = _coverage_by_sector(dataset["context"], dataset["sector_labels"])
    missingness = _factor_missingness(dataset["features"])
    data_audit = {
        "schema_version": "technical-v2-data-audit.v1",
        "run_id": run_id,
        "status": "OK" if split.status == "OK" else split.status,
        "latest_complete_date": dataset["latest"],
        "session_count": len(dataset["sessions"]),
        "stock_entities": int(dataset["universe"]["code"].nunique()),
        "sector_entities": int(dataset["memberships"]["sector_id"].nunique()) if not dataset["memberships"].empty else 0,
        "feature_rows": len(dataset["features"]),
        "label_rows": len(labels),
        "mature_signal_dates": len(mature_dates),
        "reason_codes": list(split.reason_codes),
    }
    hashes["data_audit.json"] = write_immutable_json(run_dir / "data_audit.json", data_audit)
    hashes["coverage_by_date.csv"] = _write_immutable_csv(run_dir / "coverage_by_date.csv", coverage_date)
    hashes["coverage_by_sector.csv"] = _write_immutable_csv(run_dir / "coverage_by_sector.csv", coverage_sector)
    hashes["factor_missingness.csv"] = _write_immutable_csv(run_dir / "factor_missingness.csv", missingness)

    formula_selection = FormulaSelection(
        "INSUFFICIENT_HISTORY",
        "F0_BALANCED",
        (),
        {config_id: ("INSUFFICIENT_HISTORY",) for config_id in FORMULA_CONFIG_IDS},
        tuple(split.reason_codes) or ("INSUFFICIENT_HISTORY",),
    )
    jev_selection = JevSelection(
        "UNAVAILABLE_CACHED_HISTORY",
        None,
        None,
        (),
        ("JEV_HISTORICAL_CACHE_UNAVAILABLE",),
    )
    factor_ic = pd.DataFrame(columns=["factor_id", "horizon", "mean_rank_ic", "signal_dates"])
    candidate_comparison = pd.DataFrame(
        [
            {
                "split": "validation",
                "candidate_type": (
                    "formula"
                    if candidate in FORMULA_CONFIG_IDS
                    else "jev"
                    if candidate.startswith("J")
                    else "probability_baseline"
                    if candidate == "CLASS_FREQUENCY_PRIOR"
                    else "benchmark"
                ),
                "config_id": candidate,
                "status": "INSUFFICIENT_HISTORY",
                "net_sharpe": None,
                "turnover": None,
                "closed_trades": 0,
                "trade_dates": 0,
                "max_drawdown": None,
                "unresolved": True,
                "net_return": None,
                "reason_codes": "REQUIRES_504_MATURE_SIGNAL_DATES",
            }
            for candidate in (
                *FORMULA_CONFIG_IDS,
                "M0_MOMENTUM",
                "LEGACY_SIGNAL_EXEC_V2",
                "J0_EQUAL_POOL",
                "J1_WEIGHTED_POOL",
                "CLASS_FREQUENCY_PRIOR",
            )
        ]
    )
    probability = pd.DataFrame(
        [
            {
                "method": "jev",
                "entity_type": "all",
                "horizon": 0,
                "metric": "historical_probability_evaluation",
                "value": None,
                "records": 0,
                "status": "INSUFFICIENT_HISTORY",
            }
        ]
    )
    calibration_bins = pd.DataFrame(
        columns=[
            "config_id",
            "entity_type",
            "horizon",
            "lower",
            "upper",
            "records",
            "dates",
            "mean_return",
            "status",
        ]
    )
    daily_nav = pd.DataFrame(columns=["date", "nav_cents", "cash_cents", "status", "split", "candidate_id"])
    trades = pd.DataFrame(columns=["candidate_id", "split", "code", "entry_date", "exit_date", "net_return", "status"])
    orders = pd.DataFrame(columns=["candidate_id", "split", "code", "side", "status", "reason_codes"])
    final_test_opened = False

    if split.status == "OK":
        formula_rows = _formula_rows(dataset["features"], labels, split_frame)
        factor_ic = _factor_ic(dataset["features"], labels, split_frame)
        validation_boundary = split.block_starts["test"]
        validation_rows = purge_cross_boundary(
            formula_rows.loc[formula_rows["split"].eq("validation")],
            validation_boundary,
        )
        metric_rows = []
        nav_frames = []
        trade_frames = []
        order_frames = []
        calibration_records = []
        return_models: dict[str, ReturnBinModel] = {}
        training_boundary = split.block_starts["calibration"]
        training_rows = purge_cross_boundary(
            formula_rows.loc[formula_rows["split"].eq("train")],
            training_boundary,
        )
        for config_id in FORMULA_CONFIG_IDS:
            train_h5 = training_rows.loc[
                training_rows["config_id"].eq(config_id)
                & training_rows["horizon"].eq(5)
                & training_rows["label_status"].eq("OK")
            ].copy()
            model = fit_return_bins(train_h5, entity_type="stock", horizon=5)
            return_models[config_id] = model
            for item in model.bins:
                calibration_records.append(
                    {
                        "config_id": config_id,
                        "entity_type": "stock",
                        "horizon": 5,
                        "lower": item.lower,
                        "upper": item.upper,
                        "records": item.records,
                        "dates": item.dates,
                        "mean_return": item.mean_return,
                        "status": "OK" if model.global_records >= 1000 else "INSUFFICIENT_GLOBAL_SUPPORT",
                    }
                )
            metrics, nav, candidate_trades, candidate_orders = _simulate_formula_portfolio(
                dataset,
                validation_rows,
                config_id,
                "validation",
                model,
            )
            metric_rows.append(metrics)
            nav_frames.append(nav)
            if not candidate_trades.empty:
                trade_frames.append(candidate_trades)
            if not candidate_orders.empty:
                order_frames.append(candidate_orders)
        for baseline_id in ("M0_MOMENTUM", "LEGACY_SIGNAL_EXEC_V2"):
            baseline_rows = _baseline_rows(
                dataset["features"],
                labels,
                split_frame,
                baseline_id,
            )
            baseline_validation = purge_cross_boundary(
                baseline_rows.loc[baseline_rows["split"].eq("validation")],
                validation_boundary,
            )
            metrics, nav, candidate_trades, candidate_orders = _simulate_formula_portfolio(
                dataset,
                baseline_validation,
                baseline_id,
                "validation",
                None,
                candidate_type="benchmark",
            )
            metric_rows.append(metrics)
            nav_frames.append(nav)
            if not candidate_trades.empty:
                trade_frames.append(candidate_trades)
            if not candidate_orders.empty:
                order_frames.append(candidate_orders)
        metric_rows.extend(
            [
                {
                    "split": "validation",
                    "candidate_type": "jev",
                    "config_id": pool_id,
                    "status": "UNAVAILABLE_CACHED_HISTORY",
                    "net_sharpe": 0.0,
                    "turnover": 0.0,
                    "closed_trades": 0,
                    "trade_dates": 0,
                    "max_drawdown": 0.0,
                    "unresolved": True,
                    "net_return": None,
                    "reason_codes": "JEV_HISTORICAL_CACHE_UNAVAILABLE",
                }
                for pool_id in ("J0_EQUAL_POOL", "J1_WEIGHTED_POOL")
            ]
        )
        candidate_comparison = pd.DataFrame(metric_rows)
        formula_selection = select_formula_candidate(
            candidate_comparison.loc[candidate_comparison["candidate_type"].eq("formula")]
        )
        probability = _prior_metrics(labels, split_frame)
        calibration_bins = pd.DataFrame(calibration_records)
        daily_nav = pd.concat(nav_frames, ignore_index=True) if nav_frames else daily_nav
        trades = pd.concat(trade_frames, ignore_index=True, sort=False) if trade_frames else trades
        orders = pd.concat(order_frames, ignore_index=True, sort=False) if order_frames else orders

    hashes["factor_ic.csv"] = _write_immutable_csv(run_dir / "factor_ic.csv", factor_ic)
    hashes["probability_metrics.csv"] = _write_immutable_csv(
        run_dir / "probability_metrics.csv",
        probability,
    )
    hashes["calibration_bins.csv"] = _write_immutable_csv(
        run_dir / "calibration_bins.csv",
        calibration_bins,
    )
    selection_hash = write_selection_artifact(
        run_dir / "selection.json",
        formula_selection,
        jev_selection,
        metadata={
            "run_id": run_id,
            "protocol": protocol,
            "dataset_hash": dataset_hash,
            "split_status": split.status,
            "selection_basis": "validation_only",
        },
    )
    hashes["selection.json"] = hashlib.sha256((run_dir / "selection.json").read_bytes()).hexdigest()

    if split.status == "OK" and formula_selection.status == "SELECTED":
        formula_rows = _formula_rows(dataset["features"], labels, split_frame)
        metrics, nav, candidate_trades, candidate_orders = _simulate_formula_portfolio(
            dataset,
            formula_rows,
            formula_selection.selected_config_id,
            "test",
            return_models[formula_selection.selected_config_id],
        )
        candidate_comparison = pd.concat(
            [candidate_comparison, pd.DataFrame([metrics])],
            ignore_index=True,
        )
        daily_nav = pd.concat([daily_nav, nav], ignore_index=True)
        if not candidate_trades.empty:
            trades = pd.concat([trades, candidate_trades], ignore_index=True, sort=False)
        if not candidate_orders.empty:
            orders = pd.concat([orders, candidate_orders], ignore_index=True, sort=False)
        final_test_opened = True
        # The selection artifact stays immutable; final-test results are recorded in the run manifest.
    hashes["candidate_comparison.csv"] = _write_immutable_csv(
        run_dir / "candidate_comparison.csv",
        candidate_comparison,
    )
    hashes["daily_nav.csv"] = _write_immutable_csv(run_dir / "daily_nav.csv", daily_nav)
    hashes["trades.csv"] = _write_immutable_csv(run_dir / "trades.csv", trades)
    hashes["orders.csv"] = _write_immutable_csv(run_dir / "orders.csv", orders)

    status = "OK" if final_test_opened else split.status if split.status != "OK" else formula_selection.status
    run_manifest = {
        "schema_version": "technical-v2-evaluation-run.v1",
        "run_id": run_id,
        "protocol": protocol,
        "data_source_mode": store.data_mode,
        "dataset_hash": dataset_hash,
        "selection_sha256": selection_hash,
        "selection_status": formula_selection.status,
        "jev_selection_status": jev_selection.status,
        "split_status": split.status,
        "final_test_opened": final_test_opened,
        "status": status,
        "artifact_hashes": dict(sorted(hashes.items())),
    }
    hashes["run_manifest.json"] = write_immutable_json(run_dir / "run_manifest.json", run_manifest)
    if {path.name for path in run_dir.iterdir()} != set(REQUIRED_RESULT_FILES):
        raise ContractError("evaluation result package is incomplete")
    return EvaluationRuntimeResult(
        run_id=run_id,
        run_dir=run_dir,
        status=status,
        selection_status=formula_selection.status,
        split_status=split.status,
        final_test_opened=final_test_opened,
        label_rows=len(labels),
        mature_signal_dates=len(mature_dates),
        artifact_hashes=hashes,
    )
