"""Complete comparison, clustered uncertainty, and traceable derived outputs."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from core.backtest.high_win_metrics import review_qualification
from core.pipeline.prism_compare_config import file_sha256, write_json
from core.technical_v2.contracts import ContractError


def _read(path):
    return json.loads(path.read_text())


def _daily_arrays(root,policy,split,cost):
    cell=root/policy/split/cost
    nav=pd.read_csv(cell/'daily_nav.csv.gz',dtype={'date':str})
    trades=pd.read_csv(cell/'trades.csv',dtype={'entry_date':str,'code':str})
    metrics=_read(cell/'metrics.json')
    if not metrics['nav_complete']: return None
    values=np.r_[metrics['initial_nav']*100,nav.nav_cents.to_numpy()]
    returns=values[1:]/values[:-1]-1
    grouped=trades.assign(win=trades.realized_pnl_cents.gt(0)).groupby('entry_date').agg(
        wins=('win','sum'),count=('win','size')).reindex(nav.date,fill_value=0)
    return returns,grouped.wins.to_numpy(dtype=float),grouped['count'].to_numpy(dtype=float)


def paired_bootstrap(root,selected,windows,iterations=1000,block=10,seed=20260929):
    rng=np.random.default_rng(seed)
    policies=sorted(set(selected)|{'BASE_ENV','BASE_DEF'})
    records=[]
    for phase,splits in (('selection',[k for k in windows if k.startswith('selection')]),('review',['review'])):
        # The same date-block indices are used for every policy and both costs.
        draws={}
        for split in splits:
            n=len(windows[split]);starts=rng.integers(0,n-block+1,size=(iterations,int(np.ceil(n/block))))
            draws[split]=(starts[:,:,None]+np.arange(block)[None,None,:]).reshape(iterations,-1)[:,:n]
        for cost in ('base','stress'):
            base={split:_daily_arrays(root,'BASE_ENV',split,cost) for split in splits}
            for policy in policies:
                pairs={split:_daily_arrays(root,policy,split,cost) for split in splits}
                if any(v is None for v in [*base.values(),*pairs.values()]):
                    records.append(dict(phase=phase,cost=cost,policy_id=policy,status='INCOMPLETE_NAV'));continue
                gains=[];baseline_gains=[]
                wins=np.zeros(iterations);counts=np.zeros(iterations)
                bwins=np.zeros(iterations);bcounts=np.zeros(iterations)
                for split in splits:
                    ix=draws[split];r,w,c=pairs[split];br,bw,bc=base[split]
                    gains.append(np.prod(1+r[ix],axis=1)-1)
                    baseline_gains.append(np.prod(1+br[ix],axis=1)-1)
                    wins+=w[ix].sum(axis=1);counts+=c[ix].sum(axis=1)
                    bwins+=bw[ix].sum(axis=1);bcounts+=bc[ix].sum(axis=1)
                ret=np.mean(gains,axis=0);diff=ret-np.mean(baseline_gains,axis=0)
                rates=np.divide(wins,counts,out=np.full(iterations,np.nan),where=counts>0)
                brates=np.divide(bwins,bcounts,out=np.full(iterations,np.nan),where=bcounts>0)
                def interval(a):
                    good=a[np.isfinite(a)]
                    return np.quantile(good,[.025,.975]).tolist() if len(good) else [None,None]
                records.append(dict(phase=phase,cost=cost,policy_id=policy,status='DESCRIPTIVE_REUSED_HISTORY',
                    return_interval95=interval(ret),return_difference_vs_BASE_ENV_interval95=interval(diff),
                    win_rate_interval95=interval(rates),win_rate_difference_vs_BASE_ENV_interval95=interval(rates-brates),
                    valid_trade_resamples=int(np.isfinite(rates).sum())))
    return dict(iterations=iterations,block_sessions=block,seed=seed,
        grouping='SYNCHRONOUS_MOVING_DATE_BLOCKS_EPISODES_GROUPED_BY_ENTRY_DATE',
        independent_windows='RESAMPLE_WITHIN_EACH_WINDOW_THEN_MEAN_WINDOW_RETURNS_NOT_CROSS_WINDOW_COMPOUNDING',
        caveat='DESCRIPTIVE_ONLY_CORRELATED_TRADES_MULTIPLE_SEARCH_AND_REUSED_HISTORY_NOT_CORRECTED',records=records)


def _flat_selection(summaries):
    rows=[]
    for s in summaries:
        for cost,m in s['costs'].items():
            rows.append(dict(policy_id=s['policy_id'],cost=cost,phase='selection',qualified=s['qualified'],
                net_return=m['mean_window_return'],median_return=m['median_window_return'],worst_return=m['worst_window_return'],
                max_drawdown=m['worst_drawdown'],win_rate=m['win_rate'],closed_episodes=m['closed_episode_count'],
                wilson_lower=m['wilson95'][0],wilson_upper=m['wilson95'][1],profit_factor=m['profit_factor'],
                profit_factor_status=m['profit_factor_status'],payoff_ratio_return=m['payoff_ratio_return'],
                mean_episode_net_return=m['mean_episode_net_return'],nonnegative_windows=m['nonnegative_windows'],
                positive_windows=m['positive_windows'],rejections='|'.join(m['rejections'])))
    return rows


def generate_report(output):
    output=Path(output)
    frozen=_read(output/'selection_freeze.json');freeze_hash=file_sha256(output/'selection_freeze.json')
    early=_read(output/'selection_cells.json');review=_read(output/'review_cells.json')
    protocol=_read(output/'protocol_frozen.json')
    if len(early)!=216 or len(review)!=36:
        raise ContractError('complete report requires 216 early and 36 continuous review accounts')
    keys=[(c['strategy_id'],c['split'],c['cost_scenario']) for c in early+review]
    expected={(p,split,cost) for p in [s['policy_id'] for s in frozen['summaries']]
              for split in protocol['windows'] for cost in ('base','stress')}
    if len(set(keys))!=252 or set(keys)!=expected:
        raise ContractError('report account matrix differs from frozen 252 cells')
    primary=frozen['selection']['primary_policy_id'];rejections=[]
    if primary is None: rejections.append('NO_HIGH_WIN_CANDIDATE')
    else:
        for cost in ('base','stress'):
            target=next(c for c in review if c['strategy_id']==primary and c['cost_scenario']==cost)
            baseline=next(c for c in review if c['strategy_id']=='BASE_ENV' and c['cost_scenario']==cost)
            rejections.extend(cost+':'+r for r in review_qualification(target,baseline))
    verdict='NO_HIGH_WIN_CANDIDATE' if primary is None else 'REVIEW_NOT_QUALIFIED' if rejections else 'RESEARCH_COMPARISON_PASSED_EVIDENCE_LIMITED'
    selection_rows=_flat_selection(frozen['summaries'])
    later_rows=[]
    for c in review:
        baseline=next(b for b in review if b['strategy_id']=='BASE_ENV' and b['cost_scenario']==c['cost_scenario'])
        later_rows.append(dict(policy_id=c['strategy_id'],cost=c['cost_scenario'],phase='review',
            net_return=c['net_return'],max_drawdown=c['max_drawdown'],win_rate=c['win_rate'],
            closed_episodes=c['closed_episode_count'],wilson_lower=c['wilson95'][0],wilson_upper=c['wilson95'][1],
            profit_factor=c['profit_factor'],profit_factor_status=c['profit_factor_status'],
            payoff_ratio_return=c['payoff_ratio_return'],mean_episode_net_return=c['mean_episode_net_return'],
            primary=c['strategy_id']==primary,
            diagnostic_rejections='|'.join(review_qualification(c,baseline))))
    pd.DataFrame(selection_rows).to_csv(output/'selection_summary.csv',index=False)
    pd.DataFrame(later_rows).to_csv(output/'review_summary.csv',index=False)
    pd.DataFrame(selection_rows+later_rows).to_csv(output/'all_candidates.csv',index=False)
    sector_rows=[];blockers=[];segments=[]
    for c in early+review:
        policy,split,cost=c['strategy_id'],c['split'],c['cost_scenario']
        cell=output/policy/split/cost
        trades=pd.read_csv(cell/'trades.csv',dtype={'entry_date':str,'closed_at':str})
        for sector,g in trades.groupby('sector_id',dropna=False):
            sector_rows.append(dict(policy_id=policy,split=split,cost=cost,sector_id=sector,episodes=len(g),
                wins=int(g.realized_pnl_cents.gt(0).sum()),net_pnl_cny=float(g.realized_pnl_cents.sum()/100),
                mean_episode_return=float(g.net_episode_return.mean())))
        for kind in ('signal_blockers','execution_blockers'):
            for reason,count in c[kind].items():
                blockers.append(dict(policy_id=policy,split=split,cost=cost,kind=kind,reason=reason,count=count))
        blockers.append(dict(policy_id=policy,split=split,cost=cost,kind='OPEN_ASSETS',reason='UNRESOLVED_AT_END',count=c['open_position_count']))
        for segment in c.get('continuous_segments',[]):
            segments.append(dict(policy_id=policy,cost=cost,**{k:v for k,v in segment.items() if not isinstance(v,(list,dict))}))
    pd.DataFrame(sector_rows,columns=['policy_id','split','cost','sector_id','episodes','wins','net_pnl_cny','mean_episode_return']).to_csv(output/'per_sector_summary.csv',index=False)
    pd.DataFrame(blockers).to_csv(output/'coverage_and_blockers.csv',index=False)
    pd.DataFrame(segments).to_csv(output/'review_segments.csv',index=False)
    suite=protocol['suite'];uncertainty=paired_bootstrap(output,frozen['selection']['selected_policy_ids'],protocol['windows'],
        iterations=suite['bootstrap']['iterations'],block=suite['bootstrap']['block_sessions'],seed=suite['bootstrap']['seed'])
    write_json(output/'uncertainty.json',uncertainty)
    # These three roles describe later results only; they cannot alter primary.
    base=[c for c in review if c['cost_scenario']=='base']
    roles=dict(highest_observed_win_rate=max((c for c in base if c['win_rate'] is not None),
        key=lambda c:(c['win_rate'],c['closed_episode_count']),default={}).get('strategy_id'),
        maximum_net_return=max(base,key=lambda c:c['net_return'] if c['net_return'] is not None else -np.inf)['strategy_id'],
        minimum_drawdown=min(base,key=lambda c:c['max_drawdown'] if c['max_drawdown'] is not None else np.inf)['strategy_id'])
    pct=lambda x:'N/A' if x is None or pd.isna(x) else f'{100*x:.2f}%'
    num=lambda x:'N/A' if x is None or pd.isna(x) else f'{x:.2f}'
    lines=['# 沪深主板高胜率策略实验室 v1','',f'结论：{verdict}',f'早期预先冻结唯一主候选：{primary or "无"}。',
        '资料限制：EVIDENCE_LIMITED；历史多轮使用（REUSED_HISTORY/REUSED_HOLDOUT），不是全新样本外。',
        '没有达标者时保留原策略配置；这不表示原策略已被证明稳定盈利。本实验没有生产切换。','',
        '## 同口径与指标',
        '新候选16组，另有BASE_ENV和BASE_DEF；252独立账户均完成，两个成本分别检查。',
        '每账户初始100万元，最多3只，固定4%收盘止盈、3%收盘止损、实际入场日起第5交易日开盘到期；不可成交则延后。',
        '全部排除已知风险证券、房地产、原始价低于5元及行业未知证券。历史ST、成分和公司行动不完整，不能宣称历史严格剔除ST或完整分红总收益。',
        '新策略按0.5行业内RS60分位+0.3行业20日分位+0.2标准化CLV排序；无旧综合分门槛。BASE_ENV保持原SCORE排序；BASE_DEF保持原DEFENSIVE排序、波动预算与板块分散。',
        '共同修正：评估全部信号日候选后计划；退出优先级为待退出/风险、止损、到期、止盈。对照同步使用修正；无触发差异样例的成交和NAV与原生实现一致。',
        '佣金双边0.0003、每边最低5元；卖出附加0.0005、每边其他费用0.00001。base/stress每边滑点0.001/0.002，只在真实成交价计一次；不同成本可能改变后续交易路径。',
        '交易胜率按完整经济episode扣费净盈亏；零收益不算赢、未成交不算交易。盈利日占比与交易胜率分别报告；本研究没有收益分类模型准确率。',
        'PF无已观测亏损时保存null并标注不可有限估计；零交易胜率null。盈亏比资格按平均episode净回报，另保存资金损益口径。','',
        '## 早期六独立窗口',
        '净收益列为六窗口算术平均，不是连续复利；回撤列为六窗口最差回撤。资格必须两种成本同时满足原65%胜率等门槛。','',
        '| 策略 | 成本 | 平均净收益 | 汇总胜率 | 平仓数 | Wilson下界 | PF | 最差回撤 | 两成本晋级 |',
        '|---|---|---:|---:|---:|---:|---:|---:|---|']
    for r in selection_rows:
        pf='无已观测亏损' if r['profit_factor_status']=='NO_OBSERVED_LOSSES' else num(r['profit_factor'])
        lines.append(f"| {r['policy_id']} | {r['cost']} | {pct(r['net_return'])} | {pct(r['win_rate'])} | {r['closed_episodes']} | {pct(r['wilson_lower'])} | {pf} | {pct(r['max_drawdown'])} | {r['qualified']} |")
    lines+=['','## 后段连续132交易日','第66日不重置账户、不停止新买；两个66日分段见review_segments.csv，跨段交易按平仓段列完整episode，NAV收益按实际分段。','',
        '| 策略 | 成本 | 累计净收益 | 胜率 | 平仓数 | Wilson下界 | PF | 最大回撤 |',
        '|---|---|---:|---:|---:|---:|---:|---:|']
    for r in later_rows:
        pf='无已观测亏损' if r['profit_factor_status']=='NO_OBSERVED_LOSSES' else num(r['profit_factor'])
        lines.append(f"| {r['policy_id']} | {r['cost']} | {pct(r['net_return'])} | {pct(r['win_rate'])} | {r['closed_episodes']} | {pct(r['wilson_lower'])} | {pf} | {pct(r['max_drawdown'])} |")
    lines+=['','展示角色（仅base描述，不重新选主候选，也不表示资格合格）：',
        f"- 最高观测胜率：{roles['highest_observed_win_rate']}；必须同时查看样本数与Wilson区间。",
        f"- 最大净收益：{roles['maximum_net_return']}。",
        f"- 最低回撤：{roles['minimum_drawdown']}；空仓也可能有最低回撤，不能据此称为最优交易策略。",'',
        '## 失败与推荐结论',
        '主候选失败原因：'+('；'.join(rejections) or '达到本轮相对比较门槛，但资料仍有限。')]
    for s in frozen['summaries']:
        lines.append('- '+s['policy_id']+'：'+('；'.join(s['rejections']) or '早期门槛通过'))
    lines+=['','后段每策略的诊断门槛失败原因见review_summary.csv；只有预先冻结主候选具有正式比较资格。',
        'uncertainty.json包含早期冻结候选与两对照的1000次10交易日同步块bootstrap（seed=20260929），交易按入场日期成组。Wilson与bootstrap不能修复多次选模和历史复用偏差。','',
        '## 完整明细与可追溯性',
        '每账户metrics.json包含年化收益、波动、夏普、盈利日占比、空仓比例/平均仓位、最坏5笔、最大单笔亏损、盈亏比、持有期、费用/滑点、板块集中、失败及未平仓数量。',
        '每账户trades.csv为完整episode；fills.csv.gz、orders.csv.gz、daily_nav.csv.gz、decisions.csv.gz、remaining_positions.csv为原始派生账本导出。',
        'per_sector_summary.csv列行业交易表现；coverage_and_blockers.csv列信号/成交阻碍，feature_coverage.json列数据缺失比例。',
        '原始供应商snapshot与全量特征缓存保留本地；仅上传自主策略/账本派生结果。未调用JEV、LLM、收费推理、新闻或财报服务。','',
        '## 交易日窗口']
    lines += [f'- {name}: {ds[0]}—{ds[-1]}，{len(ds)}交易日。' for name,ds in protocol['windows'].items()]
    lines+=['','绑定：',f"- 数据SHA：{protocol['binding']['dataset_sha256']}",
        f"- 协议SHA：{protocol['binding']['protocol_sha256']}",
        f"- 实现SHA：{protocol['binding']['implementation_sha256']}"]
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n')
    if file_sha256(output/'selection_freeze.json')!=freeze_hash: raise ContractError('report changed primary selection')
    result=dict(status='COMPLETE',verdict=verdict,primary_policy_id=primary,rejections=rejections,
        display_roles=roles,account_count=252,production_activation=False,history_status='EVIDENCE_LIMITED_REUSED_HISTORY',
        selection_freeze_sha256=freeze_hash,binding=protocol['binding'])
    write_json(output/'result.json',result)
    return result
