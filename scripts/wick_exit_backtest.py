import argparse
import json
import os
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description='Compare fixed profit, no profit target and high upper wick exits')
    parser.add_argument('--experiment-root',type=Path,required=True)
    parser.add_argument('--output-root',type=Path,required=True)
    args=parser.parse_args()
    for name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
        os.environ[name]='1'
    from core.pipeline.wick_exit_research import run_wick_exit
    result=run_wick_exit(args.experiment_root,args.output_root)
    print(json.dumps({k:result[k] for k in ('status','verdict','output_root','reused')}))


if __name__=='__main__': main()
