"""Episode-based reporting and preregistered high-win qualification.

Only completely closed economic episodes enter trade success statistics;
unresolved positions and incomplete NAV remain explicit qualification failures.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from core.technical_v2.contracts import ContractError

Z95 = 1.959963984540054


def wilson(wins, count):
    if count < 0 or wins < 0 or wins > count:
        raise ContractError('invalid wins/count')
    if not count:
        return [None,None]
    p = wins/count
    d = 1+Z95**2/count
    center = (p+Z95**2/(2*count))/d
    half = Z95*math.sqrt(p*(1-p)/count+Z95**2/(4*count**2))/d
    return [max(0.,center-half),min(1.,center+half)]


def episode_statistics(trades):
    f = trades.copy()
    if len(f):
        if not f.status.eq('CLOSED').all() or f.lot_id.duplicated().any():
            raise ContractError('episode input must contain unique completely closed economic lots')
        if f.entry_cost_cents.le(0).any() or not np.isfinite(f.realized_pnl_cents).all():
            raise ContractError('unaccountable closed episode')
        # A new buy is forbidden while any shares or entitlements remain.
        # Consequently closed_trades' economic root is exactly one episode.
        for _,g in f.groupby('code'):
            g = g.sort_values('entry_date')
            if (g.entry_date.iloc[1:].to_numpy() <= g.closed_at.iloc[:-1].to_numpy()).any():
                raise ContractError('overlapping episodes: aggregate economic lineage before reporting')
        f['net_episode_return'] = f.realized_pnl_cents/f.entry_cost_cents
    else:
        f['net_episode_return'] = pd.Series(dtype=float)
    pnl = f.realized_pnl_cents.astype(float)/100
    ret = f.net_episode_return
    wins, losses = int(pnl.gt(0).sum()),int(pnl.lt(0).sum())
    positive_sum, negative_sum = float(pnl.clip(lower=0).sum()),float(-pnl.clip(upper=0).sum())
    avg_win = float(pnl[pnl>0].mean()) if wins else None
    avg_loss = float(pnl[pnl<0].mean()) if losses else None
    avg_win_ret = float(ret[pnl>0].mean()) if wins else None
    avg_loss_ret = float(ret[pnl<0].mean()) if losses else None
    return dict(closed_episode_count=len(f),wins=wins,loss_count=losses,zero_count=len(f)-wins-losses,
        win_rate=wins/len(f) if len(f) else None,wilson95=wilson(wins,len(f)),
        positive_pnl_cny=positive_sum,negative_pnl_abs_cny=negative_sum,
        profit_factor=positive_sum/negative_sum if losses else None,
        profit_factor_status='FINITE' if losses else 'NO_OBSERVED_LOSSES' if wins else 'NO_POSITIVE_OR_NEGATIVE_EPISODES',
        average_net_win_cny=avg_win,average_net_loss_cny=avg_loss,
        average_net_win_return=avg_win_ret,average_net_loss_return=avg_loss_ret,
        payoff_ratio_cny=avg_win/abs(avg_loss) if wins and losses else None,
        payoff_ratio_return=avg_win_ret/abs(avg_loss_ret) if wins and losses else None,
        payoff_status='FINITE' if wins and losses else 'NO_OBSERVED_LOSSES' if wins else 'NO_OBSERVED_WINS',
        mean_episode_net_return=float(ret.mean()) if len(f) else None,
        sum_episode_net_return=float(ret.sum()),
        sum_positive_episode_return=float(ret[pnl>0].sum()),sum_negative_episode_return=float(ret[pnl<0].sum()),
        worst_five_episodes=f.sort_values('net_episode_return').head(5)[
            ['code','entry_date','closed_at','realized_pnl_cents','net_episode_return']].to_dict('records'),
        maximum_episode_loss_return=float(max(0,-ret.min())) if len(f) else None,
        maximum_episode_loss_cny=float(max(0,-pnl.min())) if len(f) else None,
        distinct_entry_dates=sorted(f.entry_date.astype(str).unique().tolist()),
        distinct_codes=sorted(f.code.unique().tolist()),
        mean_holding_sessions=float(f.holding_sessions.mean()) if len(f) else None,
        max_holding_sessions=int(f.holding_sessions.max()) if len(f) else None)


def nav_statistics(nav, initial_nav_cents):
    complete = (len(nav)>0 and not nav.date.duplicated().any() and nav.status.eq('OK').all()
                and np.isfinite(nav.nav_cents).all() and nav.nav_cents.gt(0).all())
    if not complete:
        return dict(nav_complete=False,net_return=None,annualized_return=None,max_drawdown=None,
                    annualized_volatility=None,sharpe=None,positive_day_ratio=None,flat_day_ratio=None,average_exposure=None)
    values = nav.nav_cents.to_numpy(dtype=float)
    curve = np.r_[float(initial_nav_cents),values]
    returns = curve[1:]/curve[:-1]-1
    sigma = float(returns.std(ddof=0))
    return dict(nav_complete=True,net_return=float(curve[-1]/curve[0]-1),
        annualized_return=float((curve[-1]/curve[0])**(252/len(nav))-1),
        max_drawdown=float(-(curve/np.maximum.accumulate(curve)-1).min()),
        annualized_volatility=sigma*math.sqrt(252),
        sharpe=float(returns.mean()/sigma*math.sqrt(252)) if sigma>0 else None,
        positive_day_ratio=float((returns>0).mean()),
        flat_day_ratio=float(nav.position_value_cents.eq(0).mean()),
        average_exposure=float((nav.position_value_cents/nav.nav_cents).mean()),
        return_days=len(nav),annualization_basis='252_PER_ACTUAL_ACCOUNT_TRADING_DAYS')


def qualify_episodes(s, *, min_count, min_dates, wilson_floor, min_codes=0):
    reasons=[]
    def below(key, floor):
        value=s.get(key)
        return value is None or not np.isfinite(value) or value<floor
    if below('win_rate',.65): reasons.append('WIN_RATE_BELOW_65_PERCENT')
    if s['closed_episode_count']<min_count or len(s['distinct_entry_dates'])<min_dates or len(s['distinct_codes'])<min_codes:
        reasons.append('INSUFFICIENT_TRADES')
    if s['wilson95'][0] is None or s['wilson95'][0]<wilson_floor: reasons.append('WILSON_LOWER_BELOW_GATE')
    # No observed losses implies an unbounded estimate, not a fabricated PF.
    if s['loss_count']:
        if below('profit_factor',1.30): reasons.append('PROFIT_FACTOR_BELOW_1_30')
        if below('payoff_ratio_return',.80): reasons.append('PAYOFF_RATIO_BELOW_0_80')
    elif not s['wins']:
        reasons.extend(['PROFIT_FACTOR_UNDEFINED','PAYOFF_RATIO_UNDEFINED'])
    if s.get('mean_episode_net_return') is None or s['mean_episode_net_return']<=0:
        reasons.append('NONPOSITIVE_EPISODE_EXPECTANCY')
    return reasons


def pool_episode_statistics(cells):
    count=sum(c['closed_episode_count'] for c in cells)
    wins=sum(c['wins'] for c in cells);losses=sum(c['loss_count'] for c in cells)
    pos=sum(c['positive_pnl_cny'] for c in cells);neg=sum(c['negative_pnl_abs_cny'] for c in cells)
    rp=sum(c['sum_positive_episode_return'] for c in cells)
    rn=sum(c['sum_negative_episode_return'] for c in cells)
    return dict(closed_episode_count=count,wins=wins,loss_count=losses,
        win_rate=wins/count if count else None,wilson95=wilson(wins,count),
        profit_factor=pos/neg if neg else None,
        payoff_ratio_return=(rp/wins)/abs(rn/losses) if wins and losses else None,
        mean_episode_net_return=sum(c['sum_episode_net_return'] for c in cells)/count if count else None,
        distinct_entry_dates=sorted({d for c in cells for d in c['distinct_entry_dates']}),
        distinct_codes=sorted({d for c in cells for d in c['distinct_codes']}),
        positive_pnl_cny=pos,negative_pnl_abs_cny=neg,
        payoff_ratio_cny=(pos/wins)/(neg/losses) if wins and losses else None,
        profit_factor_status='FINITE' if losses else 'NO_OBSERVED_LOSSES' if wins else 'UNDEFINED')


def selection_summary(policy_id, cells):
    expected={(f'selection_{i}',cost) for i in range(1,7) for cost in ('base','stress')}
    keys=[(c['split'],c['cost_scenario']) for c in cells]
    if len(keys)!=12 or set(keys)!=expected or any(c['strategy_id']!=policy_id for c in cells):
        raise ContractError('selection must use exactly six early windows and two costs for one policy')
    costs,rejections={},[]
    for cost in ('base','stress'):
        rows=[c for c in cells if c['cost_scenario']==cost]
        s=pool_episode_statistics(rows)
        reasons=qualify_episodes(s,min_count=60,min_dates=30,min_codes=20,wilson_floor=.55)
        complete=all(c['nav_complete'] for c in rows)
        values=[c['net_return'] for c in rows] if complete else []
        s.update(mean_window_return=float(np.mean(values)) if values else None,
            median_window_return=float(np.median(values)) if values else None,
            worst_window_return=min(values) if values else None,
            worst_drawdown=max(c['max_drawdown'] for c in rows) if complete else None,
            nonnegative_windows=sum(r>=0 for r in values),positive_windows=sum(r>0 for r in values))
        if not complete: reasons.append('INCOMPLETE_NAV')
        else:
            if s['mean_window_return']<=0: reasons.append('NONPOSITIVE_MEAN_WINDOW_RETURN')
            if min(values)<-.05: reasons.append('WORST_WINDOW_BELOW_MINUS_5_PERCENT')
            if s['worst_drawdown']>.12: reasons.append('DRAWDOWN_ABOVE_12_PERCENT')
            if s['nonnegative_windows']<4: reasons.append('FEWER_THAN_4_NONNEGATIVE_WINDOWS')
            if s['positive_windows']<2: reasons.append('FEWER_THAN_2_POSITIVE_WINDOWS')
        if any(c['open_position_count'] for c in rows): reasons.append('UNRESOLVED_ASSETS')
        s['rejections']=reasons;costs[cost]=s
        rejections.extend(f'{cost}:{r}' for r in reasons)
    return dict(policy_id=policy_id,qualified=not rejections,costs=costs,rejections=rejections,
        robust_mean_return=min(s['mean_window_return'] for s in costs.values()) if all(s['mean_window_return'] is not None for s in costs.values()) else None,
        robust_wilson_lower=min(s['wilson95'][0] or 0 for s in costs.values()),
        worst_drawdown=max(s['worst_drawdown'] for s in costs.values()) if all(s['worst_drawdown'] is not None for s in costs.values()) else None)


def choose_primary(summaries):
    eligible=[s for s in summaries if s['policy_id'].startswith('S') and s['qualified']]
    ranked=sorted(eligible,key=lambda s:(-s['robust_mean_return'],-s['robust_wilson_lower'],s['worst_drawdown'],s['policy_id']))
    selected=[s['policy_id'] for s in ranked[:3]]
    return dict(primary_policy_id=selected[0] if selected else None,selected_policy_ids=selected,
                qualified_count=len(ranked),verdict='EARLY_QUALIFIED' if selected else 'NO_HIGH_WIN_CANDIDATE')


def review_qualification(primary, baselines):
    reasons=qualify_episodes(primary,min_count=30,min_dates=20,wilson_floor=.50)
    if not primary['nav_complete']: reasons.append('INCOMPLETE_NAV')
    else:
        if primary['net_return']<=0: reasons.append('NONPOSITIVE_REVIEW_RETURN')
        if not baselines['nav_complete'] or primary['net_return']<=baselines['net_return']:
            reasons.append('NOT_ABOVE_BASE_ENV_RETURN')
        if primary['max_drawdown']>.12: reasons.append('DRAWDOWN_ABOVE_12_PERCENT')
    if primary['open_position_count']: reasons.append('UNRESOLVED_ASSETS')
    return reasons
