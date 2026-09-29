import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core.backtest.strategy_search_v2 import SearchMarket
from core.pipeline.high_win_research import run_jobs, freeze_selection, load_suite, _read
from core.pipeline.prism_compare_config import file_sha256, write_json
from core.pipeline.rank_rotation_research import load_rotation_config
from core.strategies.high_win_suite import HighWinPolicy, policy_grid
from core.technical_v2.contracts import ContractError
from scripts.high_win_backtest import parser
from tests.v2.test_factor_portfolio_research import factor_row
from tests.v2.test_high_win_metrics import selection_cells


class HighWinPipelineTests(unittest.TestCase):
    def test_serial_spawn_account_financials_and_verified_reuse(self):
        dates=pd.bdate_range('2024-01-02',periods=20).strftime('%Y%m%d').tolist()
        frames={d:pd.DataFrame([factor_row('600000.SH',d,S02_CONFIRMED=True,S02_anchor=dates[0],
            high_win_rank_score=.9)]) for d in dates}
        market=SearchMarket(frames);config=load_rotation_config();binding={'fixture':'fixed'}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def jobs(where):
                return [dict(policy=HighWinPolicy('S02'),sessions=dates,split=f'selection_{i}',
                    cost_scenario=cost,output_dir=root/where/'S02_CONFIRMED'/f'selection_{i}'/cost)
                    for i in (1,2) for cost in ('base','stress')]
            serial=run_jobs(market,None,config,jobs('serial'),binding,1)
            parallel=run_jobs(market,None,config,jobs('parallel'),binding,4)
            by_key=lambda rows:{r['key']:r['metrics'] for r in rows}
            self.assertEqual(by_key(serial),by_key(parallel))
            again=run_jobs(market,None,config,jobs('serial'),binding,1)
            self.assertTrue(all(r['reused'] for r in again))
            self.assertEqual(by_key(serial),by_key(again))
            cell=Path(jobs('serial')[0]['output_dir'])
            (cell/'trades.csv').write_text('tampered')
            resumed=run_jobs(market,None,config,jobs('serial'),binding,1)
            self.assertEqual(sum(r['reused'] for r in resumed),3)
            self.assertEqual(by_key(serial),by_key(resumed))
            self.assertTrue(list((root/'serial'/'failed_attempts').iterdir()))

    def test_freeze_reads_only_early_and_is_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)
            cells=[]
            for policy in [*[p.policy_id for p in policy_grid()],'BASE_ENV','BASE_DEF']:
                cells.extend(selection_cells(policy))
            write_json(output/'selection_cells.json',cells)
            prepared=dict(binding={'smoke':False})
            with patch('core.pipeline.high_win_research._load_prepared',return_value=prepared):
                frozen=freeze_selection(output);before=file_sha256(output/'selection_freeze.json')
                write_json(output/'review_cells.json',[{'fake_later_winner':'S08_STRICT','return':9999}])
                self.assertEqual(freeze_selection(output),frozen)
                self.assertEqual(file_sha256(output/'selection_freeze.json'),before)
                cells[0]['wins']=0
                write_json(output/'selection_cells.json',cells)
                with self.assertRaises(ContractError): freeze_selection(output)

    def test_config_fixed_and_cli_stages(self):
        self.assertEqual(load_suite()['expected_accounts'],252)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json';config=load_suite();config['selection_gates']['win_rate']=.64
            write_json(path,config)
            with self.assertRaises(ContractError): load_suite(path)
        for stage in ('prepare','screen','freeze','review','report','all'):
            args=parser().parse_args([stage,'--experiment-root','source','--output-root','out','--workers','1'])
            self.assertEqual(args.stage,stage)
        self.assertTrue(parser().parse_args(['--all','--experiment-root','source','--output-root','out']).all_stages)


if __name__=='__main__': unittest.main()
