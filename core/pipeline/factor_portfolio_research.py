from __future__ import annotations

import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from core.backtest.factor_portfolio_research import FactorPolicy,policy_grid,replay_factor,select_frontier
from core.backtest.stability_research_v2 import stability_windows,summarize_stability
from core.backtest.strategy_search_v2 import SearchMarket,enrich_features,qualification
from core.pipeline.stability_research import ENVIRONMENT_COLUMNS,_verified_source
from core.pipeline.prism_compare_config import file_sha256,implementation_identity,write_json
from core.pipeline.prism_compare_data import experiment_lock
from core.pipeline.rank_rotation_research import load_rotation_config
from core.pipeline.strategy_search_research import _artifact_records
from core.technical_v2.contracts import ContractError,sha256_json

_ACTIVE_MARKET=None
_ACTIVE_ACTIONS=None
_ACTIVE_CONFIG=None


def load_factor_market(root,manifest,features,start,end):
    names=pd.read_parquet(root/'roster.parquet')[['code','name']]
    columns=list(dict.fromkeys([*ENVIRONMENT_COLUMNS,'F04','F05','Q02']))
    pieces=[]
    for record in features['feature_chunks']:
        f=enrich_features(pd.read_parquet(record['path'],columns=columns,filters=[('date','<=',end)]))
        pieces.append(f.loc[f.date.ge(start)&f.date.le(end)])
    panel=pd.concat(pieces,ignore_index=True).merge(names,on='code',how='left',validate='many_to_one')
    frames={str(d):f for d,f in panel.groupby('date',sort=True)}
    expected=[d for d in features['sessions'] if start<=d<=end]
    if list(frames)!=expected or any(len(f)!=manifest['roster_count'] for f in frames.values()):
        raise ContractError('factor portfolio data/calendar coverage differs')
    return SearchMarket(frames)


def _run_task(job):
    return replay_factor(_ACTIVE_MARKET,config=_ACTIVE_CONFIG,corporate_actions=_ACTIVE_ACTIONS,**job)['metrics']


def _init_worker(market,actions,config):
    global _ACTIVE_MARKET,_ACTIVE_ACTIONS,_ACTIVE_CONFIG
    _ACTIVE_MARKET,_ACTIVE_ACTIONS,_ACTIVE_CONFIG=market,actions,config


def run_cells(market,actions,config,jobs,workers):
    global _ACTIVE_MARKET,_ACTIVE_ACTIONS,_ACTIVE_CONFIG
    if workers not in {1,4}: raise ContractError('use one or four isolated account workers')
    _ACTIVE_MARKET,_ACTIVE_ACTIONS,_ACTIVE_CONFIG=market,actions,config
    try:
        if workers==1: return [_run_task(job) for job in jobs]
        results=[]
        # Arrow data loading starts threads; spawn avoids inheriting their locks into account processes.
        with ProcessPoolExecutor(max_workers=workers,mp_context=multiprocessing.get_context('spawn'),
                initializer=_init_worker,initargs=(market,actions,config)) as pool:
            for i,result in enumerate(pool.map(_run_task,jobs,chunksize=1),1):
                results.append(result)
                if i%12==0 or i==len(jobs):
                    print(json.dumps(dict(event='factor_portfolio_progress',completed=i,total=len(jobs))),flush=True)
        return results
    finally:
        _ACTIVE_MARKET=_ACTIVE_ACTIONS=_ACTIVE_CONFIG=None


def run_factor_portfolio(experiment_root,output_root,*,min_price,workers=4):
    root,output=Path(experiment_root).resolve(),Path(output_root).resolve()
    if root==output or root in output.parents or output in root.parents:
        raise ContractError('factor portfolio output must be separate from inputs')
    policies=policy_grid(min_price);baseline=FactorPolicy(min_price=min_price)
    config=load_rotation_config();manifest,features=_verified_source(root,config)
    windows=stability_windows(features['sessions'],manifest['windows']['test'])
    protocol=dict(schema_version='factor-portfolio-research.v1',
        user_universe=dict(excluded_sector='SW_L1:801180.SI',minimum_unadjusted_price=min_price,
            price_checks='SIGNAL_CLOSE_AND_ACTUAL_OPEN',threshold_inclusive=True,unknown_sector='BLOCK'),
        policies=[dict(policy_id=p.policy_id,**asdict(p)) for p in policies],
        ranking_formulas=dict(SCORE='S',LEADER='.4*S+.3*R+.3*B',DEFENSIVE='.3*S+.2*R+.2*B+.3*L',
            S='formula_score3',R='50+25*(clip(F04,-1,1)+clip(F05,-1,1))',
            B='25+25*tanh(sector_log_return20/.1)+50*sector_breadth20',L='100*(1-clip(Q02/.04,0,1))'),
        volatility_sizing='REGIME_SCALED_PRIOR_NAV_THIRD_TIMES_MIN(1,.015/SIGNAL_Q02)',
        diversification='TOP_CANDIDATE_PER_SECTOR_THEN_NO_DUPLICATE_SURVIVING_SECTOR_AT_SIGNAL_AND_FILL',
        unchanged='BASE_ENTRY_4_PERCENT_TP_3_PERCENT_STOP_5_SESSION_EXPIRY_3_NAMES_COSTS_EXECUTION',
        selection_gates='EXISTING_STABILITY_5_OF_6_POSITIVE_BOTH_COSTS_DD_LE_10_WORST_GE_MINUS3_MIN60_CLOSED',
        pareto_axes='MAX_WORST_COST_MEAN_RETURN_MIN_WORST_COST_MAX_WINDOW_DRAWDOWN',
        minimum_return='HALF_BASELINE_WORST_COST_MEAN_OR_ZERO_WHICHEVER_HIGHER',
        balanced_rank='MAX_MEAN_RETURN/MAX(DRAWDOWN,.005)_THEN_LOWER_QUARTILE_RETURN_THEN_MEAN_THEN_ID',
        review='FREEZE_HIGH_RETURN_LOW_DRAWDOWN_BALANCED_BEFORE_LATER_LOADING_NO_RESELECTION',
        primary='BALANCED_ONLY_OR_DIAGNOSTIC_IF_NONE_NO_PROMOTION',
        acceptance='ALL_LATER_WINDOWS_POSITIVE_BOTH_COSTS_MIN10_CLOSED_AND_TRADED_DATES_DD_LE10_PLUS_RETURN_GT_BASELINE_DD_LE_BASELINE',
        reused_history=True,production_activation=False,api_calls=0,settlement_tail_sessions=11)
    identity=implementation_identity()
    binding=dict(dataset_sha256=file_sha256(root/'rank_dataset_manifest.json'),
        configuration_sha256=sha256_json(dict(config=config,protocol=protocol)),
        implementation_hash=identity['implementation_hash'])
    with experiment_lock(output):
        result_path=output/'run_manifest.json'
        if result_path.exists():
            old=json.loads(result_path.read_text())
            if old['status']!='COMPLETE' or old['binding']!=binding or old['files']!=_artifact_records(output):
                raise ContractError('factor portfolio archive changed; preserve and use new output')
            return {**old['result'],'reused':True}
        if any(p.name!='.compare.lock' for p in output.iterdir()):
            raise ContractError('incomplete factor portfolio output is preserved')
        write_json(output/'frozen_protocol.json',dict(binding=binding,protocol=protocol,base_parameters=config,
            windows=windows,source_identity=identity),immutable=True)
        actions=pd.read_parquet(root/'corporate_actions.parquet')
        flags=sorted(set(manifest['scope_flags'])|{'REUSED_HOLDOUT','FIXED_FACTOR_PORTFOLIO_SEARCH','NO_REAL_ESTATE','MIN_PRICE_FILTER'})
        def jobs_for(chosen,phase):
            return [dict(policy=p,sessions=dates,cost_scenario=cost,output_dir=output/phase/p.policy_id/split/cost,
                run_id='factor-portfolio',split=split,scope_flags=flags)
                for p in chosen for split,dates in windows.items() if split.startswith(phase) for cost in ('base','stress')]
        market=load_factor_market(root,manifest,features,windows['selection_1'][0],windows['selection_6'][-1])
        cells=run_cells(market,actions,config,jobs_for(policies,'selection'),workers);del market
        summaries=[summarize_stability(p,[m for m in cells if m['strategy_id']==p.policy_id]) for p in policies]
        selection=select_frontier(summaries,baseline.policy_id)
        write_json(output/'selection_cells.json',cells,immutable=True)
        write_json(output/'selection_freeze.json',dict(binding=binding,selection=selection,summaries=summaries,
            selection_cells_sha256=file_sha256(output/'selection_cells.json')),immutable=True)
        print(json.dumps(dict(event='factor_portfolio_frozen',**selection)),flush=True)
        for s in summaries:
            print(json.dumps(dict(event='factor_portfolio_selection',policy_id=s['policy_id'],qualified=s['qualified'],
                mean=s['robust_mean_return'],drawdown=s['worst_drawdown'])),flush=True)
        chosen=[p for p in policies if p.policy_id in selection['review_policy_ids']]
        market=load_factor_market(root,manifest,features,windows['holdout_1'][0],windows['holdout_2'][-1])
        reviewed=run_cells(market,actions,config,jobs_for(chosen,'holdout'),workers);del market
        primary=selection['balanced_policy_id'];rejections=[]
        if primary is None: rejections.append('NO_QUALIFIED_DISCOVERY_POLICY')
        elif primary==baseline.policy_id: rejections.append('BASELINE_REMAINS_BALANCED_CHOICE')
        else:
            targets=[m for m in reviewed if m['strategy_id']==primary]
            base=[m for m in reviewed if m['strategy_id']==baseline.policy_id]
            rejections.extend(m['split']+':'+m['cost_scenario']+':'+r for m in targets
                for r in qualification(m,min_closed=10,min_dates=10,max_drawdown=.10))
            def weak_mean(rows): return min(sum(m['net_return'] for m in rows if m['cost_scenario']==c)/2 for c in ('base','stress'))
            if any(m['net_return'] is None or m['max_drawdown'] is None for m in targets+base):
                rejections.append('INCOMPLETE_NAV')
            else:
                if weak_mean(targets)<=weak_mean(base): rejections.append('NO_LATER_RETURN_IMPROVEMENT')
                if max(m['max_drawdown'] for m in targets)>max(m['max_drawdown'] for m in base): rejections.append('LATER_DRAWDOWN_WORSE')
        verdict='RESEARCH_UPGRADE_PASSED' if not rejections else 'KEEP_RESTRICTED_BASELINE'
        result=dict(status='COMPLETE',verdict=verdict,selection=selection,summaries=summaries,
            holdout_cells=reviewed,rejections=rejections,output_root=str(output),reused=False,api_calls=0)
        write_json(output/'holdout_cells.json',reviewed,immutable=True)
        _report(output,result,windows,min_price)
        write_json(result_path,dict(status='COMPLETE',binding=binding,files=_artifact_records(output),result=result),immutable=True)
        return result


def _report(output,result,windows,min_price):
    pct=lambda v:'N/A' if v is None else f'{100*v:.2f}%'
    sel=result['selection']
    lines=['# 因子、板块与仓位：收益/回撤双目标搜索','',f"结果：{result['verdict']}",
        f'全部方案排除房地产（SW_L1:801180.SI），信号日未复权收盘与买入日原始开盘均须≥{min_price:g}元。',
        '基准也应用同样禁买条件，不能直接与此前未限制的收益混为一谈。',
        '固定12组，不搜索连续参数。SCORE为原综合分；LEADER加入相对板块/大盘强弱和板块动量/宽度；DEFENSIVE再偏向低波动。',
        'VOL15只减少新买目标仓位，系数min(1,1.5%/20日波动率)，不加杠杆、不强制重平衡。D1每板块只留最高分候选，并禁止向已有/同批计划板块新增持仓。',
        '保留4%止盈、3%止损、最多5交易日、最多3只；均按收盘信号、下一可成交开盘执行。','',
        '## 早期冻结选择','',f"高收益：{sel['high_return_policy_id']}",f"低回撤：{sel['low_drawdown_policy_id']}",
        f"折中（唯一主候选）：{sel['balanced_policy_id']}",f"仅诊断：{sel['diagnostic_policy_id']}",'',
        '按两种成本中较低的平均窗口收益、两种成本中最大的窗口回撤求Pareto前沿。先过原稳定性门槛，且收益不得低于同范围基准的一半。',
        '折中方案最大化“平均窗口收益/最大窗口回撤”，分母最低0.5%；这不是年化Calmar，也不是全市场全策略最优证明。','',
        '| 策略 | 合格 | 较弱成本平均收益 | 最大回撤 | 标准/压力盈利窗口 | 标准/压力胜率 |',
        '|---|---|---|---|---|---|']
    for s in sorted(result['summaries'],key=lambda s:-(s['robust_mean_return'] or -100)):
        b,t=s['cost_aggregates']['base'],s['cost_aggregates']['stress']
        lines.append(f"| {s['policy_id']} | {s['qualified']} | {pct(s['robust_mean_return'])} | {pct(s['worst_drawdown'])} | {b['profitable_windows']}/6 / {t['profitable_windows']}/6 | {pct(b['win_rate'])} / {pct(t['win_rate'])} |")
    lines+=['','## 后段复核：不再选优','',
        '| 窗口 | 策略 | 成本 | 净收益 | 最大回撤 | 平仓数 | 胜率 |',
        '|---|---|---|---|---|---|---|']
    for m in sorted(result['holdout_cells'],key=lambda m:(m['split']!='holdout_2',m['split'],m['cost_scenario'],m['strategy_id'])):
        lines.append(f"| {m['split']} | {m['strategy_id']} | {m['cost_scenario']} | {pct(m['net_return'])} | {pct(m['max_drawdown'])} | {m['closed_trade_count']} | {pct(m['closed_trade_win_rate'])} |")
    lines+=['','主候选未通过原因：'+('; '.join(result['rejections']) or '无，达到本轮研究门槛'),'',
        '## 日期与限制','']+[f'- {k}: {v[0]}—{v[-1]}' for k,v in windows.items()]
    lines+=['','每窗口独立100万元，末11交易日停止新买；平均窗口收益不是连续复利。',
        '所用历史已经多次研究，不是全新样本外。历史ST、板块成员及公司行动仍有覆盖限制。',
        '分散约束针对新增持仓，后续板块归属变化不强制卖出。排序及仓位只使用信号日数据；买入价门槛额外在开盘执行时复查。',
        '不使用不足以验证的历史JEV信号；不启用生产，不调用收费API。']
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
