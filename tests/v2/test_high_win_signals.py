import unittest

import pandas as pd

from core.strategies.high_win_suite import HighWinPolicy, evaluate_signals, family_conditions, policy_grid
from tests.v2.test_factor_portfolio_research import factor_row


def signal_fixture(family):
    dates=pd.bdate_range('2024-01-02',periods=30).strftime('%Y%m%d').tolist()
    extra=dict(comparison_open=10.,comparison_close=10.,comparison_high=10.1,comparison_low=9.9,
        amount_cny=60_000_000.,MA5=10.,MA10=10.,MA20=10.,MA60=9.9,HH10=10.4,LL10=9.9,
        HH20prev=10.5,LL20prev=9.8,ATR5=.1,ATR20=.2,DD20=-.05,VR3=.7,VA=1.2,
        R1=.01,R2=-.01,R3=-.03,R5=-.03,ell5=.01,ell60=.1,Zrel3=-1.5,
        LW=.5,UW=.1,CLV=.6,ER20=.2,sector_ell20=.03,sector_breadth_delta3=.11,
        sector_return20_pct=.8,rs60_within_sector_pct=.8,market_breadth_delta5=.02,
        market_breadth_prev5_min=.3,market_breadth_delta1=.1,market_return1=.01,market_ell5=-.02,
        sector_rotation_anchor=dates[24],sigma20=.02,atr_fraction=.02,ADV20=60_000_000.,
        high_win_rank_score=.8,high_win_feature_reason='')
    f=pd.DataFrame([factor_row('600000.SH',d,**extra) for d in dates])
    f.loc[29,['comparison_close','comparison_high','comparison_low','R1']]=[10.2,10.3,10.,.02]
    if family=='S01': f.loc[29,'MA20']=10.05
    if family=='S02': f.loc[26,['comparison_close','HH20prev','VA']]=[10.2,10.,1.6]
    if family=='S03': f.loc[28,['LL20prev','comparison_low','comparison_close']]=[10.,9.8,10.05]
    if family=='S05': f.loc[28,['comparison_close','HH20prev','VA']]=[10.1,10.,1.6]
    if family=='S08':
        f.loc[25,['comparison_open','R1','VA','CLV','amount_cny']]=[9.8,.03,2.,.8,100_000_000.]
    return f


class HighWinSignalTests(unittest.TestCase):
    def test_all_families_positive_and_single_required_condition_negative(self):
        negatives={'S01':('DD20',28,-.01),'S02':('comparison_low',27,9.6),
            'S03':('comparison_low',28,10.),'S04':('Zrel3',28,-.5),
            'S05':('ATR5',27,.3),'S06':('market_breadth_delta1',29,.07),
            'S07':('VR3',28,.9),'S08':('amount_cny',27,200_000_000.)}
        for family,(column,index,value) in negatives.items():
            with self.subTest(family=family):
                f=signal_fixture(family)
                self.assertTrue(evaluate_signals(f,HighWinPolicy(family)).iloc[-1].eligible)
                f.loc[index,column]=value
                self.assertFalse(evaluate_signals(f,HighWinPolicy(family)).iloc[-1].eligible)

    def test_strict_subset_and_new_signals_ignore_old_factor_gate(self):
        for family in (f'S{i:02d}' for i in range(1,9)):
            with self.subTest(family=family):
                f=signal_fixture(family)
                f['prediction_status']='MISSING';f['score3']=float('nan');f['F03']=-1.
                a=evaluate_signals(f,HighWinPolicy(family))
                b=evaluate_signals(f,HighWinPolicy(family,'STRICT'))
                self.assertTrue(a.iloc[-1].eligible)
                self.assertTrue(b.iloc[-1].eligible)
                self.assertFalse((b.eligible&~a.eligible).any())
                f.loc[29,'market_breadth_delta5']=-.01
                self.assertTrue(evaluate_signals(f,HighWinPolicy(family)).iloc[-1].eligible)
                self.assertFalse(evaluate_signals(f,HighWinPolicy(family,'STRICT')).iloc[-1].eligible)

    def test_future_appends_do_not_change_prior_signals_or_anchors(self):
        for family in (f'S{i:02d}' for i in range(1,9)):
            f=signal_fixture(family)
            first=evaluate_signals(f.iloc[:29],HighWinPolicy(family))
            full=evaluate_signals(f,HighWinPolicy(family))
            pd.testing.assert_frame_equal(first,full.iloc[:29])

    def test_recent_breakout_anchor_not_best_older_anchor(self):
        f=signal_fixture('S02')
        f.loc[27,['comparison_close','HH20prev','VA']]=[10.5,10.3,1.6]
        raw=family_conditions(f)
        self.assertEqual(raw.iloc[-1].S02_anchor,f.loc[27,'date'])
        self.assertFalse(raw.iloc[-1].S02)

    def test_missing_consolidation_bar_cannot_be_skipped(self):
        for family in ('S02','S08'):
            f=signal_fixture(family)
            f.loc[27,'comparison_low']=float('nan')
            self.assertFalse(evaluate_signals(f,HighWinPolicy(family)).iloc[-1].eligible)

    def test_signal_day_raw_price_and_real_estate_bans(self):
        for updates in ({'valuation_close':4.99},{'sector_id':'801180.SI'},
                        {'sector_id':None},{'name':'ST EXAMPLE'},{'code':'300001.SZ'}):
            f=signal_fixture('S01')
            for k,v in updates.items(): f[k]=v
            self.assertFalse(evaluate_signals(f,HighWinPolicy('S01')).iloc[-1].eligible)
        f=signal_fixture('S01');f['valuation_close']=5.
        self.assertTrue(evaluate_signals(f,HighWinPolicy('S01')).iloc[-1].eligible)
        self.assertEqual(len({p.policy_id for p in policy_grid()}),16)


if __name__=='__main__': unittest.main()
