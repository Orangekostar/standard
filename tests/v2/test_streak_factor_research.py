import importlib
import importlib.util
import unittest
import tempfile
from pathlib import Path

import pandas as pd


class StreakFactorTest(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.streak_factor_research'))
        return importlib.import_module('scripts.streak_factor_research')

    def frame(self,values):
        return pd.DataFrame({'code':['600000.SH']*len(values),
            'date':pd.bdate_range('2024-01-01',periods=len(values)).strftime('%Y%m%d'),
            'comparison_close':values,'comparison_open':values,'real_bar':True})

    def test_one_anchor_before_run_not_each_rising_day(self):
        x=self.api().label_runs(self.frame([11,10,11,12,13,14,13,12,11]))
        positive=x[x.onset_pool & x.label3]
        self.assertEqual(positive.date.tolist(),['20240102'])
        self.assertEqual(positive.run_length.tolist(),[4])
        self.assertEqual(positive.iloc[0].run_end,'20240108')

    def test_missing_day_breaks_streak_and_endpoints_purge_future_labels(self):
        api=self.api()
        f=self.frame([11,10,11,12,13,14,13])
        f.loc[3,'real_bar']=False
        x=api.label_runs(f)
        self.assertFalse(x.iloc[1].label_valid)
        short=api.label_runs(self.frame([11,10,11,12]))
        self.assertFalse(short.iloc[1].label_valid)

    def test_same_date_comparison_removes_shared_market_condition(self):
        api=self.api()
        d=pd.DataFrame({'date':['a']*40+['b']*40,'label3':[False]*20+[True]*20+[False]*20+[True]*20,
                        'F14':[10]*40+[90]*40,'F01':[0]*20+[100]*20+[0]*20+[100]*20})
        shared=api.factor_profile(d,'F14')
        distinct=api.factor_profile(d,'F01')
        self.assertAlmostEqual(shared['matched_mean_difference'],0)
        self.assertAlmostEqual(distinct['matched_mean_difference'],100)

    def test_rule_missing_values_rejected_and_threshold_inclusive(self):
        api=self.api()
        d=pd.DataFrame({'F01':[50,49,float('nan'),99],'F04':[20,20,20,21]})
        rules=[dict(factor='F01',direction='high',threshold=50),dict(factor='F04',direction='low',threshold=20)]
        self.assertEqual(api.rule_mask(d,rules).tolist(),[True,False,False,False])

    def test_no_frozen_rule_never_selects_all_stocks(self):
        api=self.api()
        self.assertFalse(api.rule_mask(pd.DataFrame({'F01':[1,2]}),[]).any())

    def test_common_market_factor_cannot_claim_same_date_selection_lift(self):
        api=self.api()
        frame=pd.DataFrame({'date':['a']*40+['b']*40,'label3':[False]*40+[True]*40,
            'F14':[0]*40+[100]*40,'gross_return3':[0]*80})
        result=api.evaluate(frame,[dict(factor='F14',direction='high',threshold=80)])
        self.assertEqual(result['precision'],1)
        self.assertEqual(result['matched_dates'],0)
        self.assertIsNone(result['matched_gain_block95'])

    def test_discovery_refuses_small_samples_even_when_perfect(self):
        api=self.api()
        frame=pd.DataFrame({factor:[0]*20+[100]*20 for factor in api.SCORES})
        frame['date']='a'
        frame['label3']=[False]*20+[True]*20
        frame['gross_return3']=0
        rules,candidates=api.discover(frame)
        self.assertEqual(rules,[])
        self.assertEqual(len(candidates),42)

    def test_parquet_loader_scores_native_factor_groups_and_exports_onset(self):
        api=self.api()
        f=self.frame([11,10,11,12,13,14,13,12,11])
        for factor in api.FACTORS:
            f[factor]=.2
        for name,value in dict(roster_active=True,is_suspended=False,prediction_status='OK',score3=60.,
            forecast_class3='up',Q03=60000000.,is_risk_warning=False,market_state='RANGE',sector_id='test').items():
            f[name]=value
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'features.parquet'
            f.to_parquet(path,index=False)
            pool,episodes=api.load_split(Path(tmp),{'feature_chunks':[{'path':str(path)}]},'20240101','20240131')
            self.assertEqual(len(episodes),1)
            self.assertEqual(episodes.iloc[0]['T'],60.)
            self.assertEqual(episodes.iloc[0]['F01'],60.)
            self.assertEqual(episodes.iloc[0]['score3'],60.)
            self.assertEqual(pool.label3.tolist(),[True])


if __name__=='__main__':
    unittest.main()
