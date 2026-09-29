from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Frozen 36-candidate, three-stock research strategy search")
    parser.add_argument("--experiment-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    from core.pipeline.strategy_search_research import DEFAULT_CONFIG, load_search_config, run_search
    result = run_search(args.experiment_root, load_search_config(args.config or DEFAULT_CONFIG), args.output_root)
    print(json.dumps({key:result[key] for key in ("status", "verdict", "selection", "holdout_candidate", "holdout_rejections", "output_root", "reused")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
