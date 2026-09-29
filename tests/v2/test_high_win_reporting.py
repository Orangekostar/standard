import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from core.pipeline.high_win_reporting import paired_bootstrap
from core.pipeline.prism_compare_config import write_json


class HighWinReportingTests(unittest.TestCase):
    def test_paired_blocks_preserve_same_date_episode_clusters_and_no_reselection(self):
        dates=pd.bdate_range('2024-01-02',periods=20).strftime('%Y%m%d').tolist()
        windows={'selection_1':dates,'review':dates}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for policy in ('BASE_ENV','BASE_DEF','S01_CONFIRMED'):
                for split in windows:
                    for cost in ('base','stress'):
                        out=root/policy/split/cost;out.mkdir(parents=True)
                        values=100_000_000*np.cumprod(1+np.array([.01,-.005]*10))
                        pd.DataFrame({'date':dates,'nav_cents':values}).to_csv(out/'daily_nav.csv.gz',index=False)
                        trades=pd.DataFrame([dict(code=code,entry_date=date,realized_pnl_cents=pnl)
                            for date in dates for code,pnl in (('A',100),('B',-100))])
                        trades.to_csv(out/'trades.csv',index=False)
                        write_json(out/'metrics.json',{'nav_complete':True,'initial_nav':1_000_000})
            one=paired_bootstrap(root,['S01_CONFIRMED'],windows)
            two=paired_bootstrap(root,['S01_CONFIRMED'],windows)
            self.assertEqual(one,two)
            self.assertEqual(one['iterations'],1000)
            self.assertEqual(one['block_sessions'],10)
            for record in one['records']:
                self.assertEqual(record['return_difference_vs_BASE_ENV_interval95'],[0.,0.])
                self.assertEqual(record['win_rate_interval95'],[.5,.5])
                self.assertEqual(record['win_rate_difference_vs_BASE_ENV_interval95'],[0.,0.])
                self.assertEqual(record['valid_trade_resamples'],1000)


if __name__=='__main__': unittest.main()
