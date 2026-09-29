import importlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from core.backtest.strategy_search_v2 import SearchMarket
from core.pipeline.rank_rotation_research import load_rotation_config
from core.pipeline.prism_compare_config import write_json
from core.technical_v2.contracts import ContractError
from tests.v2.test_stability_research import row


class PriceVolumeTest(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('core.backtest.price_volume_research'))
        return importlib.import_module('core.backtest.price_volume_research')

    def test_nested_signals_require_ordered_history_and_context(self):
        api=self.api()
        f=pd.DataFrame([row(f'60000{i}.SH','20240102') for i in range(4)])
        f['pv_breakout']=True
        f['pv_recent_wash']=[False,True,True,True]
        f['pv_recent_sequence']=[False,False,True,True]
        f['pv_recent_weak_sequence']=[False,False,False,True]
        f['pv_background_up']=True
        for mode,want in [('BREAKOUT',4),('WASH_BREAKOUT',3),('BASE_WASH_BREAKOUT',2),('CONTEXT_SEQUENCE',1)]:
            self.assertEqual(int(api.entry_mask(f,mode).sum()),want)
        f.loc[3,'pv_background_up']=False
        self.assertEqual(int(api.entry_mask(f,'CONTEXT_SEQUENCE').sum()),0)

    def features(self):
        dates=pd.bdate_range('2024-01-01',periods=90).strftime('%Y%m%d')
        f=pd.DataFrame([row('600000.SH',d) for d in dates])
        f['comparison_close']=10.
        f['comparison_open']=9.95
        f['comparison_high']=10.02
        f['comparison_low']=9.8
        f['amount_cny']=60000000.
        f['market_index']=np.linspace(100,110,len(f))
        f['sector_index']=np.linspace(100,112,len(f))
        return f

    def test_features_are_causal_and_missing_prices_do_not_create_volume_patterns(self):
        api=self.api()
        f=self.features()
        short=api.phase_features(f.iloc[:80])
        f.loc[80:,'comparison_close']=20.
        f.loc[80:,'amount_cny']=1000000000.
        full=api.phase_features(f)
        cols=[c for c in full if c.startswith('pv_')]
        pd.testing.assert_frame_equal(short[cols],full.iloc[:80][cols])
        f.loc[70,'real_bar']=False
        missing=api.phase_features(f)
        self.assertFalse(bool(missing.loc[70,'pv_base']))
        self.assertFalse(bool(missing.loc[70,'pv_wash']))
        self.assertFalse(bool(missing.loc[70,'pv_breakout']))

    def test_breakout_uses_prior_high_and_prior_amount_not_current_day_in_reference(self):
        api=self.api()
        f=self.features()
        f.loc[89,['comparison_close','comparison_high','comparison_low','amount_cny']]=[10.5,10.5,10.,90000000.]
        x=api.phase_features(f)
        self.assertTrue(bool(x.loc[89,'pv_breakout']))
        self.assertAlmostEqual(x.loc[89,'pv_amount_ratio'],1.5)
        self.assertAlmostEqual(x.loc[89,'pv_high10prev'],10.)

    def test_complete_sequence_requires_prior_base_and_preserved_floor(self):
        api=self.api()
        f=self.features()
        f.loc[67:71,'amount_cny']=80000000.
        f.loc[72:80,'comparison_close']=[10.2,10.1,10.,9.8,9.85,9.9,10.,10.1,10.4]
        f.loc[73:75,'amount_cny']=30000000.
        f.loc[80,'amount_cny']=100000000.
        f['comparison_high']=f.comparison_close+.02
        f['comparison_low']=f.comparison_close-.2
        f.loc[71:75,'market_index']=[106,105,104,103,102]
        x=api.phase_features(f)
        self.assertTrue(bool(x.loc[70,'pv_base']))
        self.assertTrue(bool(x.loc[75,'pv_wash_after_base']))
        self.assertTrue(bool(x.loc[75,'pv_weak_wash_after_base']))
        self.assertTrue(bool(api.entry_mask(x,'CONTEXT_SEQUENCE').loc[80]))
        # A deeper break of the old floor must not be narrated as supported washing.
        f.loc[75,'comparison_close']=9.5
        x=api.phase_features(f)
        self.assertFalse(bool(x.loc[75,'pv_wash_after_base']))

    def test_entry_uses_signal_close_and_fill_next_open_with_normal_ledger(self):
        api=self.api()
        dates=pd.bdate_range('2024-01-02',periods=20).strftime('%Y%m%d').tolist()
        frames={d:pd.DataFrame([{**row('600000.SH',d),'pv_breakout':d==dates[0],
            'pv_recent_wash':False,'pv_recent_sequence':False,'pv_recent_weak_sequence':False,'pv_background_up':False}]) for d in dates}
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'cell'
            result=api.replay_phase(SearchMarket(frames),policy=api.PhasePolicy('BREAKOUT'),
                sessions=dates,config=load_rotation_config(),cost_scenario='base',output_dir=out,run_id='test')
            fills=pd.read_csv(out/'fills.csv.gz',dtype={'trade_date':str})
            self.assertEqual(fills.loc[fills.side=='BUY','trade_date'].tolist(),[dates[1]])
            self.assertEqual(result['metrics']['open_position_count'],0)
            self.assertEqual(result['metrics']['strategy_id'],'PV_BREAKOUT_H5_R50')
            self.assertNotIn('candidate',result['metrics'])

    def test_pipeline_freezes_before_later_history_and_does_not_promote_failed_discovery(self):
        self.assertIsNotNone(importlib.util.find_spec('core.pipeline.price_volume_research'))
        api=importlib.import_module('core.pipeline.price_volume_research')
        dates=pd.bdate_range('2023-01-02',periods=801).strftime('%Y%m%d').tolist()
        with tempfile.TemporaryDirectory() as tmp:
            root,out=Path(tmp)/'data',Path(tmp)/'result'
            root.mkdir()
            write_json(root/'rank_dataset_manifest.json',{})
            pd.DataFrame(columns=['event_id','code']).to_parquet(root/'corporate_actions.parquet')
            dataset=dict(windows={'test':dates[-132:]},scope_flags=[],roster_count=1)
            calls=[]
            def loader(root,manifest,features,start,end):
                if start>=dates[-132]:
                    self.assertTrue((out/'selection_freeze.json').exists())
                return None,pd.DataFrame()
            def replay(market,*,policy,split,cost_scenario,output_dir,**kwargs):
                value=.02 if policy==api.baseline_policy() else -.01 if split.startswith('selection') else .2
                m=dict(status='OK',strategy_id=policy.policy_id,split=split,cost_scenario=cost_scenario,
                    net_return=value,max_drawdown=.03,closed_trade_count=12,winning_closed_trade_count=6,
                    closed_trade_win_rate=.5,traded_date_count=11,open_position_count=0,
                    fees_total=100.,modeled_slippage_total=200.)
                calls.append((policy.policy_id,split))
                output_dir.mkdir(parents=True)
                write_json(output_dir/'metrics.json',m)
                return {'metrics':m}
            with patch.object(api,'_verified_source',return_value=(dataset,{'sessions':dates})),\
                patch.object(api,'load_phase_market',side_effect=loader),\
                patch.object(api,'describe_cohorts',return_value=[]),\
                patch.object(api,'replay_phase',side_effect=replay),\
                patch.object(api,'replay_environment',side_effect=replay),patch('builtins.print'):
                result=api.run_price_volume(root,out)
                self.assertEqual(len(calls),80)
                self.assertEqual(result['verdict'],'NO_DISCOVERY_UPGRADE')
                self.assertIsNone(result['selection']['chosen_policy_id'])
                self.assertEqual(result['selection'],json.loads((out/'selection_freeze.json').read_text())['selection'])
                self.assertTrue(api.run_price_volume(root,out)['reused'])
                (out/'REPORT.md').write_text('changed')
                with self.assertRaises(ContractError):
                    api.run_price_volume(root,out)


if __name__=='__main__':
    unittest.main()
