from __future__ import annotations

import importlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core.backtest.strategy_search_v2 import SearchMarket, replay_search
from core.pipeline.rank_rotation_research import load_rotation_config
from core.pipeline.prism_compare_config import write_json
from core.technical_v2.contracts import ContractError


def row(code, date, **updates):
    result = dict(code=code, date=date, name="NORMAL", execution_open=10., valuation_close=10.,
        comparison_open=9.8, comparison_close=10., adj_factor=1., real_bar=True,
        roster_active=True, instrument_type="stock", listing_board="MAIN_SH",
        list_date="20000101", delist_date=None, sector_id="TEST", is_risk_warning=None,
        is_suspended=False, up_limit=11., down_limit=9., no_price_limit=False,
        prediction_status="OK", score3=75., forecast_class3="up", overextended=False,
        Q01=.02, Q03=60000000., Q05=-.08, F02=.5, F03=.2, market_state="RANGE",
        ret1=.01, ret3=-.01, close_over_ma20=1., market_context_status="OK",
        sector_context_status="OK", sector_log_return20=.02,
        market_breadth20=.7, sector_breadth20=.7)
    return {**result, **updates}


class StabilityTest(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec("core.backtest.stability_research_v2"))
        return importlib.import_module("core.backtest.stability_research_v2")

    def test_grid_has_twelve_prespecified_policies_and_one_separate_control(self):
        api = self.api()
        policies = api.policy_grid()
        self.assertEqual(len(policies), 12)
        self.assertEqual(len({p.policy_id for p in policies}), 12)
        self.assertEqual({p.holding_sessions for p in policies}, {5, 10})
        self.assertEqual({p.range_scale for p in policies}, {.5, 1.})
        self.assertNotIn(api.control_policy().policy_id, {p.policy_id for p in policies})

    def test_absolute_sector_momentum_and_breadth_are_distinct_gates(self):
        api = self.api()
        frame = pd.DataFrame([row("600000.SH", "20240102", sector_log_return20=-.01),
            row("600001.SH", "20240102", market_breadth20=.49),
            row("600002.SH", "20240102")]).set_index("code", drop=False)
        self.assertEqual(api.environment_mask(frame, "SECTOR20").tolist(), [False, True, True])
        self.assertEqual(api.environment_mask(frame, "BREADTH").tolist(), [True, False, True])
        self.assertEqual(api.environment_mask(frame, "SECTOR20_BREADTH").tolist(), [False, False, True])

    def test_unknown_context_and_nonfinite_inputs_cannot_open(self):
        api = self.api()
        for updates in [dict(sector_context_status="UNAVAILABLE"), dict(market_context_status="UNKNOWN"),
                        dict(sector_log_return20=float("nan")), dict(sector_log_return20=float("inf"))]:
            frame = pd.DataFrame([row("600000.SH", "20240102", **updates)])
            self.assertFalse(api.environment_mask(frame, "SECTOR20").iloc[0])
        with self.assertRaises(ContractError):
            api.environment_mask(frame, "UNIMPLEMENTED_GATE")

    def test_six_selection_windows_and_two_later_windows_are_disjoint(self):
        dates = pd.bdate_range("2023-01-02", periods=801).strftime("%Y%m%d").tolist()
        windows = self.api().stability_windows(dates, dates[-132:])
        self.assertEqual(list(windows), [f"selection_{i}" for i in range(1, 7)]+["holdout_1", "holdout_2"])
        self.assertEqual([len(x) for x in windows.values()], [63]*6+[66]*2)
        flat = [d for days in windows.values() for d in days]
        self.assertEqual(flat, dates[-510:])

    def cells(self, returns=None):
        returns = returns or [.02]*6
        return [dict(split=f"selection_{i+1}", cost_scenario=cost, net_return=value,
            status="OK", max_drawdown=.05, closed_trade_count=12,
            winning_closed_trade_count=6, traded_date_count=10, open_position_count=0,
            fees_total=100., modeled_slippage_total=200.)
            for cost in ["base", "stress"] for i,value in enumerate(returns)]

    def test_positive_aggregate_does_not_hide_unstable_quarters(self):
        api = self.api()
        result = api.summarize_stability(api.policy_grid()[0], self.cells([.5,-.01,-.01,-.01,-.01,.5]))
        self.assertFalse(result["qualified"])
        self.assertIn("base:TOO_FEW_PROFITABLE_WINDOWS", result["rejection_reasons"])

    def test_worst_quarter_drawdown_samples_and_open_positions_are_hard_gates(self):
        api = self.api()
        policy = api.policy_grid()[0]
        self.assertTrue(api.summarize_stability(policy, self.cells())["qualified"])
        for field,value,reason in [("net_return",-.031,"QUARTER_LOSS_ABOVE_LIMIT"),
                ("max_drawdown",.101,"DRAWDOWN_ABOVE_LIMIT"),
                ("open_position_count",1,"UNSETTLED_AFTER_TAIL"),
                ("status","NAV_INCOMPLETE","INCOMPLETE_NAV")]:
            cells = self.cells()
            cells[-1][field] = value
            self.assertIn("stress:"+reason, api.summarize_stability(policy,cells)["rejection_reasons"])
        cells = self.cells()
        for cell in cells:
            cell["closed_trade_count"] = 1
        self.assertIn("base:TOO_FEW_CLOSED_TRADES", api.summarize_stability(policy,cells)["rejection_reasons"])

    def test_aggregation_refuses_holdout_duplicate_or_missing_cost_cells(self):
        api = self.api()
        cells = self.cells()
        wrong = [dict(cells[0],split="holdout_1"), *cells[1:]]
        for rows in [wrong, cells[:-1], [*cells,cells[0]]]:
            with self.assertRaises(ContractError):
                api.summarize_stability(api.policy_grid()[0],rows)

    def test_selection_prefers_weak_quarter_strength_over_high_mean(self):
        api = self.api()
        policies = api.policy_grid()
        high_mean = api.summarize_stability(policies[0],self.cells([.005]*5+[.9]))
        steady = api.summarize_stability(policies[1],self.cells([.025]*6))
        selection = api.select_stability([high_mean,steady])
        self.assertEqual(selection["chosen_policy_id"],policies[1].policy_id)
        steady["qualified"] = high_mean["qualified"] = False
        self.assertIsNone(api.select_stability([steady,high_mean])["chosen_policy_id"])

    def test_half_range_budget_uses_signal_day_and_preserves_t_plus_one(self):
        api = self.api()
        dates = pd.bdate_range("2024-01-02",periods=20).strftime("%Y%m%d").tolist()
        frames = {date:pd.DataFrame([row(f"60000{i}.SH",date,
            market_state="RANGE" if date==dates[0] else "TREND_EXPANSION") for i in range(3)]) for date in dates}
        policy = api.EnvironmentPolicy(5,"SECTOR20",.5)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/"replay"
            result = api.replay_environment(SearchMarket(frames),sessions=dates,policy=policy,
                config=load_rotation_config(),cost_scenario="base",output_dir=output,
                run_id="fixture",split="selection_1")
            fills = pd.read_csv(output/"fills.csv.gz",dtype={"trade_date":str})
            buys = fills.loc[fills.side.eq("BUY")].iloc[:3]
            spend = buys.quantity*buys.price+buys.fee_cents/100
            self.assertTrue(spend.le(1000000/6).all())
            self.assertTrue(spend.gt(160000).all())
            self.assertTrue(buys.trade_date.eq(dates[1]).all())
            self.assertEqual(result["metrics"]["open_position_count"],0)
            self.assertEqual(result["metrics"]["peak_position_count"],3)
            self.assertTrue(pd.read_csv(output/"closed_trades.csv.gz").holding_sessions.ge(1).all())

    def test_control_matches_original_replay_fills_and_nav(self):
        api = self.api()
        dates = pd.bdate_range("2024-01-02",periods=24).strftime("%Y%m%d").tolist()
        frames = {d:pd.DataFrame([row(f"60000{i}.SH",d) for i in range(3)]) for d in dates}
        policy = api.control_policy()
        with tempfile.TemporaryDirectory() as tmp:
            old,new = Path(tmp)/"old",Path(tmp)/"new"
            replay_search(SearchMarket(frames),sessions=dates,candidate=policy.candidate,
                config=load_rotation_config(),cost_scenario="base",output_dir=old,run_id="old")
            api.replay_environment(SearchMarket(frames),sessions=dates,policy=policy,
                config=load_rotation_config(),cost_scenario="base",output_dir=new,run_id="new")
            for name,columns in [("daily_nav",["date","nav_cents","cash_cents","position_value_cents"]),
                                 ("fills",["code","trade_date","side","quantity","price","fee_cents"])]:
                pd.testing.assert_frame_equal(pd.read_csv(old/f"{name}.csv.gz")[columns],
                                              pd.read_csv(new/f"{name}.csv.gz")[columns])

    def test_missing_market_date_is_rejected_before_writing_account(self):
        api = self.api()
        dates = pd.bdate_range("2024-01-02",periods=20).strftime("%Y%m%d").tolist()
        frames = {d:pd.DataFrame([row("600000.SH",d)]) for d in dates[:-1]}
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/"missing"
            with self.assertRaises(ContractError):
                api.replay_environment(SearchMarket(frames),sessions=dates,policy=api.policy_grid()[0],
                    config=load_rotation_config(),cost_scenario="base",output_dir=output,run_id="missing")
            self.assertFalse(output.exists())


class StabilityPipelineTest(unittest.TestCase):
    def pipeline(self):
        self.assertIsNotNone(importlib.util.find_spec("core.pipeline.stability_research"))
        return importlib.import_module("core.pipeline.stability_research")

    def exercise_pipeline(self, discovery_return, holdout_return):
        api = self.pipeline()
        dates = pd.bdate_range("2023-01-02",periods=801).strftime("%Y%m%d").tolist()
        with tempfile.TemporaryDirectory() as tmp:
            root,output = Path(tmp)/"data",Path(tmp)/"results"
            root.mkdir()
            write_json(root/"rank_dataset_manifest.json",{})
            pd.DataFrame(columns=["event_id","code"]).to_parquet(root/"corporate_actions.parquet",index=False)
            dataset = dict(windows={"test":dates[-132:]},scope_flags=["REUSED_HOLDOUT"],roster_count=1)
            features = dict(sessions=dates)
            loads,holdout_policies = [],set()
            def market_loader(root_arg, dataset_arg, features_arg, start, end):
                loads.append((start,end))
                if start >= dates[-132]:
                    self.assertTrue((output/"selection_freeze.json").exists())
                else:
                    self.assertFalse((output/"selection_freeze.json").exists())
                return None
            def replay(market, **kwargs):
                split,cost,policy = kwargs["split"],kwargs["cost_scenario"],kwargs["policy"]
                holdout = split.startswith("holdout_")
                if holdout:
                    freeze = json.loads((output/"selection_freeze.json").read_text())
                    self.assertIn(policy.policy_id,[freeze["holdout_policy_id"],api.control_policy().policy_id])
                    holdout_policies.add(policy.policy_id)
                value = holdout_return if holdout else discovery_return
                metrics = dict(strategy_id=policy.policy_id,split=split,cost_scenario=cost,
                    start_date=kwargs["sessions"][0],end_date=kwargs["sessions"][-1],
                    net_return=value,status="OK",max_drawdown=.05,closed_trade_count=12,
                    winning_closed_trade_count=6,closed_trade_win_rate=.5,traded_date_count=10,
                    open_position_count=0,fees_total=100.,modeled_slippage_total=200.,
                    average_holding_sessions=5.,unrealized_pnl_cny=0.,initial_nav=1000000.,
                    final_nav=1000000.*(1+value))
                write_json(Path(kwargs["output_dir"])/"metrics.json",metrics)
                return {"metrics":metrics}
            with patch.object(api,"_verified_source",return_value=(dataset,features)), \
                 patch.object(api,"_load_market",side_effect=market_loader), \
                 patch.object(api,"replay_environment",side_effect=replay) as replay_mock, \
                 patch("builtins.print"):
                result = api.run_stability(root,output)
                self.assertEqual(replay_mock.call_count,164)
                self.assertEqual(len(holdout_policies),2)
                self.assertEqual(loads,[(dates[-510],dates[-133]),(dates[-132],dates[-1])])
                reused = api.run_stability(root,output)
                self.assertTrue(reused["reused"])
                self.assertEqual(replay_mock.call_count,164)
                (output/"RESEARCH_REPORT.md").write_text("changed")
                with self.assertRaises(ContractError):
                    api.run_stability(root,output)
            return result

    def test_freeze_before_holdout_and_failed_holdout_cannot_reselect(self):
        result = self.exercise_pipeline(.02,-.01)
        self.assertEqual(result["verdict"],"SELECTION_PASS_HOLDOUT_FAIL")
        self.assertIsNotNone(result["selection"]["chosen_policy_id"])
        self.assertEqual(len(result["holdout_rejections"]),4)

    def test_successful_diagnostic_does_not_promote_unqualified_discovery(self):
        result = self.exercise_pipeline(0.,.05)
        self.assertEqual(result["verdict"],"NO_QUALIFIED_CANDIDATE")
        self.assertIsNone(result["selection"]["chosen_policy_id"])
        self.assertEqual(result["holdout_rejections"],[])


if __name__ == "__main__":
    unittest.main()
