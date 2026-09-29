from __future__ import annotations

import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from core.backtest.prism_compare_engine import _gzip_writer
from core.backtest.stability_research_v2 import _EnvironmentReplay
from core.backtest.strategy_search_v2 import Candidate, SearchMarket
from core.data.symbols import is_buyable_mainboard_ts_code, is_risk_warning_name
from core.technical_v2.contracts import ContractError

MODES=('BREAKOUT','WASH_BREAKOUT','BASE_WASH_BREAKOUT','CONTEXT_SEQUENCE')


@dataclass(frozen=True)
class PhasePolicy:
    mode: str
    gate: str = 'PRICE_VOLUME'
    range_scale: float = .5

    def __post_init__(self):
        if self.mode not in MODES or self.gate!='PRICE_VOLUME' or self.range_scale!=.5:
            raise ContractError('unsupported fixed price-volume policy')

    @property
    def policy_id(self):
        return f'PV_{self.mode}_H5_R50'

    @property
    def candidate(self):
        # Reuse only the bounded exit implementation; entry is entirely phase-based.
        return Candidate('TREND_PULLBACK',1,5,.04)


def phase_features(frame):
    f=frame.sort_values(['code','date']).reset_index(drop=True).copy()
    if f.duplicated(['code','date']).any():
        raise ContractError('duplicate price-volume feature rows')
    g=f.groupby('code',sort=False)
    def roll(column,n,op='mean',shift=0):
        return g[column].transform(lambda s:s.shift(shift).rolling(n,min_periods=n).agg(op))
    real=f.real_bar.eq(True)
    f['_pv_close']=f.comparison_close.where(real&f.comparison_close.gt(0))
    f['_pv_amount']=f.amount_cny.where(real&f.amount_cny.gt(0))
    g=f.groupby('code',sort=False)
    c=f['_pv_close']
    r1=c/g['_pv_close'].shift(1)-1
    r3=c/g['_pv_close'].shift(3)-1
    r20=c/g['_pv_close'].shift(20)-1
    width=f.comparison_high-f.comparison_low
    clv=((2*c-f.comparison_high-f.comparison_low)/width).where(width.gt(0),0.)
    f['_pv_money_pressure']=clv*f['_pv_amount']
    g=f.groupby('code',sort=False)
    f['pv_low20']=roll('_pv_close',20,'min')
    f['pv_high20']=roll('_pv_close',20,'max')
    f['pv_high10prev']=roll('_pv_close',10,'max',1)
    f['pv_amount_ratio']=f['_pv_amount']/roll('_pv_amount',20,shift=1)
    f['pv_contract_ratio']=roll('_pv_amount',3)/roll('_pv_amount',20,shift=3)
    f['pv_base_amount_ratio']=roll('_pv_amount',5)/roll('_pv_amount',20,shift=5)
    f['pv_pressure20']=roll('_pv_money_pressure',20,'sum')/roll('_pv_amount',20,'sum')
    f['pv_market5']=f.market_index/g.market_index.shift(5)-1
    f['pv_sector5']=f.sector_index/g.sector_index.shift(5)-1
    context=f.market_context_status.eq('OK')&f.sector_context_status.eq('OK')
    f['pv_background_down']=context&((f.pv_market5<0)|(f.pv_sector5<0))
    f['pv_background_up']=context&(f.pv_market5>0)&(f.pv_sector5>0)
    f['pv_base']=(real&r20.abs().le(.05)&(f.pv_high20/f.pv_low20-1).le(.12)
        &f.pv_base_amount_ratio.ge(1.05)&f.pv_pressure20.ge(.1)).fillna(False)
    drawdown=c/f.pv_high20-1
    f['pv_wash_price']=(real&r3.lt(0)&drawdown.between(-.12,-.03)&c.ge(.95*roll('_pv_close',60))).fillna(False)
    f['pv_wash']=(f.pv_wash_price&f.pv_contract_ratio.le(.8)).fillna(False)
    f['pv_break_price']=(real&c.gt(f.pv_high10prev)&r1.gt(0)&r1.le(.07)&clv.ge(.5)).fillna(False)
    f['pv_breakout']=(f.pv_break_price&f.pv_amount_ratio.ge(1.3)).fillna(False)
    f['_pv_base_floor']=f.pv_low20.where(f.pv_base)
    g=f.groupby('code',sort=False)
    floor=g['_pv_base_floor'].transform(lambda s:s.ffill(limit=39).shift(1))
    f['pv_wash_after_base']=(f.pv_wash&floor.notna()&c.ge(.97*floor)).fillna(False)
    f['pv_weak_wash_after_base']=f.pv_wash_after_base&f.pv_background_down
    g=f.groupby('code',sort=False)
    for source,target in [('pv_wash','pv_recent_wash'),('pv_wash_after_base','pv_recent_sequence'),
                          ('pv_weak_wash_after_base','pv_recent_weak_sequence')]:
        f[target]=g[source].transform(lambda s:s.shift(1).rolling(10,min_periods=1).max()).eq(1)
    for flag in ('pv_base','pv_wash','pv_breakout','pv_wash_price','pv_break_price'):
        f[flag+'_event']=f[flag]&~g[flag].shift(1).eq(True)
    return f.drop(columns=[c for c in f if c.startswith('_pv_')])


def research_universe(frame):
    return (frame.real_bar.eq(True)&frame.roster_active.eq(True)&frame.instrument_type.eq('stock')
        &frame.listing_board.isin(['MAIN_SH','MAIN_SZ'])&frame.code.map(is_buyable_mainboard_ts_code)
        &~frame.name.fillna('').map(is_risk_warning_name)&~frame.is_risk_warning.eq(True)
        &frame.is_suspended.eq(False)&frame.prediction_status.eq('OK')&np.isfinite(frame.score3)
        &frame.Q03.ge(50000000)&frame.adj_factor.gt(0)&frame.valuation_close.gt(0))


def entry_mask(frame,mode):
    if mode not in MODES:
        raise ContractError('unknown phase entry mode')
    mask=research_universe(frame)&frame.market_state.isin(['RANGE','TREND_EXPANSION'])&frame.pv_breakout.eq(True)
    if mode=='WASH_BREAKOUT':
        mask &= frame.pv_recent_wash.eq(True)
    elif mode=='BASE_WASH_BREAKOUT':
        mask &= frame.pv_recent_sequence.eq(True)
    elif mode=='CONTEXT_SEQUENCE':
        mask &= frame.pv_recent_weak_sequence.eq(True)&frame.pv_background_up.eq(True)
    return mask.fillna(False)


class PhaseMarket:
    def __init__(self,market,policy):
        self.frames,self.policy=market.frames,policy

    def ranks(self,date,candidate):
        f=self.frames[date]
        eligible=f.loc[entry_mask(f,self.policy.mode),['code','score3']].rename(columns={'code':'code_sort'})
        return eligible.sort_values(['score3','code_sort'],ascending=[False,True]).score3


class PhaseReplay(_EnvironmentReplay):
    def _entry_reason(self):
        return 'PRICE_VOLUME_'+self.environment_policy.mode

    def _metric_fields(self,nav,fills,closed,remaining):
        result=super()._metric_fields(nav,fills,closed,remaining)
        result.pop('candidate')
        result['phase_policy']=asdict(self.environment_policy)
        result['exit_rules']=dict(holding_sessions=5,close_take_profit=.04,close_stop_loss=.03)
        result['prediction_method']='OBSERVABLE_PRICE_VOLUME_SEQUENCE_NOT_INSTITUTION_IDENTITY'
        return result


def replay_phase(market,*,policy,sessions,config,cost_scenario,output_dir,run_id,
                 split='selection_1',scope_flags=(),corporate_actions=None):
    output=Path(output_dir).resolve()
    if (sessions!=sorted(set(sessions)) or len(sessions)<=12 or not set(sessions).issubset(market.frames)
            or cost_scenario not in config['costs']['slippage_cases']):
        raise ContractError('invalid phase replay calendar or cost')
    if output.exists() and any(output.iterdir()):
        raise ContractError('phase replay output must be fresh')
    output.mkdir(parents=True,exist_ok=True)
    started=time.perf_counter()
    replay=PhaseReplay(config,sessions,cost_scenario,output,run_id+':'+policy.policy_id,split,
        scope_flags,corporate_actions,candidate=policy.candidate,tail_sessions=11,environment_policy=policy)
    with ExitStack() as stack:
        writer=_gzip_writer(stack,output/'decisions.csv.gz',
            ['date','code','score','forecast_class','rank_score','action','reasons'])
        wrapped=PhaseMarket(market,policy)
        for i,date in enumerate(sessions):
            replay.search_day(wrapped,date,sessions[i+1] if i+1<len(sessions) else None,writer)
    return replay.finish(started)
