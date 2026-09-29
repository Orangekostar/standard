from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from core.backtest.wick_exit_research import WickPolicy,wick_features,replay_wick
from core.backtest.stability_research_v2 import stability_windows,summarize_stability
from core.backtest.strategy_search_v2 import SearchMarket,enrich_features
from core.pipeline.stability_research import ENVIRONMENT_COLUMNS,_verified_source
from core.pipeline.prism_compare_config import file_sha256,implementation_identity,write_json
from core.pipeline.prism_compare_data import experiment_lock
from core.pipeline.rank_rotation_research import load_rotation_config
from core.pipeline.strategy_search_research import _artifact_records
from core.technical_v2.contracts import ContractError,sha256_json


def load_wick_market(root,manifest,features,start,end):
    names=pd.read_parquet(root/'roster.parquet')[['code','name']]
    columns=list(dict.fromkeys([*ENVIRONMENT_COLUMNS,'comparison_high','comparison_low']))
    pieces=[]
    for record in features['feature_chunks']:
        f=pd.read_parquet(record['path'],columns=columns,filters=[('date','<=',end)])
        f=wick_features(enrich_features(f))
        pieces.append(f.loc[f.date.ge(start)&f.date.le(end)])
    panel=pd.concat(pieces,ignore_index=True).merge(names,on='code',how='left',validate='many_to_one')
    frames={str(d):f for d,f in panel.groupby('date',sort=True)}
    expected=[d for d in features['sessions'] if start<=d<=end]
    if list(frames)!=expected or any(len(f)!=manifest['roster_count'] for f in frames.values()):
        raise ContractError('wick replay feature/calendar coverage differs')
    events=panel.loc[panel.high_wick,['date','code','comparison_open','comparison_high','comparison_low',
        'comparison_close','wick_prior_high20','wick_high_relative20','wick_fraction','wick_intraday_rise']]
    return SearchMarket(frames),events


def run_wick_exit(experiment_root,output_root):
    root,output=Path(experiment_root).resolve(),Path(output_root).resolve()
    if root==output or root in output.parents or output in root.parents:
        raise ContractError('wick output must be isolated from source')
    config=load_rotation_config();manifest,features=_verified_source(root,config)
    policies=[WickPolicy(mode=m) for m in ('FIXED','NO_TP','WICK')]
    windows=stability_windows(features['sessions'],manifest['windows']['test'])
    protocol=dict(schema_version='wick-exit-research.v1',high_lookback=20,high_relative_floor=.98,
        intraday_high_over_previous_close_minimum=1.03,upper_wick_over_range_minimum=.5,
        upper_wick_over_body_minimum=2,ohlc='COMPARISON_ADJUSTED_REAL_FINITE_VALID',
        missing_shape='NO_WICK_SIGNAL_BASE_STOP_EXPIRY_STILL_ACTIVE',
        signal='AFTER_CLOSE_NEXT_EXECUTABLE_OPEN_NEVER_INTRADAY_HIGH_FILL',
        stop_loss=.03,maximum_holding_sessions=5,tail_sessions=11,
        unchanged='ENTRY_RANKING_MARKET_SECTOR_GATES_CAPACITY_CASH_COSTS_EXECUTION',
        policies=[dict(policy_id=p.policy_id,**asdict(p)) for p in policies],
        selection='NONE_THREE_FIXED_COMPARISONS_NO_THRESHOLD_SEARCH',
        reused_history=True,production_activation=False,api_calls=0)
    identity=implementation_identity()
    binding=dict(dataset_sha256=file_sha256(root/'rank_dataset_manifest.json'),
        configuration_sha256=sha256_json(dict(config=config,protocol=protocol)),
        implementation_hash=identity['implementation_hash'])
    with experiment_lock(output):
        result_path=output/'run_manifest.json'
        if result_path.exists():
            archive=json.loads(result_path.read_text())
            if archive['status']!='COMPLETE' or archive['binding']!=binding or archive['files']!=_artifact_records(output):
                raise ContractError('wick archive changed; preserve it and use a new output')
            return {**archive['result'],'reused':True}
        if any(p.name!='.compare.lock' for p in output.iterdir()):
            raise ContractError('incomplete wick output is preserved; use a new directory')
        write_json(output/'frozen_protocol.json',dict(binding=binding,protocol=protocol,
            base_parameters=config,windows=windows,source_identity=identity),immutable=True)
        actions=pd.read_parquet(root/'corporate_actions.parquet')
        scope=sorted(set(manifest['scope_flags'])|{'REUSED_HOLDOUT','FIXED_WICK_EXIT_COMPARISON'})
        cells=[]
        for phase,first,last in [('selection','selection_1','selection_6'),('holdout','holdout_1','holdout_2')]:
            market,events=load_wick_market(root,manifest,features,windows[first][0],windows[last][-1])
            events.to_csv(output/f'wick_events_{phase}.csv.gz',index=False,compression='gzip')
            for split,dates in windows.items():
                if not split.startswith(phase): continue
                for p in policies:
                    for cost in ('base','stress'):
                        r=replay_wick(market,policy=p,sessions=dates,config=config,cost_scenario=cost,
                            output_dir=output/phase/p.policy_id/split/cost,run_id='wick-exit',split=split,
                            scope_flags=scope,corporate_actions=actions)
                        m=r['metrics'];cells.append(m)
                        print(json.dumps(dict(event='wick_exit_cell_complete',policy_id=p.policy_id,
                            split=split,cost=cost,net_return=m['net_return'],closed=m['closed_trade_count'],
                            win_rate=m['closed_trade_win_rate'],wick_exits=m['high_wick_exit_count'])),flush=True)
            del market,events
        summaries=[summarize_stability(p,[r for r in cells if r['strategy_id']==p.policy_id
            and r['split'].startswith('selection')]) for p in policies]
        write_json(output/'cells.json',cells,immutable=True)
        result=dict(status='COMPLETE',verdict='FIXED_EXIT_COMPARISON_RESEARCH_ONLY',cells=cells,
            summaries=summaries,output_root=str(output),reused=False,api_calls=0)
        _report(output,result,windows)
        write_json(result_path,dict(status='COMPLETE',binding=binding,files=_artifact_records(output),result=result),immutable=True)
        return result


def _report(output,result,windows):
    pct=lambda v:'N/A' if v is None else f'{100*v:.2f}%'
    lines=['# 取消固定止盈，改为高位长上影线退出','',
        'FIXED：原4%收盘止盈；NO_TP：取消固定止盈；WICK：取消固定止盈，增加高位插针退出。',
        '三组保留原入场、3%收盘止损、最多持有5交易日、最多3只和仓位规则，不叠加板块缩量禁买。',
        '插针：当日最高价≥此前20个交易日最高价的98%，且≥昨收的103%；上影线≥全天高低振幅的50%，且≥实体的2倍。',
        '红绿K线均可；无需盈利才卖。需要有效的当日OHLC和20日前序高点；缺数时不判插针，但止损和到期退出照常执行。',
        '收盘确认，下一可成交开盘成交。绝不假设在上影线顶点成交。已有待卖订单优先保留，其后风险/止损优先于插针，插针优先于新到期。','',
        '| 窗口 | 策略 | 成本 | 净收益 | 最大回撤 | 平仓数 | 胜率 | 插针退出数 |',
        '|---|---|---|---|---|---|---|---|']
    for m in sorted(result['cells'],key=lambda r:(r['split']!='holdout_2',r['split'],r['cost_scenario'],r['strategy_id'])):
        lines.append(f"| {m['split']} | {m['strategy_id']} | {m['cost_scenario']} | {pct(m['net_return'])} | {pct(m['max_drawdown'])} | {m['closed_trade_count']} | {pct(m['closed_trade_win_rate'])} | {m['high_wick_exit_count']} |")
    lines+=['','## 前六窗口汇总','',
        '| 策略 | 成本 | 平均窗口净收益 | 盈利窗口 | 平仓数 | 合并胜率 | 既有稳定性门槛 |',
        '|---|---|---|---|---|---|---|']
    for s in result['summaries']:
        for cost,a in s['cost_aggregates'].items():
            lines.append(f"| {s['policy_id']} | {cost} | {pct(a['mean_window_return'])} | {a['profitable_windows']}/6 | {a['closed_trade_count']} | {pct(a['win_rate'])} | {s['qualified']} |")
    lines+=['','## 日期与边界','']+[f'- {k}: {v[0]}—{v[-1]}' for k,v in windows.items()]
    lines+=['','每窗口独立100万元，最后11交易日不新买；平均窗口收益不是连续复利。费用、涨跌停、T+1、资金及滑点规则沿用基准。',
        '重复使用历史，非新样本外；历史ST、板块成分与公司行动资料仍有限。本实验没有取消5日到期卖出，不代表无限期等待插针。',
        '固定一次定义，无后验阈值搜索；未改生产，也未调用JEV。']
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
