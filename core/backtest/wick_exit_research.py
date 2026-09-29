from __future__ import annotations

import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from core.backtest.prism_compare_engine import _gzip_writer
from core.backtest.stability_research_v2 import EnvironmentPolicy, _EnvironmentMarket, _EnvironmentReplay
from core.backtest.strategy_search_v2 import SearchMarket
from core.technical_v2.contracts import ContractError


@dataclass(frozen=True)
class WickPolicy(EnvironmentPolicy):
    holding_sessions: int = 5
    gate: str = 'SECTOR20_BREADTH'
    range_scale: float = .5
    mode: str = 'WICK'

    def __post_init__(self):
        super().__post_init__()
        if (self.holding_sessions!=5 or self.gate!='SECTOR20_BREADTH' or self.range_scale!=.5
                or self.mode not in {'FIXED','NO_TP','WICK'}):
            raise ContractError('unsupported fixed wick-exit experiment')

    @property
    def policy_id(self): return 'EXIT_'+self.mode+'_H5_S3_R50'


def wick_features(frame):
    f=frame.sort_values(['code','date']).reset_index(drop=True).copy()
    if f.duplicated(['code','date']).any(): raise ValueError('duplicate wick feature rows')
    o,h,l,c=(pd.to_numeric(f['comparison_'+key],errors='coerce') for key in ('open','high','low','close'))
    valid=(f.real_bar.eq(True)&np.isfinite(o)&np.isfinite(h)&np.isfinite(l)&np.isfinite(c)
           &o.gt(0)&h.gt(0)&l.gt(0)&c.gt(0)&h.ge(pd.concat([o,c],axis=1).max(axis=1))
           &l.le(pd.concat([o,c],axis=1).min(axis=1)))
    f['_wick_high']=h.where(valid);f['_wick_close']=c.where(valid)
    g=f.groupby('code',sort=False)
    previous_high=g['_wick_high'].transform(lambda s:s.shift(1).rolling(20,min_periods=20).max())
    previous_close=g['_wick_close'].shift(1)
    width=h-l;upper=h-pd.concat([o,c],axis=1).max(axis=1);body=(c-o).abs()
    f['wick_prior_high20']=previous_high
    f['wick_high_relative20']=h/previous_high
    f['wick_fraction']=(upper/width).where(width.gt(0))
    f['wick_intraday_rise']=h/previous_close-1
    f['high_wick']=(valid&width.gt(0)&previous_high.gt(0)&previous_close.gt(0)
        &h.ge(.98*previous_high)&h.ge(1.03*previous_close)&upper.ge(.5*width)
        &upper.ge(2*body)).fillna(False)
    return f.drop(columns=['_wick_high','_wick_close'])


class WickReplay(_EnvironmentReplay):
    def _submit_exits(self,date,next_date,rows,sells):
        """Modify the shared sell map before its submission and capacity calculation."""
        mode=self.environment_policy.mode
        if mode!='FIXED':
            for code in list(sells):
                if sells[code]=='SEARCH_TAKE_PROFIT':
                    # This experiment never queues fixed-profit orders. Do not cancel a real pending exit.
                    if code in self.exit_reasons:
                        raise ContractError('unexpected pending fixed-profit exit in no-TP experiment')
                    del sells[code]
            for lot in self.portfolio.lots():
                if lot.status!='OPEN': continue
                code=lot.code
                if code in self.exit_reasons or sells.get(code) in {'RISK_WARNING_SECURITY','SEARCH_STOP_LOSS'}:
                    continue
                if mode=='WICK' and rows.get(code,{}).get('high_wick') is True:
                    sells[code]='HIGH_WICK_EXIT'
                elif self.date_index[next_date]-self.date_index[lot.entry_date]>=self.environment_policy.holding_sessions:
                    # The inherited fixed-TP branch may have hidden an expiry on this same close.
                    sells.setdefault(code,'SEARCH_MAX_HOLDING')
        return super()._submit_exits(date,next_date,rows,sells)

    def _metric_fields(self,nav,fills,closed,remaining):
        result=super()._metric_fields(nav,fills,closed,remaining)
        result.pop('candidate')
        result['exit_rules']=dict(fixed_take_profit=.04 if self.environment_policy.mode=='FIXED' else None,
            close_stop_loss=.03,maximum_holding_sessions=5,high_wick=self.environment_policy.mode=='WICK',
            priority='PENDING_EXIT_THEN_RISK_THEN_STOP_THEN_WICK_THEN_EXPIRY',
            timing='CLOSE_SIGNAL_NEXT_EXECUTABLE_OPEN')
        result['high_wick_exit_count']=int(closed.exit_reason.eq('HIGH_WICK_EXIT').sum())
        return result


def replay_wick(market,*,policy,sessions,config,cost_scenario,output_dir,run_id,
                split='selection_1',scope_flags=(),corporate_actions=None):
    if (sessions!=sorted(set(sessions)) or len(sessions)<=12 or not isinstance(market,SearchMarket)
            or not set(sessions).issubset(market.frames) or cost_scenario not in config['costs']['slippage_cases']):
        raise ContractError('invalid wick replay calendar, market or costs')
    output=Path(output_dir).resolve()
    if output.exists() and any(output.iterdir()): raise ContractError('wick replay requires a fresh cell')
    output.mkdir(parents=True,exist_ok=True);started=time.perf_counter()
    replay=WickReplay(config,sessions,cost_scenario,output,run_id+':'+policy.policy_id,split,
        scope_flags,corporate_actions,candidate=policy.candidate,tail_sessions=11,environment_policy=policy)
    wrapped=_EnvironmentMarket(market,policy)
    with ExitStack() as stack:
        writer=_gzip_writer(stack,output/'decisions.csv.gz',
            ['date','code','score','forecast_class','rank_score','action','reasons'])
        for i,date in enumerate(sessions):
            replay.search_day(wrapped,date,sessions[i+1] if i+1<len(sessions) else None,writer)
    return replay.finish(started)
