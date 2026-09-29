from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from core.backtest.stability_research_v2 import (
    EnvironmentPolicy, replay_environment, select_stability, stability_windows, summarize_stability,
)
from core.backtest.strategy_search_v2 import SearchMarket, qualification
from core.pipeline.prism_compare_config import file_sha256, implementation_identity, write_json
from core.pipeline.prism_compare_data import experiment_lock
from core.pipeline.rank_rotation_research import load_rotation_config
from core.pipeline.stability_research import _verified_source, _load_market, research_protocol
from core.pipeline.strategy_search_research import _artifact_records
from core.technical_v2.contracts import ContractError, sha256_json


@dataclass(frozen=True)
class EntryPolicy(EnvironmentPolicy):
    holding_sessions: int = 5
    gate: str = 'SECTOR20_BREADTH'
    range_scale: float = .5
    require_up: bool = False
    breadth_floor: float = .5

    def __post_init__(self):
        super().__post_init__()
        if (self.holding_sessions != 5 or self.gate != 'SECTOR20_BREADTH' or self.range_scale != .5
                or type(self.require_up) is not bool
                or (self.require_up,self.breadth_floor) not in {(False,.5),(False,.6),(True,.5),(True,.6),(True,.65)}):
            raise ContractError('entry policy outside fixed four-candidate research grid')

    @property
    def policy_id(self):
        return f'ENTRY_{"UP" if self.require_up else "ANY"}_B{int(self.breadth_floor*100)}_H5_R50'


def policy_grid():
    return [EntryPolicy(),EntryPolicy(require_up=True),EntryPolicy(breadth_floor=.6),
            EntryPolicy(require_up=True,breadth_floor=.6),EntryPolicy(require_up=True,breadth_floor=.65)]


class FilterMarket(SearchMarket):
    def __init__(self, base, policy):
        self.base,self.policy,self.frames=base,policy,base.frames

    def ranks(self,date,candidate):
        ranks=self.base.ranks(date,candidate)
        frame=self.frames[date].loc[ranks.index]
        mask=(np.isfinite(frame.market_breadth20)&np.isfinite(frame.sector_breadth20)
              &frame.market_breadth20.ge(self.policy.breadth_floor)
              &frame.sector_breadth20.ge(self.policy.breadth_floor))
        if self.policy.require_up:
            mask &= frame.forecast_class3.eq('up')
        return ranks.loc[mask.fillna(False)]


def replay_filter(market,*,policy,**kwargs):
    return replay_environment(FilterMarket(market,policy),policy=policy,**kwargs)


def choose_upgrade(summaries):
    baseline_id=EntryPolicy().policy_id
    baseline=next(row for row in summaries if row['policy_id']==baseline_id)
    candidates=[row for row in summaries if row['policy_id']!=baseline_id]
    selection=select_stability(candidates)
    def strength(row):
        return (row['robust_lower_quartile'],row['robust_mean_return'],-row['worst_drawdown'])
    chosen=selection['chosen_policy_id']
    if chosen is not None and baseline['qualified']:
        target=next(row for row in candidates if row['policy_id']==chosen)
        if strength(target)<=strength(baseline):
            selection['chosen_policy_id']=None
    selection['baseline_policy_id']=baseline_id
    return selection


def run_entry_filter(experiment_root,output_root):
    root,output=Path(experiment_root).resolve(),Path(output_root).resolve()
    if root==output or root in output.parents or output in root.parents:
        raise ContractError('entry filter results must be isolated from input data')
    config=load_rotation_config()
    manifest,features=_verified_source(root,config)
    policies=policy_grid()
    protocol=dict(schema_version='entry-filter-research.v1',
        inherited_stability_gates={k:v for k,v in research_protocol().items() if k.startswith('selection_') or
            k in {'maximum_window_drawdown','holdout_min_closed_per_window_each_cost',
                  'holdout_min_traded_dates_per_window_each_cost','settlement_tail_sessions'}},
        policies=[dict(policy_id=p.policy_id,**asdict(p)) for p in policies],
        fixed_baseline=EntryPolicy().policy_id,
        discovery='FOUR_CANDIDATES_MUST_QUALIFY_AND_STRICTLY_BEAT_BASELINE_STABILITY_RANK',
        review='FREEZE_ONE_TARGET_BEFORE_LOADING_LATER_HISTORY_NO_RESELECTION',
        review_improvement='WORST_COST_MEAN_RETURN_HIGHER_AND_WORST_DRAWDOWN_NO_HIGHER_THAN_BASELINE',
        diagnostic='IF_NO_UPGRADE_REVIEW_RANK_LEADER_WITHOUT_PROMOTION',
        window_accounts='INDEPENDENT_ONE_MILLION_CNY_NOT_CONTINUOUS_COMPOUNDING',
        reused_history=True,production_activation=False,api_calls=0)
    identity=implementation_identity()
    binding=dict(dataset_sha256=file_sha256(root/'rank_dataset_manifest.json'),
        configuration_sha256=sha256_json(dict(config=config,protocol=protocol)),
        implementation_hash=identity['implementation_hash'])
    with experiment_lock(output):
        result_path=output/'run_manifest.json'
        if result_path.exists():
            archived=json.loads(result_path.read_text())
            if archived['status']!='COMPLETE' or archived['binding']!=binding or archived['files']!=_artifact_records(output):
                raise ContractError('entry filter archive differs; preserve it and use another output namespace')
            return {**archived['result'],'reused':True}
        if any(p.name!='.compare.lock' for p in output.iterdir()):
            raise ContractError('incomplete entry filter output is preserved; use another namespace')
        windows=stability_windows(features['sessions'],manifest['windows']['test'])
        write_json(output/'frozen_protocol.json',dict(binding=binding,protocol=protocol,
            windows=windows,source_identity=identity),immutable=True)
        actions=pd.read_parquet(root/'corporate_actions.parquet')
        flags=sorted(set(manifest['scope_flags'])|{'REUSED_HOLDOUT','POST_LOSS_DIAGNOSTIC_RESEARCH'})
        market=_load_market(root,manifest,features,windows['selection_1'][0],windows['selection_6'][-1])
        summaries,cells=[],[]
        for p in policies:
            policy_cells=[]
            for split,dates in windows.items():
                if not split.startswith('selection_'):
                    continue
                for cost in ('base','stress'):
                    result=replay_filter(market,policy=p,sessions=dates,config=config,cost_scenario=cost,
                        output_dir=output/'selection'/p.policy_id/split/cost,run_id='entry-filter',
                        split=split,scope_flags=flags,corporate_actions=actions)
                    policy_cells.append(result['metrics'])
            summary=summarize_stability(p,policy_cells)
            summaries.append(summary)
            cells.extend(policy_cells)
            print(json.dumps(dict(event='entry_filter_selection_complete',**summary)),flush=True)
        del market
        selection=choose_upgrade(summaries)
        target=selection['chosen_policy_id'] or selection['diagnostic_policy_id']
        write_json(output/'selection_cells.json',cells,immutable=True)
        write_json(output/'selection_freeze.json',dict(binding=binding,selection=selection,
            review_policy_id=target,selection_cells_sha256=file_sha256(output/'selection_cells.json'),
            frozen_at=datetime.now(timezone.utc).isoformat()),immutable=True)
        print(json.dumps(dict(event='entry_filter_frozen',**selection)),flush=True)
        market=_load_market(root,manifest,features,windows['holdout_1'][0],windows['holdout_2'][-1])
        reviewed=[]
        for p in [next(p for p in policies if p.policy_id==target),EntryPolicy()]:
            for split in ('holdout_1','holdout_2'):
                for cost in ('base','stress'):
                    result=replay_filter(market,policy=p,sessions=windows[split],config=config,cost_scenario=cost,
                        output_dir=output/'holdout'/p.policy_id/split/cost,run_id='entry-filter',
                        split=split,scope_flags=flags,corporate_actions=actions)
                    metrics=result['metrics']
                    reviewed.append(metrics)
                    print(json.dumps(dict(event='entry_filter_review_complete',policy_id=p.policy_id,
                        split=split,cost=cost,net_return=metrics['net_return'],closed=metrics['closed_trade_count'],
                        max_drawdown=metrics['max_drawdown'])),flush=True)
        targets=[r for r in reviewed if r['strategy_id']==target]
        baseline=[r for r in reviewed if r['strategy_id']==EntryPolicy().policy_id]
        rejections=[r['split']+':'+r['cost_scenario']+':'+reason for r in targets
            for reason in qualification(r,min_closed=10,min_dates=10,max_drawdown=.10)]
        def weak_mean(rows):
            return min(sum(r['net_return'] for r in rows if r['cost_scenario']==cost)/2 for cost in ('base','stress'))
        if all(r['net_return'] is not None and r['max_drawdown'] is not None for r in reviewed):
            if weak_mean(targets)<=weak_mean(baseline):
                rejections.append('NO_RETURN_IMPROVEMENT_VS_BASELINE')
            if max(r['max_drawdown'] for r in targets)>max(r['max_drawdown'] for r in baseline):
                rejections.append('WORSE_DRAWDOWN_VS_BASELINE')
        else:
            rejections.append('INCOMPLETE_REVIEW_COMPARISON')
        verdict=('KEEP_BASELINE_NO_DISCOVERY_UPGRADE' if selection['chosen_policy_id'] is None else
                 'KEEP_BASELINE_REVIEW_FAILED' if rejections else 'RESEARCH_UPGRADE_PASSED')
        result=dict(status='COMPLETE',verdict=verdict,selection=selection,summaries=summaries,
            review_policy_id=target,holdout_cells=reviewed,holdout_rejections=rejections,
            output_root=str(output),reused=False,api_calls=0)
        _report(output,result,windows)
        write_json(result_path,dict(status='COMPLETE',binding=binding,files=_artifact_records(output),result=result),immutable=True)
        return result


def _report(output,result,windows):
    def pct(value):
        return 'N/A' if value is None else f'{value*100:.2f}%'
    lines=['# UP与宽度门槛优化对照','',f"结果：{result['verdict']}",
        f"冻结复核对象：{result['review_policy_id']}",'',
        'ANY不要求forecast=up；UP要求up。B50/B60/B65为市场与板块宽度同时达到的门槛。',
        '所有方案均保留板块20日动量为正、加强趋势回踩、最多3只、震荡新买额度减半、5日持有、4%止盈/3%止损收盘触发与下一可成交开盘执行。',
        '初筛使用六个早期63交易日窗口。每种成本均需至少5/6窗口盈利、最差季度≥−3%、回撤≤10%、至少60笔平仓与40个成交日；改版还须严格优于原策略的预定稳定性排序。',
        '后段每窗口每种成本均需盈利、回撤≤10%、至少10笔平仓和10个成交日，且两窗口较弱成本平均收益提高、最差回撤不高于基准。初筛失败的诊断对象不能凭后段结果升级。','',
        '## 初筛（标准 / 双倍滑点）','',
        '| 策略 | 合格 | 盈利窗口 | 平均窗口收益 | 最大回撤 | 平仓数 |',
        '|---|---|---|---|---|---|']
    for r in result['summaries']:
        b,s=r['cost_aggregates']['base'],r['cost_aggregates']['stress']
        lines.append(f"| {r['policy_id']} | {r['qualified']} | {b['profitable_windows']}/6 · {s['profitable_windows']}/6 | {pct(b['mean_window_return'])} / {pct(s['mean_window_return'])} | {pct(b['max_drawdown'])} / {pct(s['max_drawdown'])} | {b['closed_trade_count']} / {s['closed_trade_count']} |")
    lines += ['','## 后段逐窗口结果','',
        '| 策略 | 窗口 | 成本 | 净收益 | 最大回撤 | 平仓数 | 胜率 |',
        '|---|---|---|---|---|---|---|']
    for r in result['holdout_cells']:
        lines.append(f"| {r['strategy_id']} | {r['split']} | {r['cost_scenario']} | {pct(r['net_return'])} | {pct(r['max_drawdown'])} | {r['closed_trade_count']} | {pct(r['closed_trade_win_rate'])} |")
    lines += ['','## 未达标原因','']
    lines += [r['policy_id']+': '+('; '.join(r['rejection_reasons']) or '初筛合格') for r in result['summaries']]
    lines += ['','后段：'+('; '.join(result['holdout_rejections']) or '达标'),'','## 日期与证据边界','']
    lines += [f'- {key}: {dates[0]}—{dates[-1]}' for key,dates in windows.items()]
    lines += ['','每窗口独立100万元，末11个交易日停止新买，平均收益不是连续复利收益。',
        '后段历史已用于诊断，不是新的独立样本外验证；历史ST、成分和公司行动资料不完整。未启用生产交易，也未调用JEV。']
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
