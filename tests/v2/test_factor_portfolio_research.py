import importlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core.backtest.strategy_search_v2 import SearchMarket
from core.backtest.stability_research_v2 import EnvironmentPolicy,replay_environment
from core.pipeline.rank_rotation_research import load_rotation_config
from core.pipeline.prism_compare_config import write_json
from core.technical_v2.contracts import ContractError
from tests.v2.test_stability_research import row


def factor_row(code,date,**updates):
    return row(code,date,**{'F04':.3,'F05':.3,'Q02':.02,**updates})


class FactorPortfolioTest(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('core.backtest.factor_portfolio_research'))
        return importlib.import_module('core.backtest.factor_portfolio_research')

    def test_grid_has_twelve_fixed_policies_and_baseline(self):
        a=self.api();grid=a.policy_grid()
        self.assertEqual(len(grid),12);self.assertEqual(len({p.policy_id for p in grid}),12)
        self.assertIn(a.FactorPolicy(),grid)

    def test_relative_and_sector_strength_reorder_candidates_without_future_data(self):
        a=self.api();date='20240102'
        f=pd.DataFrame([factor_row('600000.SH',date,score3=80.),
                       factor_row('600001.SH',date,score3=70.)])
        f.loc[0,['F04','F05','sector_log_return20','sector_breadth20']]=[-.5,-.5,.01,.5]
        f.loc[1,['F04','F05','sector_log_return20','sector_breadth20']]=[.8,.8,.1,.9]
        market=SearchMarket({date:f})
        p=a.FactorPolicy(ranking='LEADER')
        self.assertEqual(a.FactorMarket(market,p).ranks(date,p.candidate).index.tolist(),['600001.SH','600000.SH'])
        p=a.FactorPolicy()
        self.assertEqual(a.FactorMarket(market,p).ranks(date,p.candidate).index.tolist(),['600000.SH','600001.SH'])
        f.loc[1,'F04']=float('nan');p=a.FactorPolicy(ranking='LEADER')
        self.assertEqual(a.FactorMarket(SearchMarket({date:f}),p).ranks(date,p.candidate).index.tolist(),['600000.SH'])

    def test_volatility_budget_uses_previous_signal_not_execution_day(self):
        a=self.api();dates=pd.bdate_range('2024-01-02',periods=20).strftime('%Y%m%d').tolist()
        frames={d:pd.DataFrame([factor_row('600000.SH',d)]) for d in dates}
        frames[dates[0]].loc[0,'Q02']=.03
        for d in dates[1:]: frames[d].loc[0,'Q02']=.001
        with tempfile.TemporaryDirectory() as tmp:
            a.replay_factor(SearchMarket(frames),policy=a.FactorPolicy(vol_control=True),
                sessions=dates,config=load_rotation_config(),cost_scenario='base',output_dir=Path(tmp)/'cell',run_id='test')
            fills=pd.read_csv(Path(tmp)/'cell/fills.csv.gz')
            self.assertEqual(int(fills.loc[fills.side=='BUY'].iloc[0].quantity),8300)

    def test_diversification_keeps_top_stock_per_sector_and_blocks_held_sector(self):
        a=self.api();dates=pd.bdate_range('2024-01-02',periods=20).strftime('%Y%m%d').tolist()
        frames={}
        for i,d in enumerate(dates):
            frames[d]=pd.DataFrame([factor_row('600000.SH',d,sector_id='A',score3=90. if i==0 else float('nan')),
                factor_row('600001.SH',d,sector_id='A',score3=89.),
                factor_row('600002.SH',d,sector_id='B',score3=88.),
                factor_row('600003.SH',d,sector_id='C',score3=87.)])
        with tempfile.TemporaryDirectory() as tmp:
            a.replay_factor(SearchMarket(frames),policy=a.FactorPolicy(diversify=True),
                sessions=dates,config=load_rotation_config(),cost_scenario='base',output_dir=Path(tmp)/'cell',run_id='test')
            fills=pd.read_csv(Path(tmp)/'cell/fills.csv.gz',dtype={'trade_date':str})
            first=fills.loc[(fills.side=='BUY')&(fills.trade_date==dates[1])]
            self.assertEqual(set(first.code),{'600000.SH','600002.SH','600003.SH'})
            self.assertFalse(((fills.code=='600001.SH')&(fills.side=='BUY')&(fills.trade_date<dates[6])).any())

    def test_baseline_matches_original_fills_and_nav(self):
        a=self.api();dates=pd.bdate_range('2024-01-02',periods=20).strftime('%Y%m%d').tolist()
        frames={d:pd.DataFrame([factor_row('600000.SH',d)]) for d in dates}
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('old','new'):
                kwargs=dict(sessions=dates,config=load_rotation_config(),cost_scenario='base',
                    output_dir=Path(tmp)/name,run_id='test')
                if name=='old': replay_environment(SearchMarket(frames),policy=EnvironmentPolicy(5,'SECTOR20_BREADTH',.5),**kwargs)
                else: a.replay_factor(SearchMarket(frames),policy=a.FactorPolicy(),**kwargs)
            for file,cols in [('fills',['code','trade_date','side','quantity','price','fee_cents']),
                              ('daily_nav',['date','nav_cents','cash_cents'])]:
                pd.testing.assert_frame_equal(pd.read_csv(Path(tmp)/'old'/f'{file}.csv.gz')[cols],
                    pd.read_csv(Path(tmp)/'new'/f'{file}.csv.gz')[cols])

    def test_pareto_removes_dominated_and_unqualified_and_freezes_three_roles(self):
        a=self.api()
        def s(name,ret,dd,qualified=True):
            return dict(policy_id=name,qualified=qualified,robust_mean_return=ret,
                worst_drawdown=dd,robust_lower_quartile=.01)
        baseline=s(a.FactorPolicy().policy_id,.08,.08)
        r=a.select_frontier([baseline,s('HIGH',.12,.09),s('LOW',.05,.025),
            s('DOMINATED',.04,.06),s('UNQUALIFIED',.9,.01,False)])
        self.assertEqual(r['high_return_policy_id'],'HIGH')
        self.assertEqual(r['low_drawdown_policy_id'],'LOW')
        self.assertEqual(r['balanced_policy_id'],'LOW')
        self.assertNotIn('DOMINATED',r['pareto_policy_ids'])
        self.assertNotIn('UNQUALIFIED',r['pareto_policy_ids'])

    def test_user_price_floor_and_real_estate_apply_at_signal_and_actual_open(self):
        a=self.api();dates=pd.bdate_range('2024-01-02',periods=20).strftime('%Y%m%d').tolist()
        frames={d:pd.DataFrame([
            factor_row('600000.SH',d,sector_id='801180.SI'),
            factor_row('600001.SH',d,valuation_close=4.99),
            factor_row('600002.SH',d,valuation_close=5.,execution_open=5.),
            factor_row('600003.SH',d,valuation_close=5.1,execution_open=4.9),
            factor_row('600004.SH',d,sector_id='TEST' if i==0 else '801180.SI'),
        ]) for i,d in enumerate(dates)}
        with tempfile.TemporaryDirectory() as tmp:
            a.replay_factor(SearchMarket(frames),policy=a.FactorPolicy(min_price=5.),
                sessions=dates,config=load_rotation_config(),cost_scenario='base',output_dir=Path(tmp)/'cell',run_id='test')
            fills=pd.read_csv(Path(tmp)/'cell/fills.csv.gz')
            self.assertEqual(set(fills.loc[fills.side=='BUY','code']),{'600002.SH'})

    def test_isolated_parallel_accounts_match_serial_financial_results(self):
        a=self.api();pipeline=importlib.import_module('core.pipeline.factor_portfolio_research')
        dates=pd.bdate_range('2024-01-02',periods=20).strftime('%Y%m%d').tolist()
        market=SearchMarket({d:pd.DataFrame([factor_row('600000.SH',d)]) for d in dates})
        with tempfile.TemporaryDirectory() as tmp:
            for workers in (1,4):
                jobs=[dict(policy=a.FactorPolicy(vol_control=v),sessions=dates,cost_scenario='base',
                    output_dir=Path(tmp)/str(workers)/str(v),run_id='test') for v in (False,True)]
                pipeline.run_cells(market,None,load_rotation_config(),jobs,workers)
            for v in (False,True):
                for file,cols in [('fills',['code','trade_date','side','quantity','price','fee_cents']),
                                  ('daily_nav',['date','nav_cents','cash_cents'])]:
                    pd.testing.assert_frame_equal(pd.read_csv(Path(tmp)/'1'/str(v)/f'{file}.csv.gz')[cols],
                        pd.read_csv(Path(tmp)/'4'/str(v)/f'{file}.csv.gz')[cols])

    def test_pipeline_freezes_selection_before_later_data_and_never_reselects(self):
        api=importlib.import_module('core.pipeline.factor_portfolio_research')
        dates=pd.bdate_range('2023-01-02',periods=801).strftime('%Y%m%d').tolist()
        with tempfile.TemporaryDirectory() as tmp:
            root,out=Path(tmp)/'source',Path(tmp)/'result';root.mkdir()
            write_json(root/'rank_dataset_manifest.json',{})
            pd.DataFrame(columns=['event_id','code']).to_parquet(root/'corporate_actions.parquet')
            dataset=dict(windows={'test':dates[-132:]},scope_flags=[],roster_count=1)
            calls=[]
            def load(root,manifest,features,start,end):
                if start>=dates[-132]: self.assertTrue((out/'selection_freeze.json').exists())
                return None
            def replay(market,*,policy,split,cost_scenario,output_dir,**kwargs):
                value={'SCORE':.02,'LEADER':.03,'DEFENSIVE':.025}[policy.ranking]-(.005 if policy.diversify else 0)
                if split.startswith('holdout') and policy.ranking!='SCORE': value=-.01
                calls.append((policy.policy_id,split,cost_scenario))
                metrics=dict(status='OK',strategy_id=policy.policy_id,split=split,cost_scenario=cost_scenario,
                    net_return=value,max_drawdown=.01 if policy.vol_control else .04,
                    closed_trade_count=12,winning_closed_trade_count=6,closed_trade_win_rate=.5,
                    traded_date_count=11,open_position_count=0,fees_total=100.,modeled_slippage_total=200.)
                output_dir.mkdir(parents=True);write_json(output_dir/'metrics.json',metrics)
                return {'metrics':metrics}
            with patch.object(api,'_verified_source',return_value=(dataset,{'sessions':dates})),\
                 patch.object(api,'load_factor_market',side_effect=load),\
                 patch.object(api,'replay_factor',side_effect=replay),patch('builtins.print'):
                result=api.run_factor_portfolio(root,out,min_price=5.,workers=1)
                self.assertEqual(len(calls),152)
                frozen=json.loads((out/'selection_freeze.json').read_text())
                self.assertEqual(result['selection'],frozen['selection'])
                self.assertEqual(result['verdict'],'KEEP_RESTRICTED_BASELINE')
                self.assertTrue(api.run_factor_portfolio(root,out,min_price=5.,workers=1)['reused'])
                (out/'REPORT.md').write_text('tampered')
                with self.assertRaises(ContractError): api.run_factor_portfolio(root,out,min_price=5.,workers=1)


if __name__=='__main__': unittest.main()
