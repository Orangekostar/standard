from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from core.backtest.stability_research_v2 import (
    EnvironmentPolicy, environment_mask, replay_environment, stability_windows, summarize_stability,
)
from core.backtest.strategy_search_v2 import SearchMarket
from core.pipeline.prism_compare_config import file_sha256, implementation_identity, write_json
from core.pipeline.prism_compare_data import _verify_cache_file, experiment_lock
from core.pipeline.rank_rotation_research import load_rotation_config
from core.pipeline.stability_research import _load_market, _verified_source
from core.pipeline.strategy_search_research import _artifact_records
from core.technical_v2.contracts import ContractError, sha256_json


@dataclass(frozen=True)
class PullbackPolicy(EnvironmentPolicy):
    holding_sessions: int = 5
    gate: str = 'SECTOR20_BREADTH'
    range_scale: float = .5
    avoid_pullback: bool = True

    def __post_init__(self):
        super().__post_init__()
        if (self.holding_sessions != 5 or self.gate != 'SECTOR20_BREADTH'
                or self.range_scale != .5 or type(self.avoid_pullback) is not bool):
            raise ContractError('sector pullback study has one fixed baseline and one fixed filter')

    @property
    def policy_id(self):
        return 'SECTOR_PULLBACK_AVOID' if self.avoid_pullback else 'SECTOR_PULLBACK_BASELINE'


def sector_flags(context, sessions):
    """Trailing sector return and amount; no stock price/volume or forward labels."""
    if sessions != sorted(set(sessions)):
        raise ValueError('sector calendar must be unique and ordered')
    frame=context.loc[context.namespace.eq('SW_L1')].copy()
    if frame.empty or frame.duplicated(['sector_id','date']).any():
        raise ValueError('SW_L1 sector context is empty or duplicated')
    pieces=[]
    for sector, group in frame.groupby('sector_id',sort=True):
        out=group.set_index('date').reindex(sessions).rename_axis('date')
        price=pd.to_numeric(out.index_level,errors='coerce')
        amount=pd.to_numeric(out.amount_cny,errors='coerce')
        good=(out.status.eq('OK') & np.isfinite(price) & price.gt(0)
              & np.isfinite(amount) & amount.gt(0) & out.membership_changed.eq(False))
        # Missing sessions, invalid coverage, or changing constituents must not mimic shrinking volume.
        known=good.rolling(23,min_periods=23).sum().eq(23)
        ret=price/price.shift(3)-1
        ratio=amount.rolling(3,min_periods=3).mean()/amount.shift(3).rolling(20,min_periods=20).mean()
        pieces.append(pd.DataFrame(dict(sector_id=sector,return3=ret,amount_ratio=ratio,
            known=known,blocked=known & ret.lt(0) & ratio.le(.8))).reset_index())
    return pd.concat(pieces,ignore_index=True)


class PullbackMarket(SearchMarket):
    def __init__(self,base,flags):
        self.base,self.frames=base,base.frames
        self.flags={date:group.set_index('sector_id') for date,group in flags.groupby('date',sort=True)}

    def eligibility(self,date,codes):
        sectors=self.frames[date].loc[codes,'sector_id']
        flags=self.flags.get(date,pd.DataFrame(columns=['known','blocked']))
        known=sectors.map(flags.known).eq(True)
        blocked=sectors.map(flags.blocked).eq(True)
        return known,blocked

    def ranks(self,date,candidate):
        ranks=self.base.ranks(date,candidate)
        known,blocked=self.eligibility(date,ranks.index)
        return ranks.loc[known & ~blocked]


def replay_filter(market,flags,*,policy,**kwargs):
    filtered=PullbackMarket(market,flags) if policy.avoid_pullback else market
    return replay_environment(filtered,policy=policy,**kwargs)


def run_sector_pullback(experiment_root,output_root):
    root,output=Path(experiment_root).resolve(),Path(output_root).resolve()
    if root==output or root in output.parents or output in root.parents:
        raise ContractError('sector pullback results must be isolated from inputs')
    config=load_rotation_config()
    manifest,features=_verified_source(root,config)
    sector_record=features['context']['sectors']
    _verify_cache_file(sector_record)
    policies=[PullbackPolicy(avoid_pullback=False),PullbackPolicy()]
    windows=stability_windows(features['sessions'],manifest['windows']['test'])
    protocol=dict(schema_version='sector-pullback-research.v1',namespace='SW_L1',
        sector_return_sessions=3,sector_return_less_than=0,amount_recent_sessions=3,
        amount_reference_sessions=20,amount_reference_shift=3,amount_ratio_at_most=.8,
        known='23_CONSECUTIVE_OK_SESSIONS_POSITIVE_FINITE_PRICE_AMOUNT_UNCHANGED_MEMBERSHIP',
        unknown='NO_NEW_ENTRY_FOR_FILTER_ONLY',timing='SIGNAL_CLOSE_FOR_NEXT_OPEN',
        exits='UNCHANGED_NO_FORCED_EXIT_ON_SECTOR_PULLBACK',settlement_tail_sessions=11,
        policies=[dict(policy_id=p.policy_id,**asdict(p)) for p in policies],
        selection='NONE_ONE_FIXED_PAIRED_COMPARISON_NO_THRESHOLD_TUNING',
        reused_history=True,independent_window_capital_cny=1000000,
        production_activation=False,api_calls=0)
    identity=implementation_identity()
    binding=dict(dataset_sha256=file_sha256(root/'rank_dataset_manifest.json'),
        sector_context_sha256=sector_record['sha256'],
        configuration_sha256=sha256_json(dict(config=config,protocol=protocol)),
        implementation_hash=identity['implementation_hash'])
    with experiment_lock(output):
        result_path=output/'run_manifest.json'
        if result_path.exists():
            archived=json.loads(result_path.read_text())
            if archived['status']!='COMPLETE' or archived['binding']!=binding or archived['files']!=_artifact_records(output):
                raise ContractError('sector pullback archive differs; preserve it and use another output')
            return {**archived['result'],'reused':True}
        if any(p.name!='.compare.lock' for p in output.iterdir()):
            raise ContractError('incomplete sector pullback archive is preserved; use another output')
        write_json(output/'frozen_protocol.json',dict(binding=binding,protocol=protocol,
            base_parameters=config,windows=windows,source_identity=identity),immutable=True)
        flags=sector_flags(pd.read_parquet(sector_record['path']),features['sessions'])
        flags.to_csv(output/'sector_flags.csv.gz',index=False,compression='gzip')
        actions=pd.read_parquet(root/'corporate_actions.parquet')
        scope=sorted(set(manifest['scope_flags'])|{'REUSED_HOLDOUT','FIXED_SECTOR_PULLBACK_DIAGNOSTIC'})
        cells=[]; diagnostics=[]
        for phase,first,last in [('selection','selection_1','selection_6'),('holdout','holdout_1','holdout_2')]:
            market=_load_market(root,manifest,features,windows[first][0],windows[last][-1])
            filtered=PullbackMarket(market,flags)
            for split,dates in windows.items():
                if not split.startswith(phase):
                    continue
                # Candidate counts are signal opportunities, not executed trades; include only entry dates.
                for date in dates[:-12]:
                    ranks=market.ranks(date,policies[0].candidate)
                    ranks=ranks.loc[environment_mask(market.frames[date].loc[ranks.index],policies[0].gate)]
                    known,blocked=filtered.eligibility(date,ranks.index)
                    diagnostics.append(dict(split=split,date=date,candidates=len(ranks),
                        pullback_blocked=int(blocked.sum()),unknown_blocked=int((~known).sum())))
                for p in policies:
                    for cost in ('base','stress'):
                        replay_market=filtered if p.avoid_pullback else market
                        result=replay_environment(replay_market,policy=p,sessions=dates,config=config,
                            cost_scenario=cost,output_dir=output/phase/p.policy_id/split/cost,
                            run_id='sector-pullback',split=split,scope_flags=scope,corporate_actions=actions)
                        m=result['metrics']; cells.append(m)
                        print(json.dumps(dict(event='sector_pullback_cell_complete',policy_id=p.policy_id,
                            split=split,cost=cost,net_return=m['net_return'],closed=m['closed_trade_count'],
                            win_rate=m['closed_trade_win_rate'])),flush=True)
            del filtered,market
        summaries=[summarize_stability(p,[r for r in cells if r['strategy_id']==p.policy_id
            and r['split'].startswith('selection')]) for p in policies]
        write_json(output/'cells.json',cells,immutable=True)
        pd.DataFrame(diagnostics).to_csv(output/'candidate_filter_counts.csv',index=False)
        result=dict(status='COMPLETE',verdict='FIXED_COMPARISON_RESEARCH_ONLY',cells=cells,
            summaries=summaries,output_root=str(output),reused=False,api_calls=0)
        _report(output,result,windows)
        write_json(result_path,dict(status='COMPLETE',binding=binding,files=_artifact_records(output),result=result),immutable=True)
        return result


def _report(output,result,windows):
    def pct(v): return 'N/A' if v is None else f'{100*v:.2f}%'
    lines=['# 板块缩量回撤不开仓：固定条件对照','',
        '条件：申万一级板块合成指数近3日收益<0，近3日平均成交额/此前20日平均成交额≤0.8。',
        '比较区间不重叠；仅在信号收盘判断，限制次日新买，不强制卖出已有持仓。',
        '为防成分变动或缺数伪造缩量，需连续23个交易日板块数据有效且成分未变；未知时改版不开新仓。',
        '原策略的板块20日动量、市场/板块宽度、最多3只、5日持仓、4%止盈/3%止损、资金和成交限制全部保留。','',
        '| 窗口 | 策略 | 成本 | 净收益 | 最大回撤 | 平仓数 | 盈利数 | 胜率 |',
        '|---|---|---|---|---|---|---|---|']
    for r in sorted(result['cells'],key=lambda r:(r['split']!='holdout_2',r['split'],r['cost_scenario'],r['strategy_id'])):
        lines.append(f"| {r['split']} | {r['strategy_id']} | {r['cost_scenario']} | {pct(r['net_return'])} | {pct(r['max_drawdown'])} | {r['closed_trade_count']} | {r['winning_closed_trade_count']} | {pct(r['closed_trade_win_rate'])} |")
    lines+=['','## 前六窗口汇总','',
        '| 策略 | 成本 | 平均窗口净收益 | 盈利窗口 | 平仓数 | 合并胜率 | 原稳定性门槛 |',
        '|---|---|---|---|---|---|---|']
    for s in result['summaries']:
        for cost,a in s['cost_aggregates'].items():
            lines.append(f"| {s['policy_id']} | {cost} | {pct(a['mean_window_return'])} | {a['profitable_windows']}/6 | {a['closed_trade_count']} | {pct(a['win_rate'])} | {s['qualified']} |")
    counts=pd.read_csv(output/'candidate_filter_counts.csv').groupby('split')[['candidates','pullback_blocked','unknown_blocked']].sum()
    lines+=['','## 开仓信号影响（股票×信号日，不是成交笔数）','',counts.to_csv(index=True),'',
        '## 日期与限制','']+[f'- {k}: {v[0]}—{v[-1]}' for k,v in windows.items()]
    lines+=['','各窗口独立100万元，最后11个交易日停止新买。标准滑点每边0.1%，压力情景0.2%；费用沿用原配置。',
        '胜率按扣费后盈利平仓笔数/全部平仓笔数统计；平均窗口收益不是连续复利。压力成本会改变出场触发与后续交易路径。',
        '仅一个预先固定条件，无后验阈值搜索。历史已反复研究，不是新的样本外；历史ST、板块成分和公司行动信息仍不完整。',
        '板块是可用成分股合成序列，并非官方指数，也不能凭量价确认机构意图。未启用生产或调用JEV。']
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
