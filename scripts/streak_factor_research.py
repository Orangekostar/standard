"""Descriptive, temporally split rising-streak research; not a trading strategy."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from core.pipeline.prism_compare_config import file_sha256, implementation_identity, write_json
from core.pipeline.prism_compare_data import experiment_lock
from core.pipeline.rank_rotation_research import load_rotation_config
from core.pipeline.stability_research import _verified_source
from core.pipeline.strategy_search_research import _artifact_records
from core.strategies.formula_v2 import FACTOR_GROUPS
from core.technical_v2.contracts import ContractError

FACTORS=[f'F{i:02d}' for i in range(1,16)]
SCORES=FACTORS+list(FACTOR_GROUPS)+['score3']
FACTOR_NAMES=dict(zip(FACTORS,['20日风险调整动量','60日风险调整动量','均线结构','相对板块强弱',
    '相对市场强弱','20日突破距离','20日区间位置','5日收盘压力','成交额异动方向','20日量价压力',
    '5日量能方向','板块相对市场强弱','板块宽度','市场宽度','市场20日趋势']))
FACTOR_NAMES.update(T='趋势组',R='相对强弱组',S='结构组',V='量价组',C='环境组',score3='原三日总分')
SPLITS={'train':('20240821','20260318'),'validation':('20260319','20260625'),
        'test':('20260626','20260928')}


def label_runs(frame):
    f=frame.sort_values(['code','date']).reset_index(drop=True).copy()
    g=f.groupby('code',sort=False)
    prior=g.comparison_close.shift(1)
    prior_real=g.real_bar.shift(1).eq(True)
    valid=f.real_bar.eq(True)&prior_real&f.comparison_close.gt(0)&prior.gt(0)
    up=valid&f.comparison_close.gt(prior*(1+1e-10))
    f['onset_pool']=valid&~up
    f['label_valid']=valid.copy()
    for k in (1,2,3):
        f['label_valid'] &= g.real_bar.shift(-k).eq(True)&g.comparison_close.shift(-k).gt(0)
    length=np.zeros(len(f),dtype=int)
    ends=np.full(len(f),'',dtype=object)
    censored=np.zeros(len(f),dtype=bool)
    rises=up.to_numpy()
    dates=f.date.to_numpy()
    for indices in g.indices.values():
        n=0
        end=''
        for pos in indices[::-1]:
            length[pos]=n
            ends[pos]=end if n else ''
            censored[pos]=bool(n and end==dates[indices[-1]])
            if rises[pos]:
                if n==0:
                    end=dates[pos]
                n+=1
            else:
                n=0
                end=''
    f['run_length']=length
    f['run_end']=ends
    f['right_censored']=censored
    f['run_start']=g.date.shift(-1)
    f['label3']=(length>=3)&f.label_valid
    f['gross_return3']=g.comparison_close.shift(-3)/g.comparison_open.shift(-1)-1
    return f


def factor_profile(frame,factor):
    x=frame.loc[np.isfinite(frame[factor]),['date','label3',factor]]
    yes=x.loc[x.label3,factor]
    no=x.loc[~x.label3,factor]
    stat=x.groupby(['date','label3'])[factor].agg(['mean','count']).unstack('label3')
    difference=pd.Series(dtype=float)
    if True in stat['mean'].columns and False in stat['mean'].columns:
        enough=(stat['count'][True]>=3)&(stat['count'][False]>=3)
        difference=(stat['mean'][True]-stat['mean'][False]).loc[enough].dropna()
    return dict(factor=factor,name=FACTOR_NAMES.get(factor,factor),positive_count=len(yes),negative_count=len(no),
        positive_mean=float(yes.mean()),negative_mean=float(no.mean()),positive_median=float(yes.median()),
        negative_median=float(no.median()),matched_dates=len(difference),
        matched_mean_difference=float(difference.mean()) if len(difference) else None)


def rule_mask(frame,rules):
    mask=pd.Series(bool(rules),index=frame.index)
    for r in rules:
        value=frame[r['factor']]
        mask &= np.isfinite(value)&(value.ge(r['threshold']) if r['direction']=='high' else value.le(r['threshold']))
    return mask


def evaluate(frame,rules,seed=20260929):
    selected=rule_mask(frame,rules)
    d=frame[['date','label3','gross_return3']].copy()
    d['selected']=selected
    grouped=d.groupby(['date','selected']).label3.agg(['mean','size']).unstack('selected')
    diffs=pd.Series(dtype=float)
    if True in grouped['mean'].columns and False in grouped['mean'].columns:
        enough=(grouped['size'][True]>=5)&(grouped['size'][False]>=20)
        diffs=(grouped['mean'][True]-grouped['mean'][False]).loc[enough].dropna()
    ci=None
    if len(diffs)>=10:
        values=diffs.to_numpy()
        rng=np.random.default_rng(seed)
        starts=rng.integers(0,len(values)-4,size=(1000,(len(values)+4)//5))
        indices=(starts[:,:,None]+np.arange(5)).reshape(1000,-1)[:,:len(values)]
        ci=np.quantile(values[indices].mean(axis=1),[.025,.975]).tolist()
    chosen=frame.loc[selected]
    precision=float(chosen.label3.mean()) if len(chosen) else None
    return dict(universe_rows=len(frame),universe_positives=int(frame.label3.sum()),
        universe_rate=float(frame.label3.mean()) if len(frame) else None,
        selected_rows=len(chosen),selected_dates=int(chosen.date.nunique()),selected_positives=int(chosen.label3.sum()),
        precision=precision,coverage=float(selected.mean()) if len(frame) else 0,
        recall=float(chosen.label3.sum()/frame.label3.sum()) if frame.label3.sum() else None,
        matched_dates=len(diffs),matched_probability_gain=float(diffs.mean()) if len(diffs) else None,
        positive_gain_date_fraction=float((diffs>0).mean()) if len(diffs) else None,
        matched_gain_block95=ci,
        selected_next_open_day3_close_gross_mean=float(chosen.gross_return3.mean()) if len(chosen) else None)


def load_split(root,features,start,end):
    columns=list(dict.fromkeys(['code','date','comparison_close','comparison_open','real_bar','roster_active',
        'is_suspended','prediction_status','score3','forecast_class3','Q03','is_risk_warning',
        'market_state','sector_id',*FACTORS]))
    pieces,episodes=[],[]
    for record in features['feature_chunks']:
        f=pd.read_parquet(record['path'],columns=columns,filters=[('date','<=',end)])
        f=label_runs(f)
        for group,ids in FACTOR_GROUPS.items():
            f[group]=50+50*f[list(ids)].mean(axis=1)
        f[FACTORS]=50+50*f[FACTORS]
        span=f.run_start.ge(start)&f.run_start.le(end)&f.onset_pool
        valid=(f.roster_active.eq(True)&f.is_suspended.eq(False)&~f.is_risk_warning.eq(True)
            &f.prediction_status.eq('OK')&f.Q03.ge(50000000)&np.isfinite(f.score3))
        f['research_eligible']=valid
        keep=['code','date','run_start','run_end','run_length','right_censored','label3',
              'research_eligible','gross_return3','Q03','forecast_class3','market_state','sector_id',*SCORES]
        episodes.append(f.loc[span&f.label3,keep])
        pieces.append(f.loc[span&f.label_valid&valid,keep])
    return pd.concat(pieces,ignore_index=True),pd.concat(episodes,ignore_index=True)


def discover(frame):
    candidates=[]
    for factor in SCORES:
        for direction,q in [('low',.2),('high',.8)]:
            threshold=float(frame[factor].quantile(q))
            rule=dict(factor=factor,direction=direction,threshold=threshold)
            stat=evaluate(frame,[rule])
            candidates.append(dict(**rule,**stat))
    eligible=[r for r in candidates if r['selected_rows']>=1000 and r['matched_dates']>=60
        and r['matched_gain_block95'] and r['matched_gain_block95'][0]>0
        and r['positive_gain_date_fraction']>=.55]
    eligible.sort(key=lambda r:(-r['matched_probability_gain'],r['factor'],r['direction']))
    chosen,groups=[],set()
    for r in eligible:
        group=next((g for g,ids in FACTOR_GROUPS.items() if r['factor'] in ids or r['factor']==g),'TOTAL')
        if group in groups:
            continue
        chosen.append({k:r[k] for k in ('factor','direction','threshold')})
        groups.add(group)
        if len(chosen)==2:
            break
    return chosen,candidates


def run(root,output):
    root,output=Path(root).resolve(),Path(output).resolve()
    if root==output or root in output.parents or output in root.parents:
        raise ContractError('research output must be separate from inputs')
    manifest,features=_verified_source(root,load_rotation_config())
    identity=implementation_identity()
    binding=dict(dataset_sha256=file_sha256(root/'rank_dataset_manifest.json'),implementation_hash=identity['implementation_hash'])
    with experiment_lock(output):
        if (output/'run_manifest.json').exists():
            old=json.loads((output/'run_manifest.json').read_text())
            if old['binding']!=binding or old['files']!=_artifact_records(output):
                raise ContractError('archive differs; preserve and use a new directory')
            return {**old['result'],'reused':True}
        if any(p.name!='.compare.lock' for p in output.iterdir()):
            raise ContractError('incomplete output preserved; choose a fresh directory')
        protocol=dict(label='AT_LEAST_THREE_STRICTLY_RISING_ADJUSTED_CLOSES_AFTER_NONRISING_ANCHOR',
            missing='MISSING_REAL_BAR_BREAKS_STREAK_AND_INVALIDATES_THREE_DAY_LABEL',splits=SPLITS,
            factor_scale='F_AND_GROUP_SCORES_50_PLUS_50_TIMES_NATIVE_VALUE_SCORE3_UNCHANGED',
            thresholds='TRAIN_ONLY_20TH_AND_80TH_PERCENTILES_42_SINGLE_FACTOR_CANDIDATES',
            rule='UP_TO_TWO_BEST_DISTINCT_GROUP_RULES_CONJOINED_FROZEN_BEFORE_VALIDATION',
            selection='TRAIN_MATCHED_GAIN_CI_LOWER_POSITIVE_60_DATES_1000_ROWS_55PCT_POSITIVE_DATES',
            evaluation='SAME_DATE_SELECTED_VS_COMPLEMENT_AT_LEAST_5_AND_20_ROWS',
            uncertainty='1000_MOVING_FIVE_DATE_BLOCK_BOOTSTRAPS_SEED_20260929_NOT_MULTIPLE_TEST_ADJUSTED',
            validation='BOTH_LATER_SPLITS_20_MATCHED_DATES_100_SELECTED_CI_LOWER_POSITIVE_AND_PRECISION_LIFT_1_2',
            eligibility='ACTIVE_MAINBOARD_NONST_KNOWN_NOT_SUSPENDED_VALID_SCORE_ADV20_AT_LEAST_50M',
            production_activation=False,api_calls=0,reused_history=True)
        write_json(output/'protocol.json',dict(binding=binding,protocol=protocol,source_identity=identity),immutable=True)
        names=pd.read_parquet(root/'roster.parquet')[['code','name']]
        outcomes,profiles={},[]
        rules=[]
        for split,(start,end) in SPLITS.items():
            frame,episodes=load_split(root,features,start,end)
            if split=='train':
                rules,candidates=discover(frame)
                pd.DataFrame(candidates).to_csv(output/'training_candidates.csv',index=False)
                write_json(output/'frozen_rule.json',dict(rules=rules,frozen_at=datetime.now(timezone.utc).isoformat(),
                    binding=binding,training_candidates_sha256=file_sha256(output/'training_candidates.csv')),immutable=True)
            else:
                assert (output/'frozen_rule.json').exists()
            stat=evaluate(frame,rules)
            stat['episodes_all']=len(episodes)
            stat['episode_stocks_all']=int(episodes.code.nunique())
            stat['episodes_5plus']=int(episodes.run_length.ge(5).sum())
            stat['eligible_episode_count']=int(episodes.research_eligible.sum())
            outcomes[split]=stat
            profiles.extend(dict(split=split,**factor_profile(frame,factor)) for factor in SCORES)
            episodes.merge(names,on='code',validate='many_to_one').to_csv(output/f'{split}_all_streaks.csv.gz',index=False)
            frame.assign(rule_selected=rule_mask(frame,rules)).to_parquet(output/f'{split}_labeled_population.parquet',index=False)
            print(json.dumps(dict(event='streak_split_complete',split=split,rules=rules,**stat)),flush=True)
        def passes(s):
            return (s['selected_rows']>=100 and s['matched_dates']>=20 and s['matched_gain_block95']
                and s['matched_gain_block95'][0]>0 and s['precision']>=1.2*s['universe_rate'])
        verdict='CANDIDATE_SIGNAL_REQUIRES_EXECUTION_BACKTEST' if rules and all(passes(outcomes[s]) for s in ('validation','test')) else 'NO_RELIABLE_TRANSFERABLE_RULE_FOUND'
        result=dict(verdict=verdict,rules=rules,outcomes=outcomes,reused=False)
        pd.DataFrame(profiles).to_csv(output/'factor_profiles.csv',index=False,encoding='utf-8-sig')
        _report(output,result,profiles)
        write_json(output/'run_manifest.json',dict(status='COMPLETE',binding=binding,result=result,files=_artifact_records(output)),immutable=True)
        return result


def _report(output,result,profiles):
    def pct(v):
        return 'N/A' if v is None else f'{v*100:.2f}%'
    lines=['# 连涨前因子研究','',f"结论：{result['verdict']}",'',
        '连涨=连续至少3个实际交易日复权收盘上涨；同一段只记一次，信号取第一天上涨前一日。普通对照也必须当日没有上涨，且未来3天数据完整。停牌/缺失中断连涨。期末未结束的已确认连涨标记右截尾。',
        '主板非ST冻结股票池；规律研究另要求前日20日均成交额≥5000万元、有效预测、在市且未停牌。全量连涨清单保留不符合研究流动性/预测条件的记录并标记research_eligible。',
        'F01—F15及组分数映射到0—100（50为原值0），不是胜率；score3沿用原分数。逐只逐段数据见test_all_streaks.csv.gz。',
        '','## 训练期冻结条件','']
    lines += [f"- {r['factor']} {FACTOR_NAMES[r['factor']]} {'≥' if r['direction']=='high' else '≤'} {r['threshold']:.2f}分" for r in result['rules']]
    lines += ['','多个条件同时满足；没有合格条件则不选股。规则仅由训练段决定，后段不改参数。','',
        '## 分段检验','',
        '| 区间 | 连涨段数/股票数（全池） | 可研究基准连涨率 | 规则命中率 | 入选样本数 | 同日命中率增量与95%区间 |',
        '|---|---|---|---|---|---|']
    for split,s in result['outcomes'].items():
        ci=s['matched_gain_block95']
        text=f"{pct(s['matched_probability_gain'])} [{pct(ci[0])}, {pct(ci[1])}]" if ci else '样本不足'
        lines.append(f"| {split} {SPLITS[split][0]}—{SPLITS[split][1]} | {s['episodes_all']}/{s['episode_stocks_all']} | {pct(s['universe_rate'])} | {pct(s['precision'])} | {s['selected_rows']} | {text} |")
    lines += ['','## 2026年6—9月因子对照','',
        '| 因子 | 连涨前均分 | 未连涨均分 | 同日配对均分差 |','|---|---|---|---|']
    for r in sorted([r for r in profiles if r['split']=='test'],key=lambda r:-abs(r['matched_mean_difference'] or 0)):
        diff=r['matched_mean_difference']
        lines.append(f"| {r['factor']} {r['name']} | {r['positive_mean']:.2f} | {r['negative_mean']:.2f} | {diff:.2f} |" if diff is not None else f"| {r['factor']} | N/A | N/A | N/A |")
    lines += ['','这是历史识别研究，不是可执行组合回测。下一开盘到第三日收盘的毛收益仅作诊断，未计费用、滑点、涨停买不到、仓位与退出约束，不能称为策略净收益。',
        '同日对照减少行情好坏导致的伪相似；区间按日期块自助法估算，非多重检验校正。三段区间历史均曾用于此前研究，后段不是全新独立样本。历史ST、公司行动与成分资料缺失限制仍在，不能据此保证连涨或直接实盘套用。']
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--experiment-root',type=Path,required=True)
    parser.add_argument('--output-root',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(run(args.experiment_root,args.output_root)))
