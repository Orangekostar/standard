import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core.backtest.stability_research_v2 import EnvironmentPolicy,replay_environment
from core.backtest.strategy_search_v2 import SearchMarket
from core.pipeline.rank_rotation_research import load_rotation_config
from core.pipeline.prism_compare_config import write_json
from core.technical_v2.contracts import ContractError
from tests.v2.test_stability_research import row


class WickExitTest(unittest.TestCase):
    def api(self): return importlib.import_module('core.backtest.wick_exit_research')

    def test_fixed_shape_thresholds_causality_and_invalid_ohlc(self):
        api=self.api();dates=pd.bdate_range('2024-01-02',periods=23).strftime('%Y%m%d').tolist()
        f=pd.DataFrame(dict(code='600000.SH',date=dates,real_bar=True,comparison_open=100.,
            comparison_close=100.,comparison_high=102.,comparison_low=99.))
        f.loc[21,['comparison_open','comparison_high','comparison_low','comparison_close']]=[101.,106.,100.,102.]
        actual=api.wick_features(f)
        self.assertTrue(actual.loc[21,'high_wick'])
        self.assertAlmostEqual(actual.loc[21,'wick_fraction'],4/6)
        pd.testing.assert_frame_equal(actual.iloc[:22].reset_index(drop=True),api.wick_features(f.iloc[:22]))
        for updates in [{'comparison_high':102.5},{'comparison_high':106.,'comparison_close':105.},
                        {'comparison_low':103.},{'real_bar':False},{'comparison_high':float('inf')}]:
            altered=f.copy()
            for k,v in updates.items(): altered.loc[21,k]=v
            self.assertFalse(api.wick_features(altered).loc[21,'high_wick'])
        altered=f.copy();altered.loc[10,'real_bar']=False
        self.assertFalse(api.wick_features(altered).loc[21,'high_wick'])
        with self.assertRaises(ValueError): api.wick_features(pd.concat([f,f.iloc[-1:]]))

    def run_cell(self,mode,prices,pins=(),stop=False):
        api=self.api();dates=pd.bdate_range('2024-01-02',periods=20).strftime('%Y%m%d').tolist()
        frames={d:pd.DataFrame([row('600000.SH',d,comparison_close=prices.get(i,10.),
            valuation_close=prices.get(i,10.),execution_open=prices.get(i,10.),
            high_wick=i in pins,score3=75. if i==0 else float('nan'))]) for i,d in enumerate(dates)}
        with tempfile.TemporaryDirectory() as tmp:
            result=api.replay_wick(SearchMarket(frames),policy=api.WickPolicy(mode=mode),sessions=dates,
                config=load_rotation_config(),cost_scenario='base',output_dir=Path(tmp)/'cell',run_id='test')
            fills=pd.read_csv(Path(tmp)/'cell/fills.csv.gz',dtype={'trade_date':str})
            closed=pd.read_csv(Path(tmp)/'cell/closed_trades.csv.gz')
            return dates,fills,closed,result['metrics']

    def test_no_fixed_profit_and_wick_sells_only_at_next_open(self):
        prices={2:10.7,3:10.6,4:10.5,5:10.5,6:10.5}
        dates,fills,closed,m=self.run_cell('WICK',prices,pins=(3,))
        self.assertEqual(fills.loc[fills.side=='SELL','trade_date'].tolist(),[dates[4]])
        self.assertEqual(closed.exit_reason.tolist(),['HIGH_WICK_EXIT'])
        self.assertEqual(m['take_profit_exit_count'],0)
        self.assertIsNone(m['exit_rules']['fixed_take_profit'])
        self.assertNotIn('candidate',m)
        dates,fills,closed,m=self.run_cell('NO_TP',prices,pins=(3,))
        self.assertEqual(fills.loc[fills.side=='SELL','trade_date'].tolist(),[dates[6]])
        self.assertEqual(closed.exit_reason.tolist(),['SEARCH_MAX_HOLDING'])

    def test_stop_has_priority_over_wick_and_entry_day_signal_obeys_t_plus_one(self):
        dates,fills,closed,m=self.run_cell('WICK',{2:9.5,3:9.4},pins=(2,))
        self.assertEqual(closed.exit_reason.tolist(),['SEARCH_STOP_LOSS'])
        self.assertEqual(fills.loc[fills.side=='SELL','trade_date'].tolist(),[dates[3]])
        dates,fills,closed,m=self.run_cell('WICK',{},pins=(1,))
        self.assertEqual(fills.loc[fills.side=='BUY','trade_date'].tolist(),[dates[1]])
        self.assertEqual(fills.loc[fills.side=='SELL','trade_date'].tolist(),[dates[2]])

    def test_fixed_baseline_preserves_fills_nav_and_expiry_metrics(self):
        api=self.api();dates=pd.bdate_range('2024-01-02',periods=20).strftime('%Y%m%d').tolist()
        frames={d:pd.DataFrame([row('600000.SH',d,high_wick=True,comparison_close=10.8 if i==2 else 10.)])
                for i,d in enumerate(dates)}
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('old','new'):
                args=dict(sessions=dates,config=load_rotation_config(),cost_scenario='base',output_dir=Path(tmp)/name,run_id='test')
                if name=='old': replay_environment(SearchMarket(frames),policy=EnvironmentPolicy(5,'SECTOR20_BREADTH',.5),**args)
                else: api.replay_wick(SearchMarket(frames),policy=api.WickPolicy(mode='FIXED'),**args)
            for file,cols in [('fills',['code','trade_date','side','quantity','price','fee_cents']),
                              ('daily_nav',['date','nav_cents','cash_cents'])]:
                pd.testing.assert_frame_equal(pd.read_csv(Path(tmp)/'old'/f'{file}.csv.gz')[cols],
                    pd.read_csv(Path(tmp)/'new'/f'{file}.csv.gz')[cols])

    def test_pending_wick_exit_survives_limit_down_and_later_shape_disappears(self):
        api=self.api();dates=pd.bdate_range('2024-01-02',periods=20).strftime('%Y%m%d').tolist()
        frames={d:pd.DataFrame([row('600000.SH',d,high_wick=i==1,
            execution_open=9. if i==2 else 10.,comparison_close=9. if i==2 else 10.,
            valuation_close=9. if i==2 else 10.,score3=75. if i==0 else float('nan'))]) for i,d in enumerate(dates)}
        with tempfile.TemporaryDirectory() as tmp:
            api.replay_wick(SearchMarket(frames),policy=api.WickPolicy(),sessions=dates,
                config=load_rotation_config(),cost_scenario='base',output_dir=Path(tmp)/'cell',run_id='test')
            fills=pd.read_csv(Path(tmp)/'cell/fills.csv.gz',dtype={'trade_date':str})
            closed=pd.read_csv(Path(tmp)/'cell/closed_trades.csv.gz')
            self.assertEqual(fills.loc[fills.side=='SELL','trade_date'].tolist(),[dates[3]])
            self.assertEqual(closed.exit_reason.tolist(),['HIGH_WICK_EXIT'])

    def test_wick_sale_frees_capacity_for_replacement_on_same_next_open(self):
        api=self.api();dates=pd.bdate_range('2024-01-02',periods=20).strftime('%Y%m%d').tolist()
        codes=['600000.SH','600001.SH','600002.SH','600003.SH']
        frames={d:pd.DataFrame([row(code,d,high_wick=i==2 and j==0,
            score3=75.-j if (i==0 and j<3) or (i==2 and j==3) else float('nan'))
            for j,code in enumerate(codes)]) for i,d in enumerate(dates)}
        with tempfile.TemporaryDirectory() as tmp:
            api.replay_wick(SearchMarket(frames),policy=api.WickPolicy(),sessions=dates,
                config=load_rotation_config(),cost_scenario='base',output_dir=Path(tmp)/'cell',run_id='test')
            fills=pd.read_csv(Path(tmp)/'cell/fills.csv.gz',dtype={'trade_date':str})
            day=fills.loc[fills.trade_date==dates[3]].sort_values('execution_sequence')
            self.assertEqual(day[['code','side']].values.tolist(),[[codes[0],'SELL'],[codes[3],'BUY']])

    def test_pipeline_freezes_all_48_accounts_and_rejects_archive_tampering(self):
        api=importlib.import_module('core.pipeline.wick_exit_research')
        dates=pd.bdate_range('2023-01-02',periods=801).strftime('%Y%m%d').tolist()
        with tempfile.TemporaryDirectory() as tmp:
            root,out=Path(tmp)/'source',Path(tmp)/'result';root.mkdir()
            write_json(root/'rank_dataset_manifest.json',{})
            pd.DataFrame(columns=['event_id','code']).to_parquet(root/'corporate_actions.parquet')
            dataset=dict(windows={'test':dates[-132:]},scope_flags=[],roster_count=1)
            calls=[]
            def replay(market,*,policy,split,cost_scenario,output_dir,**kwargs):
                frozen=json.loads((out/'frozen_protocol.json').read_text())
                self.assertEqual(len(frozen['protocol']['policies']),3)
                calls.append((policy.policy_id,split,cost_scenario))
                metrics=dict(status='OK',strategy_id=policy.policy_id,split=split,cost_scenario=cost_scenario,
                    net_return=.01,max_drawdown=.02,closed_trade_count=12,winning_closed_trade_count=6,
                    closed_trade_win_rate=.5,traded_date_count=11,open_position_count=0,
                    high_wick_exit_count=0,fees_total=100.,modeled_slippage_total=200.)
                output_dir.mkdir(parents=True);write_json(output_dir/'metrics.json',metrics)
                return {'metrics':metrics}
            with patch.object(api,'_verified_source',return_value=(dataset,{'sessions':dates})),\
                 patch.object(api,'load_wick_market',return_value=(None,pd.DataFrame(columns=['date','code']))),\
                 patch.object(api,'replay_wick',side_effect=replay),patch('builtins.print'):
                result=api.run_wick_exit(root,out)
                self.assertEqual(len(calls),48);self.assertEqual(len(set(calls)),48)
                self.assertEqual(len(result['cells']),48)
                self.assertTrue(api.run_wick_exit(root,out)['reused'])
                (out/'REPORT.md').write_text('tampered')
                with self.assertRaises(ContractError): api.run_wick_exit(root,out)


if __name__=='__main__': unittest.main()
