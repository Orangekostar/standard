import unittest

import numpy as np
import pandas as pd

from core.factors.high_win_features import build_high_win_features, finalize_cross_section


def fixture(n=100):
    dates = pd.bdate_range('2024-01-01', periods=n).strftime('%Y%m%d').tolist()
    context = pd.DataFrame(dict(date=dates, index_level=100*np.exp(np.arange(n)*.001),
        breadth20=.6, status='OK', covered_count=20, coverage=1.))
    sectors = pd.concat([context.assign(sector_id=f'S{i}', namespace='SW_L1',
        index_level=100*np.exp(np.arange(n)*(.001+i*.0001))) for i in range(5)], ignore_index=True)
    c = 10+np.arange(n)*.01
    panel = pd.DataFrame(dict(date=dates, code='600000.SH', sector_id='S0',
        comparison_open=c-.01, comparison_close=c, comparison_high=c+.1,
        comparison_low=c-.1, amount_cny=100.+np.arange(n), real_bar=True))
    return dates, panel, context, sectors


class HighWinFeatureTests(unittest.TestCase):
    def test_volume_endpoints_and_volatility(self):
        dates, p, m, s = fixture()
        f = build_high_win_features(p,m,s,dates,dates[-1])
        t=70
        self.assertAlmostEqual(f.loc[t,'VA'],p.loc[t,'amount_cny']/p.loc[t-20:t-1,'amount_cny'].median())
        self.assertAlmostEqual(f.loc[t,'VR3'],p.loc[t-2:t,'amount_cny'].mean()/p.loc[t-22:t-3,'amount_cny'].median())
        expected=np.log(p.comparison_close/p.comparison_close.shift()).loc[t-19:t].std(ddof=0)
        self.assertAlmostEqual(f.loc[t,'sigma20'],expected)
        self.assertAlmostEqual(f.loc[t,'HH20prev'],p.loc[t-20:t-1,'comparison_high'].max())

    def test_missing_session_not_filled_and_future_invariance(self):
        dates,p,m,s=fixture()
        first=build_high_win_features(p,m,s,dates,dates[80])
        full=build_high_win_features(p,m,s,dates,dates[-1])
        pd.testing.assert_frame_equal(first,full.iloc[:81].reset_index(drop=True))
        missing=build_high_win_features(p.drop(index=65),m,s,dates,dates[-1])
        self.assertTrue(pd.isna(missing.loc[65,'comparison_close']))
        self.assertTrue(pd.isna(missing.loc[70,'sigma20']))
        self.assertTrue(pd.isna(missing.loc[70,'R20']))
        self.assertTrue(pd.isna(missing.loc[70,'MA20']))

    def test_flat_candle_and_cross_section_minimum(self):
        dates,p,m,s=fixture()
        p.loc[70,['comparison_open','comparison_high','comparison_low']]=p.loc[70,'comparison_close']
        f=build_high_win_features(p,m,s,dates,dates[-1])
        self.assertTrue(f.loc[70,['CLV','LW','UW']].isna().all())
        combined=pd.concat([f.assign(code=f'code{i}',RS60=float(i)) for i in range(10)],ignore_index=True)
        ranked=finalize_cross_section(combined)
        self.assertEqual(ranked.loc[(ranked.code=='code9') & (ranked.date==dates[-1]),'rs60_within_sector_pct'].iloc[0],1.)
        self.assertTrue(finalize_cross_section(combined.loc[combined.code!='code9']).rs60_within_sector_pct.isna().all())

    def test_sector_membership_change_uses_current_sector_history(self):
        dates,p,m,s=fixture()
        p.loc[70:,'sector_id']='S4'
        f=build_high_win_features(p,m,s,dates,dates[-1])
        self.assertAlmostEqual(f.loc[70,'sector_ell60'],60*.0014)
        self.assertAlmostEqual(f.loc[69,'sector_ell60'],60*.001)


if __name__=='__main__':
    unittest.main()
