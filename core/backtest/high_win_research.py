"""One execution shell for all eighteen high-win laboratory policies.

Native controls keep FactorMarket/FactorReplay ranking and sizing. Only the
documented candidate truncation and exit-priority corrections are shared.
"""
from __future__ import annotations

import time
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

import pandas as pd

from core.backtest.factor_portfolio_research import FactorMarket, FactorPolicy, FactorReplay
from core.backtest.prism_compare_engine import _bool, _csv_value, _decimal, _gzip_writer, _text
from core.backtest.rank_rotation_v2 import RotationBuy
from core.backtest.strategy_search_v2 import SearchMarket
from core.data.symbols import is_buyable_mainboard_ts_code, is_risk_warning_name
from core.strategies.high_win_suite import HighWinPolicy
from core.strategies.prism_a_share import finite
from core.technical_v2.contracts import ContractError

CONTROLS = {
    'BASE_ENV': FactorPolicy(ranking='SCORE', vol_control=False, diversify=False, min_price=5.),
    'BASE_DEF': FactorPolicy(ranking='DEFENSIVE', vol_control=True, diversify=True, min_price=5.),
}
DECISION_COLUMNS = ['date','code','score','forecast_class','rank_score','action','reasons',
                    'family','variant','event_anchor','information_cutoff']


class HighWinMarket:
    def __init__(self, base, policy):
        self.frames, self.policy = base.frames, policy

    def ranks(self, date, candidate=None):
        f = self.frames[date]
        scores = f.high_win_rank_score.where(f[self.policy.policy_id].eq(True)).dropna()
        ranked = pd.DataFrame({'rank_score':scores, 'code_sort':scores.index})
        return ranked.sort_values(['rank_score','code_sort'], ascending=[False,True]).rank_score


class HighWinReplay(FactorReplay):
    def __init__(self, *args, lab_policy, **kwargs):
        self.lab_policy = lab_policy
        self.lab_id = lab_policy.policy_id if isinstance(lab_policy, HighWinPolicy) else lab_policy
        self.is_new = isinstance(lab_policy, HighWinPolicy)
        self.consumed_events = set()
        self.signal_blockers = Counter()
        self.daily_exit_reasons = {}
        super().__init__(*args, **kwargs)

    def _entry_reason(self):
        return self.lab_id if self.is_new else super()._entry_reason()

    def _entry_blockers(self, code, row, date):
        reasons = super()._entry_blockers(code, row, date)
        if self.is_new:
            reasons = [r for r in reasons if r != 'PREDICTION_UNAVAILABLE']
        return reasons

    def _event(self, code, row, date):
        if not self.is_new:
            return (code, 'NATIVE_CONTROL', date)
        return (code, self.lab_policy.family, row.get(self.lab_policy.family+'_anchor'))

    def _metric_fields(self, nav, fills, closed, remaining):
        return {**super()._metric_fields(nav, fills, closed, remaining),
            'strategy_id':self.lab_id,
            'native_control_id': None if self.is_new else self.environment_policy.policy_id,
            'prediction_method':'PRICE_VOLUME_RULES' if self.is_new else 'NATIVE_FACTOR_CONTROL',
            'signal_blockers':dict(self.signal_blockers),
            'event_consumption':'FIRST_FULL_SIGNAL_IN_ACCOUNT_INCLUDING_CAPACITY_AND_HOLDING_BLOCKS',
            'execution_corrections':['ALL_SIGNAL_CANDIDATES_BEFORE_PLAN','EXPIRY_BEFORE_TAKE_PROFIT'],
            'production_activation':False}

    def search_day(self, market, date, next_date, writer):
        frame = market.frames[date]
        # Only previous plans are executed here. Candidate evaluation happens
        # after today's close has been marked, never in open sizing/fill logic.
        opening_codes = self._held() | {b.code for b in self.pending_buys} | set(self.pending_exits)
        rows = {str(r['code']):r for r in frame.loc[frame.index.isin(opening_codes)].to_dict('records')}
        self._execute(date, rows)
        mark = self._mark(date, rows)
        if next_date is None:
            return
        ranks = market.ranks(date, self.candidate)
        needed = set(ranks.index) - set(rows)
        rows.update({str(r['code']):r for r in frame.loc[frame.index.isin(needed)].to_dict('records')})
        self.signal_sectors = frame.sector_id.to_dict()
        self.planned_sectors = set()
        held, sells = self._held(), dict(self.exit_reasons)
        all_exit_reasons = {}
        for code in sorted(held):
            row = rows.get(code, {})
            reasons = []
            if code in sells:
                reasons.append(sells[code])
            if (not is_buyable_mainboard_ts_code(code) or is_risk_warning_name(_text(row.get('name')))
                    or _bool(row.get('is_risk_warning')) is True):
                reasons.append('RISK_WARNING_SECURITY')
            for lot in self.portfolio.lots():
                if lot.code != code or lot.status != 'OPEN':
                    continue
                price, basis = _decimal(row.get('comparison_close')), _decimal(lot.metadata.get('search_entry_price_basis'))
                change = price/basis-1 if price is not None and basis and basis>0 else None
                if change is not None and change <= _decimal('-.03'):
                    reasons.append('SEARCH_STOP_LOSS')
                if self.date_index[next_date]-self.date_index[lot.entry_date] >= 5:
                    reasons.append('SEARCH_MAX_HOLDING')
                if change is not None and change >= _decimal('.04'):
                    reasons.append('SEARCH_TAKE_PROFIT')
            reasons = list(dict.fromkeys(reasons))
            if reasons:
                sells[code] = reasons[0]
                all_exit_reasons[code] = reasons
        self.daily_exit_reasons[date] = all_exit_reasons
        self._submit_exits(date, next_date, rows, sells)
        capacity = max(0, self.max_names-len(held-set(sells)))
        in_tail = self.date_index[date] >= len(self.sessions)-self.tail_sessions-1
        buys, blockers = [], {}
        for code in ranks.index:
            row = rows[code]
            event = self._event(code,row,date)
            reasons = []
            if self.is_new and event in self.consumed_events:
                reasons.append('EVENT_ALREADY_CONSUMED')
            else:
                # Consume the observable event once, not only successful fills.
                if self.is_new:
                    self.consumed_events.add(event)
                if code in held:
                    reasons.append('EXISTING_HOLDING')
                elif in_tail:
                    reasons.append('SETTLEMENT_TAIL')
                elif mark.nav_cents is None:
                    reasons.append('INCOMPLETE_NAV')
                elif len(buys) >= capacity:
                    reasons.append('NO_PLANNED_CAPACITY')
                else:
                    reasons.extend(self._entry_blockers(code,row,date))
            if not reasons:
                buys.append(RotationBuy(code))
            blockers[code] = reasons
            self.signal_blockers.update(reasons)
        if not len(ranks):
            self.signal_blockers['NO_ELIGIBLE_CANDIDATE_DAY'] += 1
        if not held:
            self.signal_blockers['NO_HELD_ASSET_DAY'] += 1
        self.pending_buys = tuple(buys)
        self.pending_budget = int(mark.nav_cents or 0)//self.max_names
        self.previous_rows = rows
        selected = {b.code for b in buys}
        for code in sorted(held|set(ranks.index)):
            row = rows.get(code,{})
            action = 'SELL' if code in sells else 'BUY' if code in selected else 'HOLD' if code in held else 'SKIP'
            event = self._event(code,row,date)
            writer.writerow({k:_csv_value(v) for k,v in dict(date=date,code=code,
                score=finite(row.get('score3')),forecast_class=_text(row.get('forecast_class3')),
                rank_score=finite(ranks.get(code)),action=action,
                reasons=all_exit_reasons.get(code,blockers.get(code,[])),family=event[1],
                variant=self.lab_policy.variant if self.is_new else 'NATIVE',event_anchor=event[2],
                information_cutoff=date).items()})


def replay_high_win(market, *, policy, sessions, config, cost_scenario, output_dir,
                    run_id='high-win-v1', split='selection_1', scope_flags=(), corporate_actions=None):
    if (sessions != sorted(set(sessions)) or len(sessions)<=12 or not set(sessions).issubset(market.frames)
            or cost_scenario not in {'base','stress'} or not isinstance(market,SearchMarket)):
        raise ContractError('invalid high-win account inputs')
    new = isinstance(policy,HighWinPolicy)
    if not new and policy not in CONTROLS:
        raise ContractError('unknown fixed control')
    native = FactorPolicy() if new else CONTROLS[policy]
    output = Path(output_dir).resolve()
    if output.exists() and any(output.iterdir()):
        raise ContractError('high-win replay requires a fresh account directory')
    output.mkdir(parents=True,exist_ok=True)
    started = time.perf_counter()
    lab_id = policy.policy_id if new else policy
    replay = HighWinReplay(config,sessions,cost_scenario,output,run_id+':'+lab_id,split,
        scope_flags,corporate_actions,candidate=native.candidate,tail_sessions=11,
        environment_policy=native,lab_policy=policy)
    wrapped = HighWinMarket(market,policy) if new else FactorMarket(market,native)
    with ExitStack() as stack:
        writer = _gzip_writer(stack,output/'decisions.csv.gz',DECISION_COLUMNS)
        for i,date in enumerate(sessions):
            replay.search_day(wrapped,date,sessions[i+1] if i+1<len(sessions) else None,writer)
    return replay.finish(started)
