from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from core.backtest.price_volume_research import (
    MODES,PhasePolicy,phase_features,entry_mask,research_universe,replay_phase,
)
from core.backtest.stability_research_v2 import (
    EnvironmentPolicy,replay_environment,stability_windows,summarize_stability,select_stability,
)
from core.backtest.strategy_search_v2 import SearchMarket,enrich_features,qualification
from core.pipeline.prism_compare_config import file_sha256,implementation_identity,write_json
from core.pipeline.prism_compare_data import experiment_lock
from core.pipeline.rank_rotation_research import load_rotation_config
from core.pipeline.stability_research import _verified_source,ENVIRONMENT_COLUMNS
from core.pipeline.strategy_search_research import _artifact_records
from core.technical_v2.contracts import ContractError,sha256_json


def baseline_policy():
    return EnvironmentPolicy(5,'SECTOR20_BREADTH',.5)


def load_phase_market(root,manifest,features,start,end):
    names=pd.read_parquet(root/'roster.parquet')[['code','name']]
    columns=list(dict.fromkeys([*ENVIRONMENT_COLUMNS,'comparison_high','comparison_low',
        'amount_cny','market_index','sector_index']))
    pieces=[]
    for record in features['feature_chunks']:
        f=pd.read_parquet(record['path'],columns=columns,filters=[('date','<=',end)])
        f=phase_features(enrich_features(f))
        g=f.groupby('code',sort=False)
        valid=f.real_bar.eq(True)
        for k in range(1,6):
            valid &= g.real_bar.shift(-k).eq(True)
        f['forward_end']=g.date.shift(-5)
        f['gross_next_open_to_day5']=(g.comparison_close.shift(-5)/g.comparison_open.shift(-1)-1).where(valid)
        pieces.append(f.loc[f.date.ge(start)&f.date.le(end)])
    panel=pd.concat(pieces,ignore_index=True).merge(names,on='code',how='left',validate='many_to_one')
    frames={str(d):f for d,f in panel.groupby('date',sort=True)}
    expected=[d for d in features['sessions'] if start<=d<=end]
    if list(frames)!=expected or any(len(f)!=manifest['roster_count'] for f in frames.values()):
        raise ContractError('phase features have incomplete calendar or roster coverage')
    return SearchMarket(frames),panel


def describe_cohorts(panel,label,start,end):
    f=panel.loc[panel.date.ge(start)&panel.date.le(end)&research_universe(panel)
        &panel.market_context_status.eq('OK')&panel.sector_context_status.eq('OK')
        &np.isfinite(panel.pv_market5)&np.isfinite(panel.pv_sector5)].copy()
    phases={'横盘量价形态':f.pv_base_event,'仅价格回撤':f.pv_wash_price_event,
        '缩量回撤':f.pv_wash_event,'仅价格突破':f.pv_break_price_event,'放量突破':f.pv_breakout_event,
        '横盘回撤后突破':entry_mask(f,'BASE_WASH_BREAKOUT'),
        '背景先弱后强的完整序列':entry_mask(f,'CONTEXT_SEQUENCE')}
    records=[]
    for name,mask in phases.items():
        selected=f.loc[mask]
        outcome=selected.loc[selected.forward_end.le(end),'gross_next_open_to_day5'].dropna()
        records.append(dict(split=label,phase=name,stock_dates=len(selected),stocks=int(selected.code.nunique()),
            market_down_share=float(selected.pv_market5.lt(0).mean()) if len(selected) else None,
            sector_down_share=float(selected.pv_sector5.lt(0).mean()) if len(selected) else None,
            either_down_share=float(selected.pv_background_down.mean()) if len(selected) else None,
            both_up_share=float(selected.pv_background_up.mean()) if len(selected) else None,
            forward_samples=len(outcome),gross_mean=float(outcome.mean()) if len(outcome) else None,
            gross_positive_rate=float(outcome.gt(0).mean()) if len(outcome) else None))
    return records


def choose(summaries):
    baseline=next(r for r in summaries if r['policy_id']==baseline_policy().policy_id)
    candidates=[r for r in summaries if r['policy_id']!=baseline['policy_id']]
    selection=select_stability(candidates)
    def strength(r):
        return (r['robust_lower_quartile'],r['robust_mean_return'],-r['worst_drawdown'])
    if selection['chosen_policy_id'] and baseline['qualified']:
        target=next(r for r in candidates if r['policy_id']==selection['chosen_policy_id'])
        if strength(target)<=strength(baseline):
            selection['chosen_policy_id']=None
    return selection


def run_price_volume(experiment_root,output_root):
    root,output=Path(experiment_root).resolve(),Path(output_root).resolve()
    if root==output or root in output.parents or output in root.parents:
        raise ContractError('research output must be isolated from source')
    config=load_rotation_config()
    manifest,features=_verified_source(root,config)
    policies=[baseline_policy(),*[PhasePolicy(mode) for mode in MODES]]
    identity=implementation_identity()
    protocol=dict(schema_version='price-volume-stages.v1',
        meaning='OBSERVABLE_PROXIES_NOT_PROOF_OF_INSTITUTIONAL_INVENTORY_OR_INTENT',
        market='FROZEN_MAINBOARD_EQUAL_WEIGHT_SYNTHETIC_INDEX_NOT_SSE_OR_SZSE_OFFICIAL_INDEX',
        base='ABS_RETURN20_LE_5PCT_CLOSE_RANGE20_LE_12PCT_AMOUNT5_VS_PRECEDING20_GE_1_05_WEIGHTED_CLV20_GE_0_1',
        wash='RETURN3_NEGATIVE_DRAWDOWN20_3_TO_12PCT_CLOSE_GE_95PCT_MA60_AMOUNT3_VS_PRECEDING20_LE_0_8',
        sequence='BASE_WITHIN_PRIOR40_SESSIONS_THEN_WASH_HOLDING_97PCT_OF_BASE_FLOOR_THEN_BREAKOUT_WITHIN10',
        breakout='CLOSE_GT_PRIOR10_CLOSE_HIGH_RETURN1_0_TO_7PCT_CLV_GE_0_5_AMOUNT_VS_PRIOR20_GE_1_3',
        context='DURING_WASH_MARKET5_OR_SECTOR5_NEGATIVE_AT_BREAKOUT_BOTH_POSITIVE',
        entry_ranking='NATIVE_SCORE3_DESCENDING_THEN_CODE',
        account='MAX3_5DAY_HOLD_4PCT_PROFIT_3PCT_STOP_SIGNAL_CLOSE_NEXT_OPEN_RANGE_NEW_BUDGET_HALF',
        selection='SIX63_DAY_WINDOWS_BOTH_COSTS_5OF6_POSITIVE_WORST_GE_MINUS3PCT_DD_LE10PCT_60CLOSED_40DATES_FLAT',
        choice='QUALIFIED_THEN_WORST_COST_LOWER_QUARTILE_MEAN_DRAWDOWN_MUST_BEAT_ORIGINAL',
        later='TWO66_DAY_WINDOWS_ALL_FIVE_PREDECLARED_POLICIES_DIAGNOSTIC_ONLY_NO_RESELECTION',
        upgrade='ONLY_FROZEN_CHOICE_ALL_LATER_CELLS_PROFIT_DD_LE10PCT_10CLOSED_10DATES_FLAT_AND_MEAN_BETTER_DD_NO_WORSE',
        costs=config['costs'],independent_initial_capital=1000000,tail_no_new_buys=11,
        reused_history=True,production_activation=False,api_calls=0)
    binding=dict(dataset_sha256=file_sha256(root/'rank_dataset_manifest.json'),
        configuration_sha256=sha256_json(dict(config=config,protocol=protocol)),implementation_hash=identity['implementation_hash'])
    with experiment_lock(output):
        manifest_path=output/'run_manifest.json'
        if manifest_path.exists():
            old=json.loads(manifest_path.read_text())
            if old['status']!='COMPLETE' or old['binding']!=binding or old['files']!=_artifact_records(output):
                raise ContractError('archived result changed; preserve and choose another namespace')
            return {**old['result'],'reused':True}
        if any(p.name!='.compare.lock' for p in output.iterdir()):
            raise ContractError('partial result preserved; choose another namespace')
        windows=stability_windows(features['sessions'],manifest['windows']['test'])
        write_json(output/'frozen_protocol.json',dict(protocol=protocol,binding=binding,windows=windows,
            policies=[dict(policy_id=p.policy_id,**asdict(p)) for p in policies],source_identity=identity),immutable=True)
        actions=pd.read_parquet(root/'corporate_actions.parquet')
        flags=sorted(set(manifest['scope_flags'])|{'REUSED_HOLDOUT','PRICE_VOLUME_PROXY_NOT_INSTITUTION_IDENTITY'})
        market,panel=load_phase_market(root,manifest,features,windows['selection_1'][0],windows['selection_6'][-1])
        cohorts=describe_cohorts(panel,'train',windows['selection_1'][0],windows['selection_6'][-1])
        summaries,cells=[],[]
        for p in policies:
            p_cells=[]
            fn=replay_environment if isinstance(p,EnvironmentPolicy) else replay_phase
            for split,dates in windows.items():
                if not split.startswith('selection_'):
                    continue
                for cost in ('base','stress'):
                    r=fn(market,policy=p,sessions=dates,config=config,cost_scenario=cost,
                        output_dir=output/'selection'/p.policy_id/split/cost,run_id='price-volume',
                        split=split,scope_flags=flags,corporate_actions=actions)
                    p_cells.append(r['metrics'])
            summary=summarize_stability(p,p_cells)
            summaries.append(summary)
            cells.extend(p_cells)
            print(json.dumps(dict(event='price_volume_discovery_complete',**summary)),flush=True)
        selection=choose(summaries)
        write_json(output/'selection_cells.json',cells,immutable=True)
        write_json(output/'selection_freeze.json',dict(selection=selection,binding=binding,
            frozen_at=datetime.now(timezone.utc).isoformat(),selection_cells_sha256=file_sha256(output/'selection_cells.json')),immutable=True)
        print(json.dumps(dict(event='price_volume_choice_frozen',**selection)),flush=True)
        del market,panel
        market,panel=load_phase_market(root,manifest,features,windows['holdout_1'][0],windows['holdout_2'][-1])
        for split in ('holdout_1','holdout_2'):
            cohorts.extend(describe_cohorts(panel,split,windows[split][0],windows[split][-1]))
        if len(panel):
            event=panel.pv_base_event|panel.pv_wash_event|panel.pv_breakout_event
            cols=['code','name','date','market_state','score3','sector_id',*[c for c in panel if c.startswith('pv_')]]
            panel.loc[event&research_universe(panel),cols].to_csv(output/'later_phase_events.csv.gz',index=False)
        reviewed=[]
        for p in policies:
            fn=replay_environment if isinstance(p,EnvironmentPolicy) else replay_phase
            for split in ('holdout_1','holdout_2'):
                for cost in ('base','stress'):
                    r=fn(market,policy=p,sessions=windows[split],config=config,cost_scenario=cost,
                        output_dir=output/'holdout'/p.policy_id/split/cost,run_id='price-volume',
                        split=split,scope_flags=flags,corporate_actions=actions)
                    m=r['metrics']
                    reviewed.append(m)
                    print(json.dumps(dict(event='price_volume_later_complete',policy_id=p.policy_id,split=split,
                        cost=cost,net_return=m['net_return'],max_drawdown=m['max_drawdown'],closed=m['closed_trade_count'])),flush=True)
        chosen=selection['chosen_policy_id']
        rejections=[]
        if chosen:
            target=[r for r in reviewed if r['strategy_id']==chosen]
            original=[r for r in reviewed if r['strategy_id']==baseline_policy().policy_id]
            rejections=[r['split']+':'+r['cost_scenario']+':'+reason for r in target for reason in
                qualification(r,min_closed=10,min_dates=10,max_drawdown=.1)]
            if not rejections:
                def weakmean(rows):
                    return min(np.mean([r['net_return'] for r in rows if r['cost_scenario']==cost]) for cost in ('base','stress'))
                if weakmean(target)<=weakmean(original):
                    rejections.append('NO_NET_RETURN_IMPROVEMENT')
                if max(r['max_drawdown'] for r in target)>max(r['max_drawdown'] for r in original):
                    rejections.append('WORSE_DRAWDOWN')
        verdict='NO_DISCOVERY_UPGRADE' if chosen is None else 'REVIEW_FAILED' if rejections else 'RESEARCH_UPGRADE_PASSED'
        result=dict(status='COMPLETE',verdict=verdict,selection=selection,summaries=summaries,
            holdout_cells=reviewed,holdout_rejections=rejections,cohorts=cohorts,reused=False,output_root=str(output))
        pd.DataFrame(cohorts).to_csv(output/'phase_cohorts.csv',index=False,encoding='utf-8-sig')
        _report(output,result,windows)
        write_json(manifest_path,dict(status='COMPLETE',binding=binding,result=result,files=_artifact_records(output)),immutable=True)
        return result


def _report(output,result,windows):
    def pct(v):
        return 'N/A' if v is None else f'{v*100:.2f}%'
    lines=['# 量价阶段与市场/板块联动研究','',f"结论：{result['verdict']}",
        f"早期冻结选择：{result['selection']['chosen_policy_id']}",'',
        '阶段是可观测代理，不是机构成本、持仓或交易意图的证明。市场使用冻结主板等权合成指数，行业使用现有行业合成指数，非上证指数/深成指。',
        '同涨同跌只说明联动，且价格回撤/突破本身就容易随市场变化；需与不要求量能的价格形态及单纯突破比较。以下阶段统计为描述性结果，不作机构操盘因果推断。','',
        '## 阶段定义','',
        '- 横盘量价：20日涨跌幅绝对值≤5%、收盘高低区间≤12%、近5日均成交额/此前20日≥1.05、20日成交额加权收盘位置压力≥0.1。',
        '- 缩量回撤：3日跌、距20日最高收盘回撤3%—12%、仍在60日均线95%以上、近3日均成交额/此前20日≤0.8。',
        '- 完整顺序：之前40日内出现横盘量价，再出现未跌破平台收盘下沿97%的缩量回撤，再于10日内放量突破。',
        '- 放量突破：收盘突破前10日最高收盘、当日涨幅0—7%、收于当日价格区间上四分之一、成交额/此前20日均值≥1.3。',
        '- 背景条件：回撤时大盘或板块5日收益为负；突破时大盘和板块5日收益均为正。所有条件只用当日及更早数据。','',
        '## 阶段与环境（股票-日期事件；连续形态仅计首次，完整序列按信号日）','',
        '| 区间 | 阶段 | 样本数 | 大盘跌占比 | 板块跌占比 | 两者均涨占比 | 次开盘至第5日收盘毛收益均值 |',
        '|---|---|---:|---:|---:|---:|---:|']
    for r in result['cohorts']:
        lines.append(f"| {r['split']} | {r['phase']} | {r['stock_dates']} | {pct(r['market_down_share'])} | {pct(r['sector_down_share'])} | {pct(r['both_up_share'])} | {pct(r['gross_mean'])} |")
    lines += ['','毛收益不含交易成本、涨停买不到和仓位限制，不能等同于下表净收益；背景完整序列的“两者均涨”是入场定义，不是独立证据。','',
        '## 六个早期窗口筛选','',
        '| 策略 | 合格 | 标准/压力盈利窗口 | 标准/压力平均净收益 | 最差回撤 |',
        '|---|---|---|---|---|']
    for r in result['summaries']:
        b,s=r['cost_aggregates']['base'],r['cost_aggregates']['stress']
        lines.append(f"| {r['policy_id']} | {r['qualified']} | {b['profitable_windows']}/6 / {s['profitable_windows']}/6 | {pct(b['mean_window_return'])} / {pct(s['mean_window_return'])} | {pct(r['worst_drawdown'])} |")
    lines += ['','## 2026年两个窗口实际账户复核','',
        '| 策略 | 窗口 | 成本 | 净收益 | 最大回撤 | 平仓数 | 胜率 |','|---|---|---|---|---|---|---|']
    for r in result['holdout_cells']:
        lines.append(f"| {r['strategy_id']} | {r['split']} | {r['cost_scenario']} | {pct(r['net_return'])} | {pct(r['max_drawdown'])} | {r['closed_trade_count']} | {pct(r['closed_trade_win_rate'])} |")
    lines += ['','所有账户独立100万元，最多3只；按原score3排序，震荡新买额度减半；5日持有、4%收盘止盈、3%收盘止损，下一可成交开盘执行。末11日停止新买，完整保留T+1、费用、滑点、停牌、涨跌停及现金约束。',
        '早期门槛沿用≥5/6盈利、最差季度≥−3%、回撤≤10%、≥60笔平仓、≥40个成交日，两成本均需满足；按较弱成本收益25%分位数、均值、回撤排序且须优于原策略。后段全部预定规则仅作对照，禁止据此重选，只有早期冻结者可升级。',
        '历史已反复研究，非全新独立样本外区间；历史ST、公司行动和成分资料不完整。无机构逐笔账户、持仓或真实成本资料；没有生产启用或JEV调用。','']
    lines += [f'- {key}: {dates[0]}—{dates[-1]}' for key,dates in windows.items()]
    lines += ['',*[r['policy_id']+': '+('; '.join(r['rejection_reasons']) or '初筛通过') for r in result['summaries']]]
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
