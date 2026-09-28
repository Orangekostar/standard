from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from core.factors.technical_v2 import DIRECTIONAL_FACTOR_IDS, RISK_INDICATOR_IDS
from core.pipeline.technical_v2 import FIXED_SPLIT_COUNTS, WARMUP_SESSIONS
from core.strategies.formula_v2 import BASE_WEIGHTS, FACTOR_GROUPS, SCORE_BIN_EDGES
from core.technical_v2.contracts import ContractError, canonical_json, json_safe, sha256_json

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPOSITORY_ROOT / "configs/prism_v2_compare_v1.json"
STRATEGIES = ("A0_V2_F0", "B0_PRISM_A_SHARE_V1", "C0_V2_EXPOSURE_CONTROL")


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    config = json.loads(Path(path).read_text())
    native = config["native_contract"]
    expected = {
        "factor_ids": list(DIRECTIONAL_FACTOR_IDS), "risk_ids": list(RISK_INDICATOR_IDS),
        "factor_groups": {name: list(ids) for name, ids in FACTOR_GROUPS.items()},
        "f0_weights": {str(h): weights for h, weights in BASE_WEIGHTS.items()},
        "return_bin_edges": list(SCORE_BIN_EDGES), "horizons": [1, 3, 5],
        "minimum_valid_factors": 12, "nonempty_factor_groups_required": True,
        "score_up_at_least": 60, "score_down_at_most": 40,
        "entry_score5_at_least": 65, "entry_score3_at_least": 55,
        "hold_score5_at_least": 45, "return_global_min_records": 1000,
        "return_global_min_dates": 30, "return_bin_min_records": 100,
        "return_bin_min_dates": 20, "return_shrinkage_records": 100,
        "target_delta_stock_floor": .005, "target_delta_sigma_multiple": .25,
    }
    if native != expected:
        raise ContractError("configuration must preserve the original F0/intent/label/return-bin contract")
    if config["split"]["counts"] != FIXED_SPLIT_COUNTS or config["data"]["warmup_sessions"] != WARMUP_SESSIONS:
        raise ContractError("fixed split or warmup does not match the V2 contract")
    if any(config["network"][key] for key in ("paid_calls", "jev_in_primary_comparison", "auto_full_market_resync")):
        raise ContractError("comparison does not permit network inference or market resynchronization")
    if config["evaluation"]["threshold_search_trials"] != 0:
        raise ContractError("the fixed comparison does not permit threshold search")
    if config["shared_portfolio"]["net_edge_min_exclusive"] != .001:
        raise ContractError("preserve the original exclusive net-edge gate")
    if config["strategies"][STRATEGIES[1]]["online_threshold_evolution"]:
        raise ContractError("online threshold evolution is outside this experiment")
    if config["origin"] != "RESEARCH_ADAPTATION_NOT_PRISM_OFFICIAL_A_SHARE_STRATEGY":
        raise ContractError("strategy provenance must remain an explicit A-share adaptation")
    for scenario in config["evaluation"]["cost_cases"]:
        stated = config["costs"][f"reference_round_trip_cost_{scenario}"]
        if abs(stated - reference_cost(config, scenario)) > 1e-12:
            raise ContractError("reference costs must match the actual fee and slippage assumptions")
    return config


def reference_cost(config: dict[str, Any], scenario: str) -> float:
    fees = config["costs"]
    return (2 * fees["commission_each_side"] + fees["sell_addon_rate"]
            + 2 * fees["other_rate_each_side"] + 2 * fees["slippage_cases"][scenario])


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def implementation_identity() -> dict[str, Any]:
    files = sorted(path for folder in ("core", "scripts")
                   for path in (REPOSITORY_ROOT / folder).rglob("*.py"))
    hashes = {str(path.relative_to(REPOSITORY_ROOT)): file_sha256(path) for path in files}
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY_ROOT, text=True).strip()
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--", "core", "scripts", "configs"],
        cwd=REPOSITORY_ROOT, text=True,
    ).strip()
    return {"source_commit": commit, "implementation_hash": sha256_json(hashes),
            "source_files": hashes, "source_tree_clean": not dirty}


def write_json(path: str | Path, value: Any, *, immutable: bool = False) -> str:
    path = Path(path)
    content = canonical_json(json_safe(value)) + "\n"
    if immutable and path.exists() and path.read_text() != content:
        raise ContractError(f"immutable experiment artifact differs: {path}")
    if not path.exists() or path.read_text() != content:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content)
        temporary.replace(path)
    return hashlib.sha256(content.encode()).hexdigest()
