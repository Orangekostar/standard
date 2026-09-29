from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fixed environment gates and quarterly return stability research")
    parser.add_argument("--experiment-root",type=Path,required=True)
    parser.add_argument("--output-root",type=Path,required=True)
    args = parser.parse_args(argv)
    for name in ("OMP_NUM_THREADS","OPENBLAS_NUM_THREADS","MKL_NUM_THREADS","NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    from core.pipeline.stability_research import run_stability
    result = run_stability(args.experiment_root,args.output_root)
    print(json.dumps({key:result[key] for key in ("status","verdict","selection","holdout_policy",
        "holdout_rejections","output_root","reused")},ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
