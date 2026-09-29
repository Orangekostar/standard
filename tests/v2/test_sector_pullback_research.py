import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core.backtest.stability_research_v2 import EnvironmentPolicy, replay_environment
from core.backtest.strategy_search_v2 import SearchMarket
from core.pipeline.rank_rotation_research import load_rotation_config
from core.pipeline.prism_compare_config import file_sha256, write_json
from core.technical_v2.contracts import ContractError
from tests.v2.test_stability_research import row


class SectorPullbackTest(unittest.TestCase):
    def api(self):
        return importlib.import_module('core.pipeline.sector_pullback_research')

    def context(self):
        dates=pd.bdate_range('2024-01-02',periods=30).strftime('%Y%m%d').tolist()
        return pd.DataFrame(dict(date=dates,namespace='SW_L1',sector_id='TEST',
            index_level=[100.]*27+[99.,98.,97.],amount_cny=[100.]*27+[80.]*3,
            status='OK',membership_changed=False))

    def test_amount_boundary_direction_and_no_future_information(self):
        api=self.api(); source=self.context()
        flags=api.sector_flags(source,source.date.tolist())
        self.assertTrue(flags.iloc[-1].blocked)
        self.assertAlmostEqual(flags.iloc[-1].amount_ratio, .8)
        self.assertAlmostEqual(flags.iloc[-1].return3, -.03)
        pd.testing.assert_frame_equal(flags.iloc[:-1].reset_index(drop=True),
            api.sector_flags(source.iloc[:-1],source.date.iloc[:-1].tolist()))
        source.loc[29,'amount_cny']=81.
        self.assertFalse(api.sector_flags(source,source.date.tolist()).iloc[-1].blocked)
        source.loc[29,['amount_cny','index_level']]=[80.,100.]
        self.assertFalse(api.sector_flags(source,source.date.tolist()).iloc[-1].blocked)

    def test_missing_sessions_bad_context_and_membership_are_unknown(self):
        api=self.api(); source=self.context(); dates=source.date.tolist()
        for field,value in [('status','UNAVAILABLE'),('amount_cny',float('inf')),
                            ('membership_changed',True)]:
            altered=source.copy(); altered.loc[10,field]=value
            self.assertFalse(api.sector_flags(altered,dates).iloc[-1].known)
        self.assertFalse(api.sector_flags(source.drop(index=10),dates).iloc[-1].known)
        with self.assertRaises(ValueError):
            api.sector_flags(pd.concat([source,source.iloc[-1:]]),dates)

    def test_filter_uses_signal_sector_and_does_not_force_existing_exit(self):
        api=self.api(); dates=pd.bdate_range('2024-01-02',periods=20).strftime('%Y%m%d').tolist()
        frames={d:pd.DataFrame([row('600000.SH',d)]) for d in dates}
        flags=pd.DataFrame(dict(date=dates,sector_id='TEST',known=True,
            blocked=[False]+[True]*19))
        with tempfile.TemporaryDirectory() as tmp:
            api.replay_filter(SearchMarket(frames),flags,policy=api.PullbackPolicy(),sessions=dates,
                config=load_rotation_config(),cost_scenario='base',output_dir=Path(tmp)/'new',run_id='test')
            fills=pd.read_csv(Path(tmp)/'new/fills.csv.gz',dtype={'trade_date':str})
            self.assertEqual(fills.loc[fills.side=='BUY','trade_date'].tolist(),[dates[1]])
            self.assertGreater(fills.loc[fills.side=='SELL','trade_date'].iloc[0],dates[2])
        flags.loc[0,'known']=False
        market=api.PullbackMarket(SearchMarket(frames),flags)
        self.assertTrue(market.ranks(dates[0],api.PullbackPolicy().candidate).empty)

    def test_baseline_preserves_fills_and_nav_and_filter_rejects_only_one_sector(self):
        api=self.api(); dates=pd.bdate_range('2024-01-02',periods=20).strftime('%Y%m%d').tolist()
        frames={d:pd.DataFrame([row('600000.SH',d),row('600001.SH',d,sector_id='OTHER')]) for d in dates}
        flags=pd.DataFrame(dict(date=dates,sector_id='TEST',known=True,blocked=True))
        other=flags.assign(sector_id='OTHER',blocked=False)
        market=api.PullbackMarket(SearchMarket(frames),pd.concat([flags,other]))
        self.assertEqual(market.ranks(dates[0],api.PullbackPolicy().candidate).index.tolist(),['600001.SH'])
        with tempfile.TemporaryDirectory() as tmp:
            for name,p in [('old',EnvironmentPolicy(5,'SECTOR20_BREADTH',.5)),
                           ('new',api.PullbackPolicy(avoid_pullback=False))]:
                kwargs=dict(policy=p,sessions=dates,config=load_rotation_config(),cost_scenario='base',
                    output_dir=Path(tmp)/name,run_id='test')
                if name=='old': replay_environment(SearchMarket(frames),**kwargs)
                else: api.replay_filter(SearchMarket(frames),flags,**kwargs)
            for filename,cols in [('fills',['code','trade_date','side','quantity','price','fee_cents']),
                                  ('daily_nav',['date','nav_cents','cash_cents'])]:
                pd.testing.assert_frame_equal(pd.read_csv(Path(tmp)/'old'/f'{filename}.csv.gz')[cols],
                    pd.read_csv(Path(tmp)/'new'/f'{filename}.csv.gz')[cols])

    def test_all_32_fixed_accounts_frozen_before_replay_and_archive_is_immutable(self):
        api=self.api(); dates=pd.bdate_range('2023-01-02',periods=801).strftime('%Y%m%d').tolist()
        with tempfile.TemporaryDirectory() as tmp:
            root,out=Path(tmp)/'source',Path(tmp)/'result';root.mkdir()
            write_json(root/'rank_dataset_manifest.json',{})
            pd.DataFrame(columns=['event_id','code']).to_parquet(root/'corporate_actions.parquet')
            context=self.context();context.to_parquet(root/'context.parquet')
            features=dict(sessions=dates,context={'sectors':dict(path=str(root/'context.parquet'),
                sha256=file_sha256(root/'context.parquet'))})
            dataset=dict(windows={'test':dates[-132:]},scope_flags=[],roster_count=1)
            calls=[]
            def load(root,manifest,features,start,end):
                return SearchMarket({d:pd.DataFrame([row('600000.SH',d)]) for d in dates if start<=d<=end})
            def replay(market,*,policy,split,cost_scenario,output_dir,**kwargs):
                frozen=json.loads((out/'frozen_protocol.json').read_text())
                self.assertEqual(len(frozen['protocol']['policies']),2)
                self.assertEqual(frozen['protocol']['amount_ratio_at_most'],.8)
                calls.append((policy.policy_id,split,cost_scenario))
                metrics=dict(status='OK',strategy_id=policy.policy_id,split=split,cost_scenario=cost_scenario,
                    net_return=.01,max_drawdown=.02,closed_trade_count=12,winning_closed_trade_count=6,
                    closed_trade_win_rate=.5,traded_date_count=11,open_position_count=0,
                    fees_total=100.,modeled_slippage_total=200.)
                output_dir.mkdir(parents=True);write_json(output_dir/'metrics.json',metrics)
                return {'metrics':metrics}
            with patch.object(api,'_verified_source',return_value=(dataset,features)),\
                 patch.object(api,'_load_market',side_effect=load),\
                 patch.object(api,'replay_environment',side_effect=replay),patch('builtins.print'):
                result=api.run_sector_pullback(root,out)
                self.assertEqual(len(calls),32);self.assertEqual(len(set(calls)),32)
                self.assertEqual(len(result['cells']),32)
                self.assertTrue(api.run_sector_pullback(root,out)['reused'])
                (out/'REPORT.md').write_text('tampered')
                with self.assertRaises(ContractError): api.run_sector_pullback(root,out)


if __name__=='__main__':
    unittest.main()
