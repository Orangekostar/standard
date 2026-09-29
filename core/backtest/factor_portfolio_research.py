from __future__ import annotations

import time
from contextlib import ExitStack
from dataclasses import dataclass
from decimal import Decimal
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

from core.backtest.prism_compare_engine import _gzip_writer
from core.backtest.stability_research_v2 import EnvironmentPolicy,_EnvironmentReplay,environment_mask
from core.backtest.strategy_search_v2 import SearchMarket
from core.technical_v2.contracts import ContractError

REAL_ESTATE='801180.SI'


@dataclass(frozen=True)
class FactorPolicy(EnvironmentPolicy):
    holding_sessions: int = 5
    gate: str = 'SECTOR20_BREADTH'
    range_scale: float = .5
    ranking: str = 'SCORE'
    vol_control: bool = False
    diversify: bool = False
    min_price: float = 5.

    def __post_init__(self):
        super().__post_init__()
        if (self.holding_sessions!=5 or self.gate!='SECTOR20_BREADTH' or self.range_scale!=.5
                or self.ranking not in {'SCORE','LEADER','DEFENSIVE'}
                or type(self.vol_control) is not bool or type(self.diversify) is not bool
                or not np.isfinite(self.min_price) or self.min_price<=0):
            raise ContractError('invalid factor portfolio policy')

    @property
    def policy_id(self):
        return f'FS_{self.ranking}_{"VOL15" if self.vol_control else "REGIME"}_D{int(self.diversify)}_P{self.min_price:g}'


def policy_grid(min_price=5.):
    return [FactorPolicy(ranking=r,vol_control=v,diversify=d,min_price=min_price)
            for r,v,d in product(('SCORE','LEADER','DEFENSIVE'),(False,True),(False,True))]


def user_universe(frame,min_price):
    sector=frame.sector_id.fillna('').astype(str)
    return (np.isfinite(frame.valuation_close)&frame.valuation_close.ge(min_price)
            &sector.ne('')&sector.ne('UNKNOWN')&sector.ne(REAL_ESTATE))


def ranking_values(frame,ranking):
    score=frame.score3
    if ranking=='SCORE': return score
    relative=50+25*(frame.F04.clip(-1,1)+frame.F05.clip(-1,1))
    sector=25+25*np.tanh(frame.sector_log_return20/.1)+50*frame.sector_breadth20
    valid=np.isfinite(frame.F04)&np.isfinite(frame.F05)
    if ranking=='LEADER': value=.4*score+.3*relative+.3*sector
    elif ranking=='DEFENSIVE':
        value=.3*score+.2*relative+.2*sector+.3*100*(1-(frame.Q02/.04).clip(0,1))
        valid &= np.isfinite(frame.Q02)&frame.Q02.gt(0)
    else: raise ContractError('unknown portfolio ranking')
    return value.where(valid)


class FactorMarket(SearchMarket):
    def __init__(self,base,policy):
        self.base,self.policy,self.frames=base,policy,base.frames
        self.cache={}

    def ranks(self,date,candidate):
        if date not in self.cache:
            native=self.base.ranks(date,candidate)
            f=self.frames[date].loc[native.index]
            good=environment_mask(f,self.policy.gate)&user_universe(f,self.policy.min_price)
            if self.policy.vol_control: good &= np.isfinite(f.Q02)&f.Q02.gt(0)
            score=ranking_values(f,self.policy.ranking).where(good).dropna()
            ranked=pd.DataFrame({'rank_score':score,'code_sort':score.index})
            ranked=ranked.sort_values(['rank_score','code_sort'],ascending=[False,True])
            if self.policy.diversify:
                sectors=f.loc[ranked.index,'sector_id']
                ranked=ranked.loc[~sectors.duplicated()]
            self.cache[date]=ranked.rank_score
        return self.cache[date]


class FactorReplay(_EnvironmentReplay):
    def search_day(self,market,date,next_date,writer):
        self.signal_sectors=market.frames[date].sector_id.to_dict()
        self.planned_sectors=set()
        return super().search_day(market,date,next_date,writer)

    def _entry_blockers(self,code,row,date):
        reasons=super()._entry_blockers(code,row,date)
        if self.environment_policy.diversify:
            sector=row.get('sector_id')
            survivors=self._held()-set(self.exit_reasons)
            occupied={self.signal_sectors.get(c) for c in survivors}|self.planned_sectors
            if sector in occupied: reasons.append('SECTOR_ALREADY_ALLOCATED')
            if not reasons: self.planned_sectors.add(sector)
        return reasons

    def _execute(self,date,rows):
        allowed=[]
        for buy in self.pending_buys:
            row=rows.get(buy.code,{})
            opening=row.get('execution_open')
            sector=row.get('sector_id')
            reason=None
            if sector is None or pd.isna(sector) or sector in {'','UNKNOWN',REAL_ESTATE}:
                reason='USER_SECTOR_EXCLUSION_AT_OPEN'
            elif opening is None or not np.isfinite(opening) or opening<self.environment_policy.min_price:
                reason='USER_MIN_PRICE_AT_OPEN'
            if reason:
                self.attempts.append(dict(date=date,code=buy.code,side='BUY',order_id=None,status='BLOCKED',reason=reason))
            else: allowed.append(buy)
        self.pending_buys=tuple(allowed)
        return super()._execute(date,rows)

    def _buy_budget(self,code):
        p=self.environment_policy
        if p.diversify:
            sector=self.previous_rows[code].get('sector_id')
            if sector in {self.previous_rows.get(c,{}).get('sector_id') for c in self._held()}:
                return 0
        budget=super()._buy_budget(code)
        if p.vol_control:
            row=self.previous_rows[code];vol=row.get('Q02')
            if vol is None or not np.isfinite(vol) or vol<=0: return 0
            scale=Decimal('.5') if row.get('market_state')=='RANGE' else Decimal(1)
            risk=min(Decimal(1),Decimal('.015')/Decimal(str(vol)))
            budget=min(budget,int(Decimal(self.pending_budget)*scale*risk))
        return budget

    def _metric_fields(self,nav,fills,closed,remaining):
        return {**super()._metric_fields(nav,fills,closed,remaining),
            'prediction_method':'FIXED_FACTOR_SECTOR_RANK_PORTFOLIO_RESEARCH',
            'ranking_mode':self.environment_policy.ranking,
            'volatility_target_daily':.015 if self.environment_policy.vol_control else None,
            'sector_rule':'NO_NEW_DUPLICATE_SIGNAL_SECTOR' if self.environment_policy.diversify else 'NONE',
            'excluded_sector':REAL_ESTATE,'minimum_unadjusted_price':self.environment_policy.min_price}


def replay_factor(market,*,policy,sessions,config,cost_scenario,output_dir,run_id,
                  split='selection_1',scope_flags=(),corporate_actions=None):
    if (sessions!=sorted(set(sessions)) or len(sessions)<=12 or not isinstance(market,SearchMarket)
            or not set(sessions).issubset(market.frames) or cost_scenario not in config['costs']['slippage_cases']):
        raise ContractError('invalid factor replay calendar, market or costs')
    output=Path(output_dir).resolve()
    if output.exists() and any(output.iterdir()): raise ContractError('factor replay needs fresh output')
    output.mkdir(parents=True,exist_ok=True);started=time.perf_counter()
    replay=FactorReplay(config,sessions,cost_scenario,output,run_id+':'+policy.policy_id,split,
        scope_flags,corporate_actions,candidate=policy.candidate,tail_sessions=11,environment_policy=policy)
    wrapped=FactorMarket(market,policy)
    with ExitStack() as stack:
        writer=_gzip_writer(stack,output/'decisions.csv.gz',
            ['date','code','score','forecast_class','rank_score','action','reasons'])
        for i,date in enumerate(sessions):
            replay.search_day(wrapped,date,sessions[i+1] if i+1<len(sessions) else None,writer)
    return replay.finish(started)


def select_frontier(summaries,baseline_id=None):
    baseline_id=baseline_id or FactorPolicy().policy_id
    baseline=next(s for s in summaries if s['policy_id']==baseline_id)
    floor=max(0.,.5*(baseline['robust_mean_return'] or 0.))
    eligible=[s for s in summaries if s['qualified'] and s['robust_mean_return']>=floor]
    frontier=[s for s in eligible if not any(
        t['robust_mean_return']>=s['robust_mean_return'] and t['worst_drawdown']<=s['worst_drawdown']
        and (t['robust_mean_return']>s['robust_mean_return'] or t['worst_drawdown']<s['worst_drawdown'])
        for t in eligible)]
    def best(key): return sorted(frontier,key=key)[0]['policy_id'] if frontier else None
    high=best(lambda s:(-s['robust_mean_return'],s['worst_drawdown'],s['policy_id']))
    low=best(lambda s:(s['worst_drawdown'],-s['robust_mean_return'],s['policy_id']))
    balanced=best(lambda s:(-s['robust_mean_return']/max(s['worst_drawdown'],.005),
        -s['robust_lower_quartile'],-s['robust_mean_return'],s['policy_id']))
    complete=[s for s in summaries if s['robust_mean_return'] is not None and s['worst_drawdown'] is not None]
    diagnostic=(sorted(complete,key=lambda s:(-s['robust_mean_return']/max(s['worst_drawdown'],.005),s['policy_id']))[0]['policy_id']
                if not balanced and complete else None)
    review=sorted({baseline_id,*[x for x in (high,low,balanced,diagnostic) if x]})
    return dict(baseline_policy_id=baseline_id,high_return_policy_id=high,low_drawdown_policy_id=low,
        balanced_policy_id=balanced,diagnostic_policy_id=diagnostic,pareto_policy_ids=sorted(s['policy_id'] for s in frontier),
        review_policy_ids=review,minimum_robust_mean_return=floor)
