import argparse
import json
import os
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description='Fixed factor-sector portfolio return/drawdown search')
    parser.add_argument('--experiment-root',type=Path,required=True)
    parser.add_argument('--output-root',type=Path,required=True)
    parser.add_argument('--min-price',type=float,required=True)
    parser.add_argument('--workers',type=int,choices=(1,4),default=4)
    args=parser.parse_args()
    for name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
        os.environ[name]='1'
    from core.pipeline.factor_portfolio_research import run_factor_portfolio
    result=run_factor_portfolio(args.experiment_root,args.output_root,min_price=args.min_price,workers=args.workers)
    print(json.dumps({k:result[k] for k in ('status','verdict','selection','rejections','output_root','reused')}))


if __name__=='__main__': main()
