from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="package_prism_v2")
    commands = parser.add_subparsers(dest="command", required=True)
    pack = commands.add_parser("pack")
    pack.add_argument("--artifact-dir", type=Path, required=True)
    pack.add_argument("--output-dir", type=Path, required=True)
    pack.add_argument("--part-mib", type=int)
    unpack = commands.add_parser("restore")
    unpack.add_argument("--manifest", type=Path, required=True)
    unpack.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    from core.pipeline.prism_compare_delivery import package, restore

    try:
        result = package(args.artifact_dir, args.output_dir, part_mib=args.part_mib) if args.command == "pack" else restore(args.manifest, args.output_dir)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
