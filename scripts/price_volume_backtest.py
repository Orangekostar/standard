import argparse
import json
import os
from pathlib import Path


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--experiment-root',type=Path,required=True)
    parser.add_argument('--output-root',type=Path,required=True)
    args=parser.parse_args()
    for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
        os.environ[key]='1'
    from core.pipeline.price_volume_research import run_price_volume
    result=run_price_volume(args.experiment_root,args.output_root)
    print(json.dumps({k:result[k] for k in ('status','verdict','selection','holdout_rejections','output_root','reused')}))
