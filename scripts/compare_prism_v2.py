from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    for flag in ("source-db", "experiment-root", "config", "frozen-manifest"):
        common.add_argument(f"--{flag}", type=Path, default=argparse.SUPPRESS)
    result = argparse.ArgumentParser(prog="compare_prism_v2", parents=[common])
    result.add_argument("--all", action="store_true", help="prepare, validate, freeze, test, report in order")
    commands = result.add_subparsers(dest="command")
    for command in ("prepare", "validate", "freeze", "test", "report"):
        commands.add_parser(command, parents=[common])
    return result


def main(argv: list[str] | None = None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    if bool(args.command) == bool(args.all):
        cli.error("choose one stage or --all")
    root = getattr(args, "experiment_root", None)
    if root is None:
        cli.error("--experiment-root is required")
    if (args.all or args.command == "prepare") and not getattr(args, "source_db", None):
        cli.error("prepare/--all requires --source-db")
    if args.command == "test" and not getattr(args, "frozen_manifest", None):
        cli.error("test requires --frozen-manifest")
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    from core.pipeline import prism_comparison as comparison
    from core.pipeline.prism_compare_config import DEFAULT_CONFIG, load_config
    from core.pipeline.prism_compare_data import prepare
    from core.technical_v2.contracts import json_safe

    config_path = getattr(args, "config", DEFAULT_CONFIG)
    stages = ("prepare", "validate", "freeze", "test", "report") if args.all else (args.command,)
    frozen_path = getattr(args, "frozen_manifest", None)
    current_stage = None
    try:
        config = load_config(config_path)
        comparison.configure_resources(config)
        for current_stage in stages:
            if current_stage == "prepare":
                result = prepare(args.source_db, root, config)
                result = {key: result[key] for key in ("status", "as_of", "feature_identity", "split_plan", "api_calls")}
            elif current_stage == "validate":
                result = comparison.validate(root, config_path)
            elif current_stage == "freeze":
                frozen_path = comparison.freeze(root, config_path)
                result = {"status": "FROZEN", "protocol_path": str(frozen_path)}
            elif current_stage == "test":
                result = comparison.test(root, frozen_path)
            else:
                result = comparison.report(root)
            payload = {key: value for key, value in result.items()
                       if key not in ("source_identity", "binding", "cells")}
            if "cells" in result:
                payload.update(cell_count=len(result["cells"]),
                               reused_cell_count=sum(cell["reused"] for cell in result["cells"]))
            print(json.dumps(json_safe({"stage": current_stage, **payload}), ensure_ascii=True, allow_nan=False), flush=True)
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "FAILED", "stage": current_stage, "error": str(exc)}, ensure_ascii=True), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
