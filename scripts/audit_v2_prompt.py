from __future__ import annotations

import argparse
import hashlib
import json
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import pandas as pd

from core.background.snapshot_store import read_latest_manifest
from core.background.technical_v2_tasks import TECHNICAL_V2_TASKS, TechnicalV2TaskRunner
from core.data.v2_store import REQUIRED_TABLES, SCHEMA_VERSION, V2Store
from core.pipeline.evaluation_v2 import REQUIRED_RESULT_FILES
from core.technical_v2.contracts import sha256_json

FORBIDDEN_PLACEHOLDER_MARKERS = (
    "READY_FOR_FIXED_PROTOCOL",
    '"status": "NO_MATURE_LABELS"',
)
REQUIRED_CANDIDATES = {
    "F0_BALANCED",
    "F1_TREND",
    "F2_STRUCTURE",
    "M0_MOMENTUM",
    "LEGACY_SIGNAL_EXEC_V2",
    "J0_EQUAL_POOL",
    "J1_WEIGHTED_POOL",
}


def _hash_matches(path: Path, recorded: str) -> bool:
    byte_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if recorded == byte_digest:
        return True
    if path.suffix != ".json":
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return recorded == sha256_json(payload)


def audit(
    *,
    project_root: Path,
    db_path: Path,
    artifact_root: Path,
    mode: str,
    analysis_run_id: str | None = None,
    evaluation_run_id: str | None = None,
) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}

    def record(name: str, passed: bool, **details: Any) -> None:
        checks[name] = {"status": "PASS" if passed else "FAIL", **details}

    source = (project_root / "scripts" / "v2.py").read_text(encoding="utf-8")
    found_markers = [marker for marker in FORBIDDEN_PLACEHOLDER_MARKERS if marker in source]
    record("no_placeholder_success_paths", not found_markers, found=found_markers)

    store = V2Store(db_path, data_mode=mode)
    tables: set[str] = set()
    migrations: list[int] = []
    if db_path.is_file():
        with closing(store._connect()) as conn:
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "schema_migrations" in tables:
                migrations = [
                    int(row[0])
                    for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
                ]
    record(
        "schema",
        REQUIRED_TABLES.issubset(tables) and migrations == list(range(1, SCHEMA_VERSION + 1)),
        schema_version=SCHEMA_VERSION,
        migrations=migrations,
        missing_tables=sorted(REQUIRED_TABLES.difference(tables)),
    )

    latest = read_latest_manifest(artifact_root) if artifact_root.is_dir() else None
    bound_run = str(analysis_run_id or (latest or {}).get("run_id") or "")
    record(
        "analysis_publication",
        latest is not None and bool(bound_run) and latest.get("run_id") == bound_run,
        run_id=bound_run or None,
        publication_id=(latest or {}).get("publication_id"),
    )

    coverage_rows = pd.DataFrame()
    if latest is not None and bound_run:
        coverage_hash = str(latest.get("coverage_hash") or "")
        coverage_path = artifact_root / bound_run / "coverage" / f"{coverage_hash}.json"
        try:
            coverage_payload = json.loads(coverage_path.read_text(encoding="utf-8"))
            rows = coverage_payload.get("rows")
            coverage_rows = pd.DataFrame(rows if isinstance(rows, list) else [])
            coverage_valid = isinstance(rows, list) and sha256_json(rows) == coverage_hash
        except (OSError, json.JSONDecodeError):
            coverage_valid = False
    else:
        coverage_valid = False
    entity_types = set(coverage_rows.get("entity_type", pd.Series(dtype=str)).astype(str))
    record(
        "stock_sector_coverage",
        coverage_valid and entity_types == {"stock", "sector"},
        rows=len(coverage_rows),
        entity_types=sorted(entity_types),
    )

    feature_rows = store.read_feature_rows(bound_run) if tables and bound_run else pd.DataFrame()
    prediction_rows = store.read_prediction_rows(bound_run) if tables and bound_run else pd.DataFrame()
    feature_types = set(feature_rows.get("entity_type", pd.Series(dtype=str)).astype(str))
    prediction_methods = set(prediction_rows.get("method", pd.Series(dtype=str)).astype(str))
    record(
        "durable_analysis_rows",
        feature_types == {"stock", "sector"} and prediction_methods == {"formula", "jev"},
        feature_rows=len(feature_rows),
        feature_entity_types=sorted(feature_types),
        prediction_rows=len(prediction_rows),
        prediction_methods=sorted(prediction_methods),
    )

    accounts = pd.DataFrame()
    if "paper_accounts" in tables:
        with closing(store._connect()) as conn:
            accounts = pd.read_sql_query(
                "SELECT account_id, method, account_type, initial_cash_cents FROM paper_accounts",
                conn,
            )
    account_map = dict(zip(accounts.get("account_id", []), accounts.get("method", [])))
    decisions = store.read_paper_decisions() if "paper_decisions" in tables else pd.DataFrame()
    valuations = store.read_paper_valuations() if "paper_valuations" in tables else pd.DataFrame()
    paper_ok = (
        account_map.get("formula-paper") == "formula"
        and account_map.get("jev-shadow-paper") == "jev"
        and set(accounts.get("initial_cash_cents", pd.Series(dtype=int)).astype(int)) == {100_000_000}
        and set(decisions.get("account_id", pd.Series(dtype=str)).astype(str))
        == {"formula-paper", "jev-shadow-paper"}
        and set(valuations.get("account_id", pd.Series(dtype=str)).astype(str))
        == {"formula-paper", "jev-shadow-paper"}
    )
    record(
        "paper_accounts_and_audit",
        paper_ok,
        accounts=len(accounts),
        decisions=len(decisions),
        valuations=len(valuations),
    )

    runner = TechnicalV2TaskRunner()
    record(
        "worker_handlers",
        set(runner.handlers) == set(TECHNICAL_V2_TASKS),
        handlers=sorted(runner.handlers),
    )

    evaluation_dirs = sorted(
        path for path in artifact_root.glob("evaluation-*") if path.is_dir()
    )
    evaluation_dir = (
        artifact_root / evaluation_run_id
        if evaluation_run_id
        else evaluation_dirs[-1]
        if evaluation_dirs
        else None
    )
    evaluation_details: dict[str, Any] = {"run_id": evaluation_dir.name if evaluation_dir else None}
    evaluation_ok = False
    if evaluation_dir is not None and evaluation_dir.is_dir():
        names = {path.name for path in evaluation_dir.iterdir()}
        try:
            manifest = json.loads((evaluation_dir / "run_manifest.json").read_text(encoding="utf-8"))
            recorded = manifest.get("artifact_hashes")
            hashes_ok = isinstance(recorded, dict) and all(
                name == "run_manifest.json"
                or _hash_matches(evaluation_dir / name, str(recorded.get(name) or ""))
                for name in REQUIRED_RESULT_FILES
            )
            candidates = pd.read_csv(evaluation_dir / "candidate_comparison.csv")
            candidate_ids = set(candidates.get("config_id", pd.Series(dtype=str)).astype(str))
            selection = json.loads((evaluation_dir / "selection.json").read_text(encoding="utf-8"))
            evaluation_ok = (
                names == set(REQUIRED_RESULT_FILES)
                and hashes_ok
                and REQUIRED_CANDIDATES.issubset(candidate_ids)
                and selection.get("final_test_opened") is False
                and manifest.get("final_test_opened") in {True, False}
            )
            evaluation_details.update(
                {
                    "status": manifest.get("status"),
                    "split_status": manifest.get("split_status"),
                    "selection_status": manifest.get("selection_status"),
                    "final_test_opened": manifest.get("final_test_opened"),
                    "candidate_ids": sorted(candidate_ids),
                }
            )
        except (OSError, json.JSONDecodeError, pd.errors.ParserError):
            evaluation_ok = False
    record("evaluation_package", evaluation_ok, **evaluation_details)

    failed = sorted(name for name, item in checks.items() if item["status"] != "PASS")
    return {
        "schema_version": "technical-v2-prompt-audit.v1",
        "status": "PASS" if not failed else "FAIL",
        "mode": mode,
        "db_path": str(db_path),
        "artifact_root": str(artifact_root),
        "checks": checks,
        "failed_checks": failed,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit Technical V2 prompt compliance")
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--mode", choices=("real", "demo", "test"), required=True)
    parser.add_argument("--analysis-run-id", default="")
    parser.add_argument("--evaluation-run-id", default="")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = audit(
        project_root=Path(args.project_root),
        db_path=Path(args.db_path),
        artifact_root=Path(args.artifact_root),
        mode=args.mode,
        analysis_run_id=args.analysis_run_id or None,
        evaluation_run_id=args.evaluation_run_id or None,
    )
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
    return 0 if payload["status"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
