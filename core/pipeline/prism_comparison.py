from __future__ import annotations

import json
import os
import shutil
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa

from core.backtest.prism_compare_engine import replay_cell
from core.backtest.prism_compare_metrics import exposure_control
from core.pipeline.prism_compare_config import (
    DEFAULT_CONFIG, REPOSITORY_ROOT, STRATEGIES, file_sha256, implementation_identity, load_config, write_json,
)
from core.pipeline.prism_compare_data import experiment_lock
from core.technical_v2.contracts import ContractError, sha256_json


def configure_resources(config: dict[str, Any]) -> None:
    if not 1 <= config["resources"]["parallel_replays"] <= 2 or not 1 <= config["resources"]["max_cpu_threads"] <= 8:
        raise ContractError("fixed comparison allows at most two replays and eight CPU threads")
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)


def _binding(root: Path, config: dict[str, Any], identity: dict[str, Any]) -> tuple[dict, dict]:
    dataset = json.loads((root / "dataset_manifest.json").read_text())
    if dataset["configuration_sha256"] != sha256_json(config):
        raise ContractError("dataset and comparison configuration differ; prepare a matching namespace")
    if sha256_json(json.loads((root / "parameters.json").read_text())) != sha256_json(config):
        raise ContractError("prepared parameters differ from the comparison configuration")
    for name, expected in (("feature_manifest.json", dataset["feature_manifest_sha256"]),
                           ("split.csv", dataset["split_sha256"])):
        if file_sha256(root / name) != expected:
            raise ContractError(f"prepared {name} SHA256 differs")
    bins = dataset["common_return_bins"]
    if file_sha256(bins["path"]) != bins["sha256"]:
        raise ContractError("common train-return-bin SHA256 differs")
    return {"dataset_sha256": file_sha256(root / "dataset_manifest.json"),
            "configuration_sha256": sha256_json(config), "implementation_hash": identity["implementation_hash"],
            "feature_identity": dataset["feature_identity"], "split_sha256": dataset["split_sha256"],
            "common_return_bins_sha256": bins["sha256"], "data_audit_sha256": file_sha256(root / "data_audit.json")}, dataset


def _copy_bound(source: Path, destination: Path) -> None:
    if destination.exists():
        if file_sha256(source) != file_sha256(destination):
            raise ContractError(f"immutable comparison input changed: {destination}")
    else:
        shutil.copyfile(source, destination)


def _initialise(root: Path, config: dict[str, Any]) -> tuple[dict, dict]:
    identity = implementation_identity()
    binding, dataset = _binding(root, config, identity)
    run_id = f"prism-v2-{dataset['as_of']}-{sha256_json(binding)[:16]}"
    directory = (REPOSITORY_ROOT / config["delivery"]["artifact_root"] / run_id).resolve()
    if directory.is_relative_to(REPOSITORY_ROOT / "artifacts" / "technical_v2"):
        raise ContractError("comparison output must not target production publication")
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("dataset_manifest.json", "data_audit.json", "split.csv", "parameters.json"):
        _copy_bound(root / name, directory / name)
    _copy_bound(REPOSITORY_ROOT / "docs/prism_v2_compare/SOURCE_AUDIT.md", directory / "shared_fixes.md")
    audit = json.loads((root / "data_audit.json").read_text())
    write_json(directory / "baseline_identity.json", {
        "A0": "F0_ORIGINAL_SIGNAL_AND_EXPIRY_WITH_SYMMETRIC_SHARED_EXECUTION_FIXES",
        "A_NATIVE_REFERENCE": "PROVENANCE_ONLY_NOT_A_DIFFERENT_DATE_NUMERICAL_COMPARATOR",
        "native_commit": config["source"]["commit"], "native_reference_round_trip_cost": .00212,
        "native_price_triggered_exit": False, "main_reference_cost_base": config["costs"]["reference_round_trip_cost_base"],
        "old_portfolio_test_executed": audit["old_portfolio_test_executed"],
        "old_test_summary_exposure": audit["old_test_summary_exposure"],
        "source_audit": "shared_fixes.md", "origin": config["origin"]}, immutable=True)
    path = directory / "run_identity.json"
    record = {"run_id": run_id, "artifact_dir": str(directory), "binding": binding,
              "source_identity": identity, "dataset_status": dataset["status"]}
    if path.exists():
        record = json.loads(path.read_text())
        if record["binding"] != binding:
            raise ContractError("existing run identity has a different binding")
    else:
        write_json(path, record, immutable=True)
    write_json(root / "comparison_run.json", record)
    return record, dataset


def _job(root, config, run, strategy, split, cost, lambda_c):
    configure_resources(config)
    directory = Path(run["artifact_dir"])
    output = directory / strategy / split / cost
    if output.exists() and any(output.iterdir()) and not (output / "cell_manifest.json").exists():
        recovery = directory / "failed_attempts" / f"{strategy}-{split}-{cost}-{uuid.uuid4().hex[:8]}"
        recovery.parent.mkdir(exist_ok=True)
        output.rename(recovery)
        write_json(recovery / "attempt_failure.json", {"status": "INCOMPLETE_ATTEMPT_PRESERVED",
                   "restored_target": str(output), "recovery_path": str(recovery)}, immutable=True)
    result = replay_cell(root, config=config, strategy_id=strategy, split=split, cost_scenario=cost,
                         lambda_c=lambda_c, output_dir=output, run_id=run["run_id"])
    return {"strategy_id": strategy, "split": split, "cost_scenario": cost, "path": str(output),
            "metrics": result["metrics"], "reused": result["reused"],
            "cell_manifest_sha256": file_sha256(output / "cell_manifest.json")}


def _cells(root, config, run, matrix, lambda_c):
    if len(matrix) != len(set(matrix)):
        raise ContractError("comparison matrix contains duplicate cells")
    workers = min(config["resources"]["parallel_replays"], len(matrix))
    if workers <= 1:
        return [_job(root, config, run, *cell, lambda_c) for cell in matrix]
    completed = []
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_job, root, config, run, *cell, lambda_c) for cell in matrix]
        for future in as_completed(futures):
            cell = future.result()
            completed.append(cell)
            print(json.dumps({"event": "cell_complete", "strategy_id": cell["strategy_id"],
                  "split": cell["split"], "cost_scenario": cell["cost_scenario"], "reused": cell["reused"]}), flush=True)
    return sorted(completed, key=lambda item: (item["strategy_id"], item["split"], item["cost_scenario"]))


def validate(experiment_root: str | Path, config_path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    root, config = Path(experiment_root).resolve(), load_config(config_path)
    configure_resources(config)
    with experiment_lock(root):
        run, dataset = _initialise(root, config)
        if dataset["status"] != "OK":
            write_json(Path(run["artifact_dir"]) / "validation_complete.json", {
                "status": f"NOT_RUN_{dataset['status']}", "binding": run["binding"],
                "cell_manifests": {}, "reason": dataset["split_plan"]}, immutable=True)
            return {**run, "status": dataset["status"], "cells": [], "reason": dataset["split_plan"]}
        matrix = [(strategy, "validation", "base") for strategy in STRATEGIES[:2]]
        cells = _cells(root, config, run, matrix, 1.)
        receipt = {"status": "COMPLETE", "binding": run["binding"],
                   "cell_manifests": {cell["strategy_id"]: cell["cell_manifest_sha256"] for cell in cells}}
        write_json(Path(run["artifact_dir"]) / "validation_complete.json", receipt, immutable=True)
        return {**run, "status": "COMPLETE", "cells": cells}


def freeze(experiment_root: str | Path, config_path: str | Path = DEFAULT_CONFIG) -> Path:
    root, config = Path(experiment_root).resolve(), load_config(config_path)
    configure_resources(config)
    with experiment_lock(root):
        run, dataset = _initialise(root, config)
        identity = implementation_identity()
        if not identity["source_tree_clean"]:
            raise ContractError("commit the comparison code/configuration before freezing and opening test")
        directory = Path(run["artifact_dir"])
        validation_path = directory / "validation_complete.json"
        if not validation_path.exists():
            raise ContractError("run validate before freeze; no completed A/B validation exists")
        validation = json.loads(validation_path.read_text())
        if validation["binding"] != run["binding"]:
            raise ContractError("validation implementation/data binding differs")
        if dataset["status"] == "OK":
            if validation["status"] != "COMPLETE":
                raise ContractError("eligible freeze requires completed validation")
            matrix = [(strategy, "validation", "base") for strategy in STRATEGIES[:2]]
            for strategy, split, cost in matrix:
                receipt = directory / strategy / split / cost / "cell_manifest.json"
                if not receipt.exists() or file_sha256(receipt) != validation["cell_manifests"][strategy]:
                    raise ContractError("validated cell manifest SHA256 differs")
            cells = _cells(root, config, run, matrix, 1.)
            if not all(cell["reused"] for cell in cells):
                raise ContractError("freeze must reuse completed validation cells")
            control = exposure_control(cells[0]["metrics"]["average_gross_exposure"],
                                       cells[1]["metrics"]["average_gross_exposure"], config)
        else:
            control = {"lambda_c": None, "status": "UNAVAILABLE_NO_ELIGIBLE_VALIDATION",
                "a_validation_mean_exposure": None, "b_validation_mean_exposure": None,
                "test_returns_used": False, "stress_refits_lambda": False}
        path = directory / "protocol_frozen.json"
        if path.exists():
            receipt = json.loads((root / "freeze_receipt.json").read_text())
            if file_sha256(path) != receipt["protocol_sha256"]:
                raise ContractError("frozen protocol SHA256 differs")
            return path
        frozen = {"status": "FROZEN" if dataset["status"] == "OK" else "FROZEN_INELIGIBLE_HISTORY",
            "run_id": run["run_id"], "artifact_dir": str(directory),
            "binding": run["binding"], "source_identity": identity, "parameters": config,
            "exposure_control": control, "split_plan": dataset["split_plan"],
            "scope_flags": dataset["scope_flags"], "frozen_at": datetime.now(timezone.utc).isoformat(),
            "test_opened_before_freeze": False, "threshold_search_trials": 0,
            "verdict_rules": config["evaluation"], "api_calls": 0}
        protocol_sha = write_json(path, frozen, immutable=True)
        write_json(root / "freeze_receipt.json", {"run_id": run["run_id"], "protocol_path": str(path),
                   "protocol_sha256": protocol_sha, "binding": run["binding"]})
        return path


def test(experiment_root: str | Path, frozen_manifest: str | Path) -> dict[str, Any]:
    root, path = Path(experiment_root).resolve(), Path(frozen_manifest).resolve()
    if not path.exists() or not (root / "freeze_receipt.json").exists():
        raise ContractError("test requires an existing hash-bound frozen manifest; run freeze first")
    with experiment_lock(root):
        receipt = json.loads((root / "freeze_receipt.json").read_text())
        if file_sha256(path) != receipt["protocol_sha256"]:
            raise ContractError("frozen manifest SHA256 differs")
        frozen = json.loads(path.read_text())
        config = load_config(Path(frozen["artifact_dir"]) / "parameters.json")
        configure_resources(config)
        identity = implementation_identity()
        binding, dataset = _binding(root, config, identity)
        if binding != frozen["binding"] or binding != receipt["binding"]:
            raise ContractError("frozen implementation/data/configuration binding differs; invalidate all affected cells")
        if not identity["source_tree_clean"]:
            raise ContractError("test requires committed comparison source/configuration")
        run = {key: frozen[key] for key in ("run_id", "artifact_dir", "binding", "source_identity")}
        directory = Path(run["artifact_dir"])
        if frozen["status"] == "FROZEN_INELIGIBLE_HISTORY" and dataset["status"] != "OK":
            complete = {"status": f"NOT_RUN_{dataset['status']}", "binding": binding,
                "protocol_sha256": receipt["protocol_sha256"], "cell_count": 0,
                "cell_manifests": {}, "reason": dataset["split_plan"]}
            write_json(directory / "test_complete.json", complete, immutable=True)
            write_json(root / "test_complete.json", complete)
            return {**run, "status": complete["status"], "cells": []}
        if frozen["status"] != "FROZEN" or dataset["status"] != "OK":
            raise ContractError("test has no eligible frozen fixed split")
        opened = directory / "test_started.json"
        if not opened.exists():
            write_json(opened, {"protocol_sha256": receipt["protocol_sha256"],
                       "opened_at": datetime.now(timezone.utc).isoformat(), "source_commit": identity["source_commit"]}, immutable=True)
        matrix = [(strategy, split, cost) for strategy in STRATEGIES
                  for split in config["evaluation"]["splits"] for cost in config["evaluation"]["cost_cases"]]
        if len(matrix) != config["evaluation"]["main_replay_cells"]:
            raise ContractError("frozen comparison matrix does not have the required twelve cells")
        cells = _cells(root, config, run, matrix, frozen["exposure_control"]["lambda_c"])
        complete = {"status": "COMPLETE", "binding": binding, "protocol_sha256": receipt["protocol_sha256"],
                    "cell_count": len(cells), "cell_manifests": {
                        f"{cell['strategy_id']}/{cell['split']}/{cell['cost_scenario']}": cell["cell_manifest_sha256"] for cell in cells}}
        write_json(directory / "test_complete.json", complete, immutable=True)
        write_json(root / "test_complete.json", complete)
        return {**run, "status": "COMPLETE", "cells": cells}


def report(experiment_root: str | Path) -> dict[str, Any]:
    from core.pipeline.prism_compare_report import report as build_report
    return build_report(experiment_root)
