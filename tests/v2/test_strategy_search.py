from __future__ import annotations

import importlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from core.pipeline.prism_compare_config import file_sha256, load_config, write_json
from core.pipeline.prism_compare_data import REPLAY_COLUMNS
from core.data.v2_store import V2Store
from core.technical_v2.contracts import ContractError, sha256_json


def market_row(code="600000.SH", date="20240102", **updates):
    return dict(code=code, date=date, name="NORMAL", execution_open=10., valuation_close=10.,
        comparison_open=9.8, comparison_close=10., adj_factor=1., real_bar=True,
        roster_active=True, instrument_type="stock", listing_board="MAIN_SH",
        list_date="20000101", delist_date=None, sector_id="UNKNOWN", is_risk_warning=None,
        is_suspended=False, up_limit=11., down_limit=9., no_price_limit=False,
        prediction_status="OK", score3=75., forecast_class3="up", overextended=False,
        Q01=.02, Q03=60000000., Q05=-.08, F02=.5, F03=.2, market_state="RANGE",
        ret1=.02, ret3=-.06, close_over_ma20=1., **updates)


class _SearchAPI:
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec("core.backtest.strategy_search_v2"),
                             "bounded strategy search is not implemented")
        return importlib.import_module("core.backtest.strategy_search_v2")


class SearchTest(_SearchAPI, unittest.TestCase):

    def test_candidates_are_36_unique_fixed_rules_with_bounded_holding(self):
        candidates = self.api().candidate_grid()
        self.assertEqual(len(candidates), 36)
        self.assertEqual(len({item.candidate_id for item in candidates}), 36)
        self.assertEqual({item.max_holding_sessions for item in candidates}, {3, 5, 10})
        self.assertEqual({item.family for item in candidates}, {"TREND_PULLBACK", "REBOUND", "LOW_FREQ_UP"})
        self.assertEqual({item.take_profit for item in candidates}, {.02, .04})

    def test_common_entry_constraints_exclude_untradeable_or_stressed_rows(self):
        rows = [market_row()]
        for i, update in enumerate([dict(code="300001.SZ"), dict(name="*ST X"),
                dict(real_bar=False), dict(is_suspended=None), dict(market_state="STRESS"),
                dict(Q03=None), dict(prediction_status="INSUFFICIENT_FEATURES")], 1):
            row = market_row(code=f"60000{i}.SH")
            row.update(update)
            rows.append(row)
        scores = self.api().entry_scores(pd.DataFrame(rows).set_index("code"), "LOW_FREQ_UP", 0)
        self.assertEqual(scores.dropna().index.tolist(), ["600000.SH"])

    def test_rebound_is_not_mislabeled_as_up_and_needs_actual_confirmation(self):
        row = market_row()
        row.update(score3=30., forecast_class3="down")
        frame = pd.DataFrame([row]).set_index("code")
        api = self.api()
        self.assertTrue(api.entry_scores(frame, "LOW_FREQ_UP", 0).isna().all())
        self.assertTrue(api.entry_scores(frame, "REBOUND", 0).notna().all())
        frame.loc[:, "ret1"] = -.01
        self.assertTrue(api.entry_scores(frame, "REBOUND", 0).isna().all())

    def test_stronger_entry_and_overextension_filter_are_real_gates(self):
        frame = pd.DataFrame([market_row()]).set_index("code")
        frame.loc[:, "score3"] = 70.
        api = self.api()
        self.assertTrue(api.entry_scores(frame, "LOW_FREQ_UP", 0).notna().all())
        self.assertTrue(api.entry_scores(frame, "LOW_FREQ_UP", 1).isna().all())
        frame.loc[:, "overextended"] = True
        self.assertTrue(api.entry_scores(frame, "LOW_FREQ_UP", 0).isna().all())

    def test_feature_enrichment_has_no_future_dependence_or_missing_bar_fill(self):
        dates = pd.bdate_range("2024-01-02", periods=30).strftime("%Y%m%d").tolist()
        frame = pd.DataFrame([dict(code="600000.SH", date=date, comparison_close=10.+i*.1)
                              for i, date in enumerate(dates)])
        api = self.api()
        before = api.enrich_features(frame)
        changed = frame.copy()
        changed.loc[25:, "comparison_close"] = 999.
        after = api.enrich_features(changed)
        pd.testing.assert_frame_equal(before.iloc[:25], after.iloc[:25])
        frame.loc[22, "comparison_close"] = None
        missing = api.enrich_features(frame)
        self.assertTrue(pd.isna(missing.iloc[23].ret1))

    def test_selection_windows_are_contiguous_disjoint_and_before_holdout(self):
        dates = pd.bdate_range("2023-01-02", periods=400).strftime("%Y%m%d").tolist()
        windows = self.api().search_windows(dates, dates[-50:])
        selected = [date for key, values in windows.items() if key != "holdout" for date in values]
        self.assertEqual(len(selected), 189)
        self.assertEqual(len(set(selected)), 189)
        self.assertEqual(selected, dates[161:350])
        self.assertEqual(windows["holdout"], dates[-50:])
        with self.assertRaises(ContractError):
            self.api().search_windows(dates, [dates[-3], dates[-1]])

    def test_sample_profit_drawdown_and_unsettled_gates_reject_false_champions(self):
        api = self.api()
        valid = dict(closed_trade_count=30, traded_date_count=20, status="OK", net_return=.02,
                     max_drawdown=.1, open_position_count=0)
        self.assertEqual(api.qualification(valid), [])
        for field, value, reason in [("closed_trade_count", 29, "TOO_FEW_CLOSED_TRADES"),
                ("traded_date_count", 19, "TOO_FEW_TRADED_DATES"),
                ("net_return", 0., "NONPOSITIVE_NET_RETURN"),
                ("max_drawdown", .15001, "DRAWDOWN_ABOVE_LIMIT"),
                ("open_position_count", 1, "UNSETTLED_AFTER_TAIL"),
                ("status", "NAV_INCOMPLETE", "INCOMPLETE_NAV")]:
            self.assertIn(reason, api.qualification({**valid, field: value}))

    def test_choose_only_qualified_selection_win_rate_not_holdout(self):
        rows = [dict(candidate_id="a", qualified=False, win_rate=.99, net_return=-.1, max_drawdown=.1, holdout_win_rate=1.),
                dict(candidate_id="b", qualified=True, win_rate=.7, net_return=.02, max_drawdown=.1, holdout_win_rate=.1),
                dict(candidate_id="c", qualified=True, win_rate=.6, net_return=.1, max_drawdown=.05, holdout_win_rate=.99)]
        result = self.api().choose_candidate(rows)
        self.assertEqual(result["chosen_candidate_id"], "b")
        rows[1]["qualified"] = rows[2]["qualified"] = False
        result = self.api().choose_candidate(rows)
        self.assertIsNone(result["chosen_candidate_id"])
        self.assertEqual(result["diagnostic_candidate_id"], "a")


class SearchReplayTest(_SearchAPI, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dates = pd.bdate_range("2024-01-02", periods=20).strftime("%Y%m%d").tolist()
        self.config = load_config()
        self.config["rank_rotation"] = dict(horizon=3, max_names=3, risk_warning_unknown="ALLOW_RESEARCH_ONLY")

    def frames(self):
        return {date: pd.DataFrame([market_row(code=f"60000{i}.SH", date=date) for i in range(4)]).set_index("code", drop=False)
                for date in self.dates}

    def replay(self, frames, family="LOW_FREQ_UP", **kwargs):
        api = self.api()
        candidate = next(item for item in api.candidate_grid() if item.family == family
                         and item.strength == 0 and item.max_holding_sessions == 3 and item.take_profit == .02)
        result = api.replay_search(frames, sessions=self.dates, candidate=candidate,
            config=self.config, cost_scenario="base", output_dir=self.root/"result", run_id="fixture", **kwargs)
        return result, self.root/"result"

    def read(self, root, name):
        return pd.read_csv(root/f"{name}.csv.gz", dtype={"date":str,"trade_date":str})

    def test_fixed_expiry_low_turnover_next_open_and_flat_tail(self):
        frames = self.frames()
        for frame in list(frames.values())[1:]:
            frame.loc["600003.SH", "score3"] = 99.
        result, root = self.replay(frames)
        fills = self.read(root, "fills")
        first = fills.loc[fills.code.eq("600000.SH")].iloc[:2]
        self.assertEqual(first.side.tolist(), ["BUY", "SELL"])
        self.assertEqual(first.trade_date.tolist(), [self.dates[1], self.dates[4]])
        self.assertEqual(result["metrics"]["rotation_exit_count"], 0)
        self.assertEqual(result["metrics"]["open_position_count"], 0)
        self.assertEqual(result["metrics"]["peak_position_count"], 3)
        self.assertTrue(result["metrics"]["fixed_holding_expiry"])
        self.assertTrue(self.read(root,"daily_nav").cash_cents.ge(0).all())
        self.assertTrue(self.read(root,"orders").intent_reason.str.startswith("SEARCH_").any())

    def test_close_take_profit_executes_next_open_not_at_intraday_high(self):
        frames = self.frames()
        frames[self.dates[1]]["valuation_close"] = 10.25
        frames[self.dates[1]]["comparison_close"] = 10.25
        frames[self.dates[2]]["execution_open"] = 10.26
        _, root = self.replay(frames)
        sells = self.read(root,"fills").query("side == 'SELL'")
        self.assertEqual(sells.iloc[0].trade_date, self.dates[2])
        self.assertAlmostEqual(sells.iloc[0].price, 10.24)
        self.assertIn("SEARCH_TAKE_PROFIT", self.read(root,"closed_trades").exit_reason.tolist())

    def test_rebound_down_forecast_does_not_cause_immediate_down_exit(self):
        frames = self.frames()
        for frame in frames.values():
            frame["score3"] = 30.
            frame["forecast_class3"] = "down"
        result, root = self.replay(frames, family="REBOUND")
        self.assertGreater(result["metrics"]["filled_buy_count"], 0)
        self.assertEqual(result["metrics"]["down_exit_count"], 0)
        self.assertEqual(self.read(root,"closed_trades").holding_sessions.iloc[0], 3)

    def test_limit_down_exit_cannot_create_capacity_or_hide_open_loss(self):
        frames = self.frames()
        for frame in list(frames.values())[2:]:
            frame["execution_open"] = 9.
            frame["valuation_close"] = 9.
            frame["comparison_close"] = 9.
        result, root = self.replay(frames)
        self.assertEqual(result["metrics"]["filled_sell_count"], 0)
        self.assertEqual(result["metrics"]["filled_buy_count"], 3)
        self.assertEqual(result["metrics"]["open_position_count"], 3)
        self.assertLess(result["metrics"]["unrealized_pnl_cny"], 0)
        self.assertIn("UNSETTLED_AFTER_TAIL", self.api().qualification(result["metrics"]))

    def test_adv_budget_uses_previous_signal_data(self):
        self.config["shared_portfolio"]["initial_capital_cny"] = 100000000
        frames = self.frames()
        frames[self.dates[1]]["Q03"] = 100000000000.
        _, root = self.replay(frames)
        initial = self.read(root,"fills").query("side == 'BUY'").iloc[:3]
        self.assertTrue((initial.quantity*initial.price + initial.fee_cents/100).le(600000.).all())

    def test_future_close_does_not_change_first_open_fills(self):
        frames = self.frames()
        frames[self.dates[1]]["valuation_close"] = 1.
        frames[self.dates[1]]["comparison_close"] = 1.
        frames[self.dates[1]]["score3"] = 1.
        frames[self.dates[1]]["forecast_class3"] = "down"
        _, root = self.replay(frames)
        initial = self.read(root,"fills").query("side == 'BUY'").iloc[:3]
        self.assertEqual(initial.code.tolist(), ["600000.SH", "600001.SH", "600002.SH"])
        self.assertTrue(initial.price.eq(10.01).all())


class SearchPipelineTest(_SearchAPI, unittest.TestCase):
    def pipeline(self):
        self.assertIsNotNone(importlib.util.find_spec("core.pipeline.strategy_search_research"),
                             "frozen search orchestration is not implemented")
        return importlib.import_module("core.pipeline.strategy_search_research")

    def test_config_rejects_unimplemented_intraday_or_return_aggregation_policy(self):
        api = self.pipeline()
        policy = api.load_search_config()["strategy_search"]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/"policy.json"
            for key,value in [("trigger_timing","INTRADAY_TOUCH"),
                              ("return_aggregation","FAKE_CONTINUOUS_COMPOUNDING")]:
                path.write_text(json.dumps({**policy,key:value}))
                with self.assertRaises(ContractError):
                    api.load_search_config(path)

    def test_aggregation_refuses_holdout_and_unmatched_cost_windows(self):
        api = self.pipeline()
        candidate = self.api().candidate_grid()[0]
        for cells in [[dict(split="holdout",cost_scenario="base")],
                      [dict(split="selection_1",cost_scenario="base")]]:
            with self.assertRaises(ContractError):
                api.summarize_candidate(candidate,cells)

    def test_aggregate_win_rate_weights_closed_trades_and_stress_is_a_gate(self):
        candidate = self.api().candidate_grid()[0]
        cells = []
        for cost in ["base", "stress"]:
            for index, (closed, wins) in enumerate([(10, 8), (20, 10), (30, 12)]):
                cells.append(dict(split=f"selection_{index+1}", cost_scenario=cost,
                    closed_trade_count=closed, winning_closed_trade_count=wins, traded_date_count=20,
                    net_return=.02, max_drawdown=.1, status="OK", open_position_count=0,
                    fees_total=10., modeled_slippage_total=20., filled_buy_count=closed,
                    average_holding_sessions=3., unrealized_pnl_cny=0.))
        api = self.pipeline()
        result = api.summarize_candidate(candidate, cells)
        self.assertAlmostEqual(result["win_rate"], .5)
        self.assertTrue(result["qualified"])
        cells[-1]["net_return"] = -.2
        result = api.summarize_candidate(candidate, cells)
        self.assertFalse(result["qualified"])
        self.assertIn("stress:NONPOSITIVE_NET_RETURN", result["rejection_reasons"])

    def test_frozen_search_and_holdout_reuse_are_bound_to_artifacts(self):
        from core.pipeline.rank_rotation_research import load_rotation_config
        api = self.pipeline()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = root/"prepared"
            prepared.mkdir()
            config = api.load_search_config()
            config["strategy_search"].update(selection_window_count=2, selection_window_sessions=20,
                minimum_context_sessions=0, min_closed_trades=1, min_traded_dates=1)
            base = load_rotation_config()
            dates = pd.bdate_range("2024-01-02", periods=160).strftime("%Y%m%d").tolist()
            rows = []
            for index, date in enumerate(dates):
                for code in ["600000.SH", "600001.SH"]:
                    price = 10.*1.01**index
                    row = market_row(code, date)
                    row.update(execution_open=price, comparison_open=price, valuation_close=price*1.005,
                        comparison_close=price*1.005, up_limit=price*1.1, down_limit=price*.9)
                    for column in REPLAY_COLUMNS:
                        row.setdefault(column, None)
                    rows.append(row)
            feature_path = prepared/"features.parquet"
            pd.DataFrame(rows).to_parquet(feature_path, index=False)
            pd.DataFrame([dict(code=code,name="NORMAL") for code in ["600000.SH","600001.SH"]]).to_parquet(prepared/"roster.parquet",index=False)
            pd.DataFrame(columns=["event_id","code"]).to_parquet(prepared/"corporate_actions.parquet",index=False)
            write_json(prepared/"data_audit.json",dict(scope_flags=["REUSED_HOLDOUT"]))
            write_json(prepared/"feature_manifest.json",dict(sessions=dates,
                feature_chunks=[dict(path=str(feature_path),sha256=file_sha256(feature_path))]))
            snapshot = prepared/"market_snapshot.db"
            V2Store(snapshot,data_mode="test").migrate()
            manifest = dict(status="COMPLETE", configuration_sha256=sha256_json(base),
                snapshot=dict(snapshot_db=str(snapshot),snapshot_sha256=file_sha256(snapshot)),
                windows=dict(test=dates[-20:]), scope_flags=["REUSED_HOLDOUT"], roster_count=2)
            for name,key in [("feature_manifest.json","feature_manifest_sha256"),("roster.parquet","roster_sha256"),
                    ("corporate_actions.parquet","corporate_actions_sha256"),("data_audit.json","data_audit_sha256")]:
                manifest[key]=file_sha256(prepared/name)
            write_json(prepared/"rank_dataset_manifest.json",manifest)
            output=root/"results"
            result=api.run_search(prepared,config,output)
            self.assertEqual(result["status"],"COMPLETE")
            self.assertEqual(len(result["summaries"]),36)
            self.assertIsNotNone(result["selection"]["chosen_candidate_id"])
            frozen=json.loads((output/"selection_freeze.json").read_text())
            self.assertEqual(frozen["selection"],result["selection"])
            self.assertEqual(len(result["holdout_cells"]),2)
            self.assertLessEqual(max(cell["peak_position_count"] for cell in result["holdout_cells"]),3)
            stamp=(output/"selection_freeze.json").stat().st_mtime_ns
            reused=api.run_search(prepared,config,output)
            self.assertTrue(reused["reused"])
            self.assertEqual(stamp,(output/"selection_freeze.json").stat().st_mtime_ns)
            (output/"leaderboard.csv").write_text("tampered\n")
            with self.assertRaises(ContractError):
                api.run_search(prepared,config,output)


if __name__ == "__main__":
    unittest.main()
