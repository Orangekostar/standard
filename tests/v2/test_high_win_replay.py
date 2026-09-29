import tempfile
import unittest
from pathlib import Path

import pandas as pd

from core.backtest.factor_portfolio_research import FactorPolicy, replay_factor
from core.backtest.high_win_research import replay_high_win
from core.backtest.strategy_search_v2 import SearchMarket
from core.pipeline.rank_rotation_research import load_rotation_config
from core.strategies.high_win_suite import HighWinPolicy
from tests.v2.test_factor_portfolio_research import factor_row


class HighWinReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.dates=pd.bdate_range('2024-01-02',periods=30).strftime('%Y%m%d').tolist()
        self.config=load_rotation_config()

    def frames(self, count=1):
        return {d:pd.DataFrame([factor_row(f'60000{i}.SH',d,score3=90.-i,
            high_win_rank_score=1.-i*.01,S02_CONFIRMED=True,S02_anchor=self.dates[0])
            for i in range(count)]) for d in self.dates}

    def replay(self, frames, policy=HighWinPolicy('S02'), name='cell'):
        out=self.root/name
        result=replay_high_win(SearchMarket(frames),policy=policy,sessions=self.dates,
            config=self.config,cost_scenario='base',output_dir=out)
        return result['metrics'],out

    def read(self,root,name):
        return pd.read_csv(root/(name+'.csv.gz'),dtype={'date':str,'trade_date':str})

    def test_all_eight_candidates_checked_and_no_prediction_gate_for_new_rules(self):
        f=self.frames(8)
        for day in f.values():
            day.loc[:5,'list_date']='20990101'  # signal-known ban outside the ranking stage
            day['prediction_status']='MISSING';day['score3']=float('nan')
        metrics,out=self.replay(f)
        buys=self.read(out,'fills').query("side=='BUY'")
        self.assertEqual(buys.code.tolist(),['600006.SH','600007.SH'])
        self.assertTrue((buys.trade_date==self.dates[1]).all())
        self.assertEqual(metrics['closed_trade_count'],2)

    def test_same_candidate_correction_applies_to_native_controls(self):
        f=self.frames(8)
        for day in f.values(): day.loc[:5,'list_date']='20990101'
        _,out=self.replay(f,policy='BASE_ENV')
        buys=self.read(out,'fills').query("side=='BUY'")
        self.assertEqual(buys.iloc[:2].code.tolist(),['600006.SH','600007.SH'])

    def test_no_bug_native_control_fills_and_nav_match_exactly(self):
        f=self.frames(3)
        _,out=self.replay(f,policy='BASE_ENV')
        old=self.root/'native'
        replay_factor(SearchMarket(f),policy=FactorPolicy(),sessions=self.dates,config=self.config,
            cost_scenario='base',output_dir=old,run_id='native')
        pd.testing.assert_frame_equal(self.read(out,'fills')[['trade_date','code','side','quantity','price','fee_cents']],
            self.read(old,'fills')[['trade_date','code','side','quantity','price','fee_cents']])
        pd.testing.assert_frame_equal(self.read(out,'daily_nav'),self.read(old,'daily_nav'))

    def test_failed_open_consumes_anchor_and_price_boundary_is_raw(self):
        f=self.frames()
        f[self.dates[1]].loc[0,'execution_open']=4.99
        metrics,out=self.replay(f)
        self.assertEqual(metrics['filled_buy_count'],0)
        attempts=self.read(out,'execution_attempts')
        self.assertEqual(attempts.reason.tolist(),['USER_MIN_PRICE_AT_OPEN'])
        f=self.frames()
        for day in f.values():
            day.loc[0,['execution_open','valuation_close']]=[5.,5.]
            day.loc[0,['comparison_close','adj_factor']]=[10.,2.]
            day.loc[0,['up_limit','down_limit']]=[5.5,4.5]
        metrics,_=self.replay(f,name='equality')
        self.assertEqual(metrics['filled_buy_count'],1)

    def test_entry_close_stop_sells_next_day_at_actual_gap_not_stop_price(self):
        f=self.frames()
        f[self.dates[1]].loc[0,['comparison_close','valuation_close']]=9.5
        f[self.dates[2]].loc[0,'execution_open']=9.2
        _,out=self.replay(f)
        fills=self.read(out,'fills')
        self.assertEqual(fills.side.tolist(),['BUY','SELL'])
        self.assertEqual(fills.trade_date.tolist(),[self.dates[1],self.dates[2]])
        self.assertAlmostEqual(fills.iloc[1].price,9.19)
        self.assertEqual(self.read(out,'closed_trades').exit_reason.iloc[0],'SEARCH_STOP_LOSS')

    def test_expiry_before_profit_and_all_reasons_logged(self):
        f=self.frames()
        f[self.dates[5]].loc[0,['comparison_close','valuation_close']]=10.5
        _,out=self.replay(f)
        trade=self.read(out,'closed_trades').iloc[0]
        self.assertEqual(trade.exit_reason,'SEARCH_MAX_HOLDING')
        self.assertEqual(trade.holding_sessions,5)
        decision=self.read(out,'decisions').query("action=='SELL'").iloc[0]
        self.assertIn('SEARCH_TAKE_PROFIT',decision.reasons)
        self.assertIn('SEARCH_MAX_HOLDING',decision.reasons)

    def test_current_close_high_and_amount_cannot_change_current_open_fill(self):
        f=self.frames()
        f[self.dates[1]].loc[0,['valuation_close','comparison_close','comparison_high','amount_cny']]=[1.,1.,999.,1.]
        _,out=self.replay(f)
        buy=self.read(out,'fills').query("side=='BUY'").iloc[0]
        self.assertEqual(buy.price,10.01)
        self.assertEqual(buy.quantity,16600)

    def test_pending_losing_exit_does_not_free_slot(self):
        f=self.frames(4)
        for day in list(f.values())[2:]:
            day['execution_open']=9.;day['comparison_close']=9.;day['valuation_close']=9.
        metrics,out=self.replay(f)
        self.assertEqual(metrics['filled_buy_count'],3)
        self.assertEqual(metrics['filled_sell_count'],0)
        self.assertEqual(metrics['open_position_count'],3)
        self.assertLess(metrics['net_return'],0)

    def test_tail_boundary_and_continuous_midpoint(self):
        self.dates=pd.bdate_range('2024-01-02',periods=132).strftime('%Y%m%d').tolist()
        f=self.frames(3)
        for i,day in enumerate(f.values()):
            day['S02_CONFIRMED']=False
            for signal_index,stock_index in ((64,0),(119,1),(120,2)):
                if i==signal_index: day.loc[stock_index,'S02_CONFIRMED']=True
            day['S02_anchor']=self.dates[i]
        _,out=self.replay(f)
        fills=self.read(out,'fills')
        self.assertEqual(fills.query("side=='BUY'").trade_date.tolist(),[self.dates[65],self.dates[120]])
        self.assertEqual(fills.query("side=='SELL'").trade_date.tolist(),[self.dates[70],self.dates[125]])
        self.assertEqual(len(self.read(out,'daily_nav')),132)
        blocked=self.read(out,'decisions').query("code=='600002.SH'")
        self.assertIn('SETTLEMENT_TAIL',blocked.iloc[0].reasons)


if __name__=='__main__': unittest.main()
