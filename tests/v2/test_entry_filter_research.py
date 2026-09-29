import importlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core.backtest.stability_research_v2 import EnvironmentPolicy, replay_environment
from core.backtest.strategy_search_v2 import SearchMarket
from core.pipeline.rank_rotation_research import load_rotation_config
from core.pipeline.prism_compare_config import write_json
from core.technical_v2.contracts import ContractError
from tests.v2.test_stability_research import row


class EntryFilterTest(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('core.pipeline.entry_filter_research'))
        return importlib.import_module('core.pipeline.entry_filter_research')

    def test_up_gate_rejects_high_score_flat_and_uses_both_breadths(self):
        api = self.api()
        p = api.EntryPolicy(require_up=True, breadth_floor=.6)
        f = pd.DataFrame([row('600000.SH','20240102',forecast_class3='flat',score3=99.),
            row('600001.SH','20240102',market_breadth20=.59),
            row('600002.SH','20240102',sector_breadth20=.59),
            row('600003.SH','20240102',market_breadth20=.6,sector_breadth20=.6),
            row('600004.SH','20240102',market_breadth20=float('nan'))])
        market=api.FilterMarket(SearchMarket({'20240102':f}),p)
        self.assertEqual(market.ranks('20240102',p.candidate).index.tolist(),['600003.SH'])

    def test_signal_filter_applies_before_next_open_not_using_future_forecast(self):
        api=self.api()
        dates=pd.bdate_range('2024-01-02',periods=20).strftime('%Y%m%d').tolist()
        frames={d:pd.DataFrame([row('600000.SH',d,forecast_class3='up' if d==dates[0] else 'flat')]) for d in dates}
        p=api.EntryPolicy(require_up=True,breadth_floor=.5)
        with tempfile.TemporaryDirectory() as tmp:
            result=api.replay_filter(SearchMarket(frames),policy=p,sessions=dates,
                config=load_rotation_config(),cost_scenario='base',output_dir=Path(tmp)/'cell',run_id='test')
            fills=pd.read_csv(Path(tmp)/'cell/fills.csv.gz',dtype={'trade_date':str})
            self.assertEqual(fills.loc[fills.side=='BUY','trade_date'].tolist(),[dates[1]])
            self.assertEqual(result['metrics']['open_position_count'],0)

    def test_baseline_matches_previous_strategy_actual_fills_and_nav(self):
        api=self.api()
        dates=pd.bdate_range('2024-01-02',periods=20).strftime('%Y%m%d').tolist()
        frames={d:pd.DataFrame([row('600000.SH',d,forecast_class3='flat')]) for d in dates}
        with tempfile.TemporaryDirectory() as tmp:
            for name,p in [('old',EnvironmentPolicy(5,'SECTOR20_BREADTH',.5)),('new',api.EntryPolicy())]:
                fn=replay_environment if name=='old' else api.replay_filter
                fn(SearchMarket(frames),policy=p,sessions=dates,config=load_rotation_config(),
                   cost_scenario='base',output_dir=Path(tmp)/name,run_id='test')
            for filename,cols in [('fills',['code','trade_date','side','quantity','price','fee_cents']),
                                  ('daily_nav',['date','nav_cents','cash_cents'])]:
                pd.testing.assert_frame_equal(pd.read_csv(Path(tmp)/'old'/f'{filename}.csv.gz')[cols],
                                              pd.read_csv(Path(tmp)/'new'/f'{filename}.csv.gz')[cols])

    def test_unqualified_or_weaker_candidate_cannot_replace_baseline(self):
        api=self.api()
        def summary(p,q,qualified=True):
            return dict(policy_id=p.policy_id,policy={'gate':'SECTOR20_BREADTH'},qualified=qualified,
                        robust_lower_quartile=q,robust_mean_return=.03,worst_drawdown=.05)
        base=summary(api.EntryPolicy(),.02)
        weak=summary(api.EntryPolicy(require_up=True),.01)
        high=summary(api.EntryPolicy(require_up=True,breadth_floor=.6),.04,False)
        s=api.choose_upgrade([base,weak,high])
        self.assertIsNone(s['chosen_policy_id'])
        self.assertEqual(s['diagnostic_policy_id'],high['policy_id'])
        high['qualified']=True
        self.assertEqual(api.choose_upgrade([base,weak,high])['chosen_policy_id'],high['policy_id'])

    def exercise_pipeline(self, discovery_return, holdout_return):
        api=self.api()
        self.assertTrue(hasattr(api,'run_entry_filter'))
        dates=pd.bdate_range('2023-01-02',periods=801).strftime('%Y%m%d').tolist()
        with tempfile.TemporaryDirectory() as tmp:
            root,out=Path(tmp)/'source',Path(tmp)/'result'
            root.mkdir()
            write_json(root/'rank_dataset_manifest.json',{})
            pd.DataFrame(columns=['event_id','code']).to_parquet(root/'corporate_actions.parquet')
            dataset=dict(windows={'test':dates[-132:]},scope_flags=[],roster_count=1)
            calls=[]
            def load_market(root,manifest,features,start,end):
                if start>=dates[-132]:
                    self.assertTrue((out/'selection_freeze.json').exists())
                return None
            def replay(market,*,policy,split,cost_scenario,output_dir,**kwargs):
                value=.01 if policy==api.EntryPolicy() else discovery_return if split.startswith('selection') else holdout_return
                metrics=dict(status='OK',strategy_id=policy.policy_id,split=split,cost_scenario=cost_scenario,
                    net_return=value,max_drawdown=.02,closed_trade_count=12,winning_closed_trade_count=6,
                    closed_trade_win_rate=.5,traded_date_count=11,open_position_count=0,
                    fees_total=100.,modeled_slippage_total=200.)
                calls.append((policy.policy_id,split,cost_scenario))
                output_dir.mkdir(parents=True)
                write_json(output_dir/'metrics.json',metrics)
                return {'metrics':metrics}
            with patch.object(api,'_verified_source',return_value=(dataset,{'sessions':dates})),\
                 patch.object(api,'_load_market',side_effect=load_market),\
                 patch.object(api,'replay_filter',side_effect=replay),patch('builtins.print'):
                result=api.run_entry_filter(root,out)
                self.assertEqual(len(calls),68)
                target=result['review_policy_id']
                self.assertEqual({p for p,s,c in calls if s.startswith('holdout')},{target,api.EntryPolicy().policy_id})
                frozen=json.loads((out/'selection_freeze.json').read_text())
                self.assertEqual(result['selection'],frozen['selection'])
                self.assertTrue(api.run_entry_filter(root,out)['reused'])
                (out/'REPORT.md').write_text('tampered')
                with self.assertRaises(ContractError):
                    api.run_entry_filter(root,out)
            return result

    def test_failed_discovery_cannot_be_promoted_by_later_profit(self):
        result=self.exercise_pipeline(-.01,.2)
        self.assertEqual(result['verdict'],'KEEP_BASELINE_NO_DISCOVERY_UPGRADE')

    def test_failed_review_cannot_reselect_another_candidate(self):
        result=self.exercise_pipeline(.03,-.01)
        self.assertEqual(result['verdict'],'KEEP_BASELINE_REVIEW_FAILED')


if __name__=='__main__':
    unittest.main()
