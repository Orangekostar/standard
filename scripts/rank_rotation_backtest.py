from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated three-stock forecast rank rotation research replay")
    parser.add_argument("stage", choices=("prepare", "run", "all"))
    parser.add_argument("--source-experiment", type=Path)
    parser.add_argument("--experiment-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    if args.stage in {"prepare", "all"} and args.source_experiment is None:
        parser.error("prepare/all requires --source-experiment")
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    from core.pipeline.rank_rotation_research import DEFAULT_CONFIG, load_rotation_config, prepare_rotation, run_rotation
    from core.technical_v2.contracts import json_safe

    try:
        config = load_rotation_config(args.config or DEFAULT_CONFIG)
        if args.stage in {"prepare", "all"}:
            prepare_rotation(args.source_experiment, args.experiment_root, config)
        if args.stage in {"run", "all"}:
            output = args.output_root or args.experiment_root / "results"
            result = run_rotation(args.experiment_root, config, output)
            print(json.dumps(json_safe(result), ensure_ascii=True, allow_nan=False), flush=True)
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}, ensure_ascii=True), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
