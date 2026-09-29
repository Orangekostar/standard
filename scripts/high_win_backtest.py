"""Offline high-win laboratory CLI; no production or provider calls."""
import argparse
import json
import os
from pathlib import Path


def parser():
    p=argparse.ArgumentParser(description='沪深主板高胜率策略实验室：固定18策略、252账户')
    p.add_argument('stage',nargs='?',choices=('prepare','screen','freeze','review','report','all'))
    p.add_argument('--all',action='store_true',dest='all_stages')
    p.add_argument('--experiment-root',type=Path,required=True)
    p.add_argument('--output-root',type=Path,required=True)
    p.add_argument('--config',type=Path,default=Path('configs/high_win_suite_v1.json'))
    p.add_argument('--workers',type=int,choices=(1,4),default=4)
    p.add_argument('--smoke',action='store_true',help='first 30 stocks, at most 160 sessions; never used for selection')
    return p


def main():
    p=parser();a=p.parse_args()
    if a.all_stages and a.stage not in (None,'all'): p.error('choose a stage or --all, not both')
    stage='all' if a.all_stages else a.stage
    if stage is None: p.error('a stage or --all is required')
    if a.smoke and stage not in ('prepare','screen','all'): p.error('smoke only supports prepare/screen/all')
    for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
        os.environ[key]='1'
    from core.pipeline.high_win_research import run_lab
    result=run_lab(stage,a.experiment_root,a.output_root,a.config,a.workers,a.smoke)
    print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__': main()
