from __future__ import annotations

import json
import fcntl
import multiprocessing
import sqlite3
import uuid
import resource
import time
from contextlib import ExitStack, closing, contextmanager
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from core.analysis.sector_v2 import TechnicalContext, build_context
from core.data.symbols import is_buyable_mainboard_ts_code
from core.data.v2_store import V2Store
from core.factors.technical_v2 import apply_adjustment_factors, compute_technical_v2
from core.pipeline.prism_compare_config import REPOSITORY_ROOT, file_sha256, write_json
from core.pipeline.technical_v2 import build_fixed_split, build_labels, purge_cross_boundary
from core.strategies.formula_v2 import ReturnBinModel, fit_return_bins, score_stock
from core.strategies.prism_a_share import market_regimes
from core.technical_v2.contracts import ContractError, sha256_json

_VERIFIED_SNAPSHOTS: set[tuple[str, int, int, str]] = set()
_VERIFIED_CACHE_FILES: set[tuple[str, int, int, str]] = set()

REPLAY_COLUMNS = (
    "code", "date", "execution_open", "valuation_close", "comparison_close", "adj_factor",
    "real_bar", "input_status", "roster_active", "instrument_type", "listing_board", "list_date",
    "delist_date", "metadata_status", "sector_id", "namespace", "market_context_status",
    "sector_context_status", "market_breadth20", "is_suspended", "is_risk_warning", "up_limit",
    "down_limit", "no_price_limit", "feature_status", "factor_valid_count", "prediction_status",
    "score1", "score3", "score5", "forecast_class1", "forecast_class3", "forecast_class5",
    "overextended", "Q01", "Q02", "Q02_prior60_median", "Q03", "market_state",
    "state_multiplier", "state_reason",
)


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
    real_source = panel.data_source_mode.eq("real") & panel.completeness.eq("COMPLETE") & panel.roster_active
    # Opening execution must not depend on the same session's completed OHLCV.
    panel["execution_open"] = prices.open.where(real_source & np.isfinite(prices.open) & prices.open.gt(0))
    panel["valuation_close"] = prices.close.where(real_source & np.isfinite(prices.close) & prices.close.gt(0))
    valid = (np.isfinite(prices).all(axis=1) & prices.gt(0).all(axis=1)
             & prices.high.ge(prices[["open", "close", "low"]].max(axis=1))
             & prices.low.le(prices[["open", "close", "high"]].min(axis=1)))
    valid &= real_source
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


def score_features(features: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    out = features.copy()
    scores = {horizon: [] for horizon in config["native_contract"]["horizons"]}
    classes = {horizon: [] for horizon in scores}
    statuses = []
    factor_ids = config["native_contract"]["factor_ids"]
    for values in out[factor_ids].itertuples(index=False, name=None):
        inputs = dict(zip(factor_ids, values))
        for horizon in scores:
            result = score_stock(inputs, horizon, config["strategies"]["A0_V2_F0"]["formula"])
            scores[horizon].append(result.formula_score)
            classes[horizon].append(result.forecast_class)
            if horizon == config["strategies"]["A0_V2_F0"]["primary_horizon"]:
                statuses.append(result.status)
    for horizon in scores:
        out[f"score{horizon}"] = scores[horizon]
        out[f"forecast_class{horizon}"] = classes[horizon]
    out["prediction_status"] = statuses
    return out


def compute_shared_features(panel: pd.DataFrame, context: TechnicalContext,
                            config: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    features = compute_technical_v2(panel, context, as_of=panel.date.max())
    window = config["strategies"]["B0_PRISM_A_SHARE_V1"]["adaptive_stop"]["prior_vol_median_sessions"]
    features["Q02_prior60_median"] = features.groupby("code", sort=False).Q02.transform(
        lambda series: series.shift(1).rolling(window, min_periods=window).median())
    features = score_features(features, config)
    labels = build_labels(features[["code", "date", "adjusted_open", "Q02", "mark_only"]],
                          sorted(panel.date.unique().tolist()),
                          horizons=[config["strategies"]["A0_V2_F0"]["primary_horizon"]])
    return features, labels


def fixed_split_manifest(labels: pd.DataFrame, sessions: list[str],
                         config: dict[str, Any]) -> tuple[dict[str, Any], pd.DataFrame]:
    mature = sorted(labels.loc[labels.label_status.eq("OK"), "as_of_trade_date"].astype(str).unique())
    plan = build_fixed_split(mature, all_sessions=sessions)
    assignments = plan.assignments.copy()
    manifest = {"status": plan.status, "reason_codes": list(plan.reason_codes),
                "mature_signal_date_count": len(mature), "required_mature_dates": sum(config["split"]["counts"].values()),
                "missing_mature_dates": max(0, sum(config["split"]["counts"].values()) - len(mature)),
                "boundaries": plan.block_starts, "warmup_sessions": plan.warmup_dates, "replay_windows": {}}
    if plan.status != "OK":
        return manifest, assignments
    index = {date: position for position, date in enumerate(sessions)}
    horizon = config["strategies"]["A0_V2_F0"]["max_planned_holding_sessions"]
    tail = config["split"]["final_tail_sessions"]
    split_names = list(config["split"]["counts"])
    next_boundary = {name: plan.block_starts[split_names[position + 1]]
                     for position, name in enumerate(split_names[:-1])}
    assignments["common_planned_label_end_date"] = [sessions[index[date] + 1 + horizon] for date in assignments.signal_date]
    assignments["signal_allowed"] = [name not in next_boundary or end < next_boundary[name]
                                     for name, end in zip(assignments.split, assignments.common_planned_label_end_date)]
    for name in config["evaluation"]["splits"]:
        selected = assignments.loc[assignments.split.eq(name) & assignments.signal_allowed, "signal_date"].tolist()
        if not selected or index[selected[-1]] + tail >= len(sessions):
            raise ContractError("fixed split has no common mature replay window")
        end = sessions[index[selected[-1]] + tail]
        manifest["replay_windows"][name] = {"start_date": selected[0], "end_date": end, "signal_dates": selected,
            "common_session_count": index[end] - index[selected[0]] + 1, "tail_sessions": tail,
            "excluded_boundary_signals": int((assignments.split.eq(name) & ~assignments.signal_allowed).sum())}
    return manifest, assignments


def write_replay_chunk(path: str | Path, features: pd.DataFrame, sessions: list[str]) -> dict[str, Any]:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    code_count = features.code.nunique()
    ordered = features.sort_values(["date", "code"]).reset_index(drop=True)
    if code_count == 0 or len(features) != code_count * len(sessions) or ordered.duplicated(["code", "date"]).any():
        raise ContractError("replay chunk must retain the full code by actual-session grid")
    if sorted(ordered.date.astype(str).unique()) != sessions:
        raise ContractError("replay chunk dates differ from the frozen calendar")
    temporary = path.with_suffix(".tmp.parquet")
    pq.write_table(pa.Table.from_pandas(ordered, preserve_index=False), temporary,
                   row_group_size=code_count, compression="zstd")
    temporary.replace(path)
    return {"path": str(path), "sha256": file_sha256(path), "code_count": code_count,
            "row_count": len(ordered), "row_group_count": len(sessions)}


def _verify_cache_file(record: dict[str, Any]) -> None:
    path = Path(record["path"])
    stat = path.stat()
    identity = (str(path), stat.st_size, stat.st_mtime_ns, record["sha256"])
    if identity not in _VERIFIED_CACHE_FILES:
        if file_sha256(path) != record["sha256"]:
            raise ContractError(f"shared cache SHA256 mismatch: {path}")
        _VERIFIED_CACHE_FILES.add(identity)


def load_daily_cache(experiment_root: str | Path, dates: list[str] | None = None):
    manifest = json.loads((Path(experiment_root) / "feature_manifest.json").read_text())
    sessions = manifest["sessions"]
    selected = sessions if dates is None else dates
    if selected != sorted(set(selected)) or not set(selected).issubset(sessions):
        raise ContractError("daily reader requires ordered dates within the common calendar")
    index = {date: position for position, date in enumerate(sessions)}
    with _parquet_files(manifest["replay_chunks"]) as files:
        for date in selected:
            pieces = [file.read_row_group(index[date]).to_pandas() for file in files]
            frame = pd.concat(pieces, ignore_index=True).sort_values("code").reset_index(drop=True)
            if not frame.date.astype(str).eq(date).all() or frame.code.duplicated().any():
                raise ContractError("daily row-group cache has invalid date/code coverage")
            yield date, frame


@contextmanager
def _parquet_files(records: list[dict[str, Any]]):
    files = []
    try:
        for record in records:
            _verify_cache_file(record)
            file = pq.ParquetFile(record["path"])
            files.append(file)
            if file.num_row_groups != record["row_group_count"]:
                raise ContractError("replay row-group count does not match the calendar")
        yield files
    finally:
        for file in files:
            file.close()


class _SnapshotStore(V2Store):
    def _connect(self):
        connection = readonly_connection(self.path)
        connection.row_factory = sqlite3.Row
        return connection


def _data_identity(snapshot: dict[str, Any], audit: SnapshotAudit, config: dict[str, Any]) -> str:
    modules = ("core/factors/technical_v2.py", "core/analysis/sector_v2.py",
               "core/strategies/formula_v2.py", "core/strategies/prism_a_share.py",
               "core/pipeline/technical_v2.py", "core/pipeline/prism_compare_data.py",
               "core/data/v2_store.py")
    return sha256_json({"snapshot": snapshot["snapshot_sha256"], "sessions": audit.sessions,
        "codes": audit.roster.code.tolist(), "configuration": config,
        "data_implementation": {path: file_sha256(REPOSITORY_ROOT / path) for path in modules}})


def _memory_budget(config: dict[str, Any]) -> int:
    values = {line.split(":", 1)[0]: int(line.split()[1]) * 1024
              for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:")}
    return int(values["MemAvailable"] * config["resources"]["available_ram_fraction"])


def _check_memory(budget: int) -> None:
    if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024 > budget:
        raise ContractError("feature preparation exceeded its frozen available-RAM budget")


def _progress(root: Path, event: str, **values: Any) -> None:
    payload = {"event": event, **values}
    write_json(root / "prepare_progress.json", payload)
    print(json.dumps(payload, ensure_ascii=True, allow_nan=False), flush=True)


def _feature_chunk(arguments):
    position, panel_path, roster, cache, snapshot_db, context, regimes, config, sessions, budget = arguments
    receipt_path = cache / f"chunk_{position:04d}.json"
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        for key in ("features", "labels", "replay"):
            _verify_cache_file(receipt[key])
        return receipt
    started = time.perf_counter()
    store = _SnapshotStore(snapshot_db, data_mode="real")
    panel = pd.read_parquet(panel_path)
    features, labels = compute_shared_features(panel, context, config)
    extras = [column for column in roster.columns if column not in features.columns]
    features = features.merge(roster[["code", *extras]], on="code", how="left", validate="many_to_one")
    statuses = store.read_trading_status(sessions[0], sessions[-1], roster.code.tolist())
    status_columns = ["code", "date", "is_suspended", "is_risk_warning", "up_limit", "down_limit", "no_price_limit"]
    features = features.merge(statuses[status_columns], on=["code", "date"], how="left", validate="one_to_one")
    features = features.merge(regimes[["date", "market_state", "state_multiplier", "state_reason"]],
                              on="date", how="left", validate="many_to_one")
    feature_path, label_path = cache / f"features_{position:04d}.parquet", cache / f"labels_{position:04d}.parquet"
    features.to_parquet(feature_path, index=False, compression="zstd")
    labels.to_parquet(label_path, index=False, compression="zstd")
    replay = write_replay_chunk(cache / "replay" / f"{position:04d}.parquet", features[list(REPLAY_COLUMNS)], sessions)
    receipt = {"features": {"path": str(feature_path), "sha256": file_sha256(feature_path)},
               "labels": {"path": str(label_path), "sha256": file_sha256(label_path)}, "replay": replay,
               "elapsed_seconds": time.perf_counter() - started}
    _check_memory(budget)
    write_json(receipt_path, receipt, immutable=True)
    return receipt


def _cache_features(root: Path, snapshot: dict[str, Any], audit: SnapshotAudit,
                    config: dict[str, Any], *, workers: int = 1) -> dict[str, Any]:
    if type(workers) is not int or not 1 <= workers <= config["resources"]["max_cpu_threads"]:
        raise ContractError("feature workers exceed the configured process budget")
    identity = _data_identity(snapshot, audit, config)
    manifest_path = root / "feature_manifest.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text())
        if previous["feature_identity"] == identity:
            for record in [*previous["replay_chunks"], *previous["feature_chunks"], *previous["label_chunks"]]:
                _verify_cache_file(record)
            return previous
    started, budget = time.perf_counter(), _memory_budget(config)
    cache = root / "features" / identity[:20]
    cache.mkdir(parents=True, exist_ok=True)
    store = _SnapshotStore(snapshot["snapshot_db"], data_mode="real")
    size = config["data"]["feature_chunk_stocks"]
    roster_chunks = [audit.roster.iloc[offset:offset + size].copy() for offset in range(0, len(audit.roster), size)]
    panels, context_inputs = [], []
    for position, roster in enumerate(roster_chunks):
        path = cache / "panels" / f"{position:04d}.parquet"
        receipt_path = path.with_suffix(".json")
        if receipt_path.exists():
            record = json.loads(receipt_path.read_text())
            _verify_cache_file(record)
            panel = pd.read_parquet(path)
        else:
            codes = roster.code.tolist()
            raw = store.read_daily_raw(audit.sessions[0], audit.sessions[-1], codes)
            adjustments = store.read_adjustments(audit.sessions[0], audit.sessions[-1], codes)
            panel = align_panel(raw, adjustments, audit.sessions, roster)
            path.parent.mkdir(parents=True, exist_ok=True)
            panel.to_parquet(path, index=False, compression="zstd")
            record = {"path": str(path), "sha256": file_sha256(path)}
            write_json(receipt_path, record, immutable=True)
        panels.append(path)
        context_inputs.append(panel[["code", "date", "adjusted_close", "amount_cny", "mark_only"]])
        _check_memory(budget)
        _progress(root, "aligned_chunk", chunk=position + 1, total_chunks=len(roster_chunks), rows=len(panel))
    context_path = cache / "context_manifest.json"
    if context_path.exists():
        context_record = json.loads(context_path.read_text())
        for record in context_record.values():
            _verify_cache_file(record)
        context = TechnicalContext(*(pd.read_parquet(context_record[key]["path"]) for key in ("market", "sectors", "assignments")))
    else:
        context_panel = pd.concat(context_inputs, ignore_index=True)
        universe = audit.roster[["code", "context_valid_from", "context_valid_to"]].rename(
            columns={"context_valid_from": "valid_from", "context_valid_to": "valid_to"})
        context = build_context(context_panel, audit.memberships, as_of=audit.sessions[-1],
            universe=universe, sessions=audit.sessions,
            min_context_member_coverage=config["data"]["min_context_member_coverage"],
            min_sector_members=config["data"]["min_sector_members"])
        context_record = {}
        for key in ("market", "sectors", "assignments"):
            path = cache / f"context_{key}.parquet"
            getattr(context, key).to_parquet(path, index=False, compression="zstd")
            context_record[key] = {"path": str(path), "sha256": file_sha256(path)}
        write_json(context_path, context_record, immutable=True)
        del context_panel
    del context_inputs
    _check_memory(budget)
    regimes = market_regimes(context.market, config)
    regimes.to_parquet(cache / "market_regimes.parquet", index=False, compression="zstd")
    _progress(root, "context_complete", elapsed_seconds=time.perf_counter() - started,
              market_sessions=len(context.market), sector_rows=len(context.sectors))
    feature_records, label_records, replay_records, mature_dates = [], [], [], set()
    chunk_budget = budget if workers == 1 else budget // (workers + 1)
    arguments = [(position, panel_path, roster, cache, snapshot["snapshot_db"], context,
                  regimes, config, audit.sessions, chunk_budget)
                 for position, (panel_path, roster) in enumerate(zip(panels, roster_chunks))]
    with ExitStack() as stack:
        if workers == 1:
            receipts = map(_feature_chunk, arguments)
        else:
            pool = stack.enter_context(ProcessPoolExecutor(max_workers=workers,
                mp_context=multiprocessing.get_context("spawn")))
            receipts = pool.map(_feature_chunk, arguments)
        for position, receipt in enumerate(receipts):
            feature_records.append(receipt["features"])
            label_records.append(receipt["labels"])
            replay_records.append(receipt["replay"])
            maturity = pd.read_parquet(receipt["labels"]["path"], columns=["as_of_trade_date", "label_status"])
            mature_dates.update(maturity.loc[maturity.label_status.eq("OK"), "as_of_trade_date"].tolist())
            _check_memory(chunk_budget)
            _progress(root, "feature_chunk_complete", chunk=position + 1, total_chunks=len(roster_chunks),
                      chunk_elapsed_seconds=receipt["elapsed_seconds"], elapsed_seconds=time.perf_counter() - started)
    manifest = {"status": "COMPLETE", "feature_identity": identity, "snapshot_sha256": snapshot["snapshot_sha256"],
                "feature_workers": workers,
                "sessions": audit.sessions, "roster_count": len(audit.roster), "feature_chunks": feature_records,
                "label_chunks": label_records, "replay_chunks": replay_records, "mature_signal_dates": sorted(mature_dates),
                "market_regimes_path": str(cache / "market_regimes.parquet"), "context": context_record,
                "elapsed_seconds": time.perf_counter() - started, "memory_budget_bytes": budget,
                "peak_process_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}
    write_json(manifest_path, manifest)
    return manifest


def _training_rows(manifest: dict[str, Any], start: str, next_start: str) -> pd.DataFrame:
    pieces = []
    columns = ["code", "entity_type", "horizon", "as_of_trade_date", "label_end_date", "realized_return", "label_status"]
    for features, labels in zip(manifest["feature_chunks"], manifest["label_chunks"]):
        label_frame = pd.read_parquet(labels["path"], columns=columns,
            filters=[("as_of_trade_date", ">=", start), ("as_of_trade_date", "<", next_start)])
        label_frame = purge_cross_boundary(label_frame, next_start)
        score_frame = pd.read_parquet(features["path"], columns=["code", "date", "score5"],
            filters=[("date", ">=", start), ("date", "<", next_start)]).rename(
                columns={"date": "as_of_trade_date", "score5": "formula_score"})
        rows = label_frame.merge(score_frame, on=["code", "as_of_trade_date"], how="left", validate="one_to_one")
        rows["split"] = "train"
        pieces.append(rows)
    return pd.concat(pieces, ignore_index=True)


def _smoke(root: Path, snapshot: dict[str, Any], audit: SnapshotAudit, config: dict[str, Any]) -> dict[str, Any]:
    from core.backtest.prism_compare_engine import replay_frames

    count, length = config["resources"]["smoke_stocks"], config["resources"]["smoke_sessions"]
    sessions = audit.sessions[:length]
    roster = audit.roster.loc[audit.roster.instrument_type.eq("stock") & audit.roster.list_date.le(audit.sessions[0])].sort_values("code").head(count)
    if len(roster) != count or len(sessions) != length:
        raise ContractError("snapshot cannot provide the configured real offline smoke scope")
    small_audit = SnapshotAudit(audit.audit, sessions, roster,
        audit.memberships.loc[audit.memberships.code.isin(roster.code)],
        audit.actions.loc[audit.actions.code.isin(roster.code)])
    identity = sha256_json({"data": _data_identity(snapshot, small_audit, config),
                            "replay": file_sha256(REPOSITORY_ROOT / "core/backtest/prism_compare_engine.py")})
    directory = root / "smoke" / identity[:20]
    path = directory / "smoke_manifest.json"
    if path.exists():
        return json.loads(path.read_text())
    started = time.perf_counter()
    feature_manifest = _cache_features(directory, snapshot, small_audit, config)
    warmup = config["data"]["warmup_sessions"]
    if warmup + config["split"]["final_tail_sessions"] >= length:
        raise ContractError("configured smoke leaves no warmup-separated signal window")
    rows = _training_rows(feature_manifest, sessions[0], sessions[warmup])
    model = fit_common_bins(rows, sessions[warmup])
    signal_dates = sessions[warmup:-config["split"]["final_tail_sessions"]]
    metrics = {}
    for strategy in ("A0_V2_F0", "B0_PRISM_A_SHARE_V1"):
        cell = directory / strategy
        if (cell / "account.db").exists() and not (cell / "metrics.json").exists():
            cell = directory / f"{strategy}_retry_{uuid.uuid4().hex[:8]}"
        if (cell / "metrics.json").exists():
            metrics[strategy] = json.loads((cell / "metrics.json").read_text())
        else:
            metrics[strategy] = replay_frames(load_daily_cache(directory, sessions[warmup:]),
                sessions=sessions, signal_dates=signal_dates, config=config, strategy_id=strategy,
                split="smoke", cost_scenario="base", model=model, lambda_c=1., output_dir=cell,
                run_id=f"smoke-{identity[:12]}", scope_flags=[*audit.audit["scope_flags"], "SMOKE_SUBUNIVERSE_ONLY"],
                corporate_actions=small_audit.actions)["metrics"]
    result = {"status": "COMPLETE", "identity": identity, "stock_count": count, "session_count": length,
              "start_date": sessions[0], "end_date": sessions[-1], "codes": roster.code.tolist(),
              "feature_identity": feature_manifest["feature_identity"], "metrics": metrics,
              "elapsed_seconds": time.perf_counter() - started,
              "peak_process_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
              "interpretation": "OFFLINE_BUSINESS_SMOKE_NOT_A_SHORTENED_MAIN_EXPERIMENT", "api_calls": 0}
    write_json(path, result, immutable=True)
    _progress(root, "smoke_complete", stock_count=count, session_count=length, elapsed_seconds=result["elapsed_seconds"])
    return result


def prepare(source_db: str | Path, experiment_root: str | Path, config: dict[str, Any]) -> dict[str, Any]:
    root = Path(experiment_root).resolve()
    with experiment_lock(root):
        snapshot = _create_snapshot(source_db, root)
        audit = audit_snapshot(snapshot["snapshot_db"], config)
        identity = _data_identity(snapshot, audit, config)
        path = root / "dataset_manifest.json"
        if path.exists():
            previous = json.loads(path.read_text())
            if previous["feature_identity"] == identity:
                _cache_features(root, snapshot, audit, config)
                _verify_cache_file(previous["common_return_bins"])
                return previous
        write_json(root / "data_audit.json", audit.audit, immutable=True)
        write_json(root / "sessions.json", audit.sessions, immutable=True)
        write_json(root / "parameters.json", config, immutable=True)
        audit.roster.to_parquet(root / "roster.parquet", index=False)
        audit.memberships.to_parquet(root / "memberships.parquet", index=False)
        audit.actions.to_parquet(root / "corporate_actions.parquet", index=False)
        smoke = _smoke(root, snapshot, audit, config)
        features = _cache_features(root, snapshot, audit, config)
        maturity = pd.DataFrame({"as_of_trade_date": features["mature_signal_dates"], "label_status": "OK"})
        split_plan, assignments = fixed_split_manifest(maturity, audit.sessions, config)
        assignments.to_csv(root / "split.csv", index=False)
        if split_plan["status"] == "OK":
            rows = _training_rows(features, split_plan["boundaries"]["train"], split_plan["boundaries"]["calibration"])
            model = fit_common_bins(rows, split_plan["boundaries"]["calibration"])
            write_json(root / "train_fit_audit.json", {"status": "TRAIN_ONLY", "rows": model.global_records,
                "dates": model.global_dates, "latest_label_end_date": rows.label_end_date.max() if not rows.empty else None,
                "boundary_exclusive": split_plan["boundaries"]["calibration"], "test_returns_read_for_fitting": False})
        else:
            empty = pd.DataFrame(columns=["entity_type", "horizon", "as_of_trade_date", "formula_score", "realized_return"])
            model = fit_return_bins(empty, entity_type="stock", horizon=config["strategies"]["A0_V2_F0"]["primary_horizon"])
        model_path = root / "common_return_bins.json"
        model_sha = save_common_bins(model_path, model)
        manifest = {"status": split_plan["status"], "snapshot": snapshot,
            "feature_identity": features["feature_identity"], "feature_manifest_sha256": file_sha256(root / "feature_manifest.json"),
            "configuration_sha256": sha256_json(config), "as_of": audit.audit["as_of"],
            "roster_count": len(audit.roster), "session_count": len(audit.sessions), "split_plan": split_plan,
            "split_sha256": file_sha256(root / "split.csv"), "scope_flags": audit.audit["scope_flags"],
            "common_return_bins": {"path": str(model_path), "sha256": model_sha}, "smoke": smoke,
            "api_calls": 0, "source_access": "READ_ONLY_SQLITE_ONLINE_BACKUP"}
        write_json(path, manifest)
        _progress(root, "prepare_complete", status=manifest["status"], mature_signal_dates=split_plan["mature_signal_date_count"])
        return manifest


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
        excluded_board_codes = roster.loc[~roster.code.map(is_buyable_mainboard_ts_code), "code"].tolist()
        roster = roster.loc[roster.code.map(is_buyable_mainboard_ts_code)].reset_index(drop=True)
        roster["instrument_type"] = roster.instrument_type.astype("string").str.strip().str.lower()
        roster["metadata_status"] = np.where(roster.instrument_type.eq("stock").fillna(False) & roster.list_date.notna(), "AVAILABLE_LIST_DATE_HISTORY_LIMITED", "INSTRUMENT_METADATA_UNKNOWN")
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
        "universe": config["data"]["universe"], "excluded_board_codes": excluded_board_codes,
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
