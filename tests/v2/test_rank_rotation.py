from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from core.pipeline.prism_compare_config import file_sha256, load_config, write_json
from core.pipeline.prism_compare_data import SnapshotAudit, write_replay_chunk
from core.data.v2_store import V2Store
from core.technical_v2.contracts import ContractError, sha256_json


class RotationPlanTest(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(
            importlib.util.find_spec("core.backtest.rank_rotation_v2"),
            "three-stock rotation strategy is not implemented",
        )
        return importlib.import_module("core.backtest.rank_rotation_v2")

    def row(self, score, forecast="up", eligible=True, status="OK"):
        return dict(score3=score, forecast_class3=forecast,
                    entry_eligible=eligible, prediction_status=status)

    def test_buys_only_top_three_up_sorted_by_current_score(self):
        rows = {
            "600000.SH": self.row(70), "600001.SH": self.row(90),
            "600002.SH": self.row(80), "600003.SH": self.row(99, "flat"),
            "600004.SH": self.row(98, "down"), "600005.SH": self.row(95, eligible=False),
            "600006.SH": self.row(97, status="INSUFFICIENT_FEATURES"),
        }
        plan = self.api().plan_rotation(rows, set())
        self.assertEqual([buy.code for buy in plan.buys], ["600001.SH", "600002.SH", "600000.SH"])
        self.assertEqual(plan.sells, {})

    def test_strictly_higher_score_replaces_current_weakest_holding(self):
        rows = {"600000.SH": self.row(70), "600001.SH": self.row(80),
                "600002.SH": self.row(90), "600003.SH": self.row(75)}
        plan = self.api().plan_rotation(rows, {"600000.SH", "600001.SH", "600002.SH"})
        self.assertEqual(plan.sells, {"600000.SH": "HIGHER_SCORE_ROTATION"})
        self.assertEqual([(buy.code, buy.replaces) for buy in plan.buys], [("600003.SH", "600000.SH")])

    def test_equal_score_preserves_existing_holding(self):
        rows = {"600000.SH": self.row(70), "600001.SH": self.row(80),
                "600002.SH": self.row(90), "600003.SH": self.row(70)}
        plan = self.api().plan_rotation(rows, {"600000.SH", "600001.SH", "600002.SH"})
        self.assertEqual(plan.sells, {})
        self.assertEqual(plan.buys, ())

    def test_down_sells_before_up_fills_vacancy(self):
        rows = {"600000.SH": self.row(30, "down"), "600001.SH": self.row(80),
                "600002.SH": self.row(90), "600003.SH": self.row(65)}
        plan = self.api().plan_rotation(rows, {"600000.SH", "600001.SH", "600002.SH"})
        self.assertEqual(plan.sells, {"600000.SH": "FORECAST_DOWN"})
        self.assertEqual([(buy.code, buy.replaces) for buy in plan.buys], [("600003.SH", None)])

    def test_flat_remains_but_can_be_replaced_by_higher_up(self):
        rows = {"600000.SH": self.row(50, "flat"), "600001.SH": self.row(80),
                "600002.SH": self.row(90)}
        self.assertEqual(self.api().plan_rotation(rows, set(rows)).sells, {})
        rows["600003.SH"] = self.row(65)
        plan = self.api().plan_rotation(rows, {"600000.SH", "600001.SH", "600002.SH"})
        self.assertEqual(plan.sells, {"600000.SH": "HIGHER_SCORE_ROTATION"})

    def test_missing_current_prediction_is_not_invented_as_low_score(self):
        rows = {"600000.SH": self.row(None, "unknown", status="INSUFFICIENT_FEATURES"),
                "600001.SH": self.row(80), "600002.SH": self.row(90), "600003.SH": self.row(75)}
        plan = self.api().plan_rotation(rows, {"600000.SH", "600001.SH", "600002.SH"})
        self.assertEqual(plan.sells, {})
        self.assertEqual(plan.buys, ())

    def test_tied_new_entries_have_deterministic_code_order(self):
        rows = {"600002.SH": self.row(80), "600000.SH": self.row(80), "600001.SH": self.row(80)}
        plan = self.api().plan_rotation(rows, set())
        self.assertEqual([buy.code for buy in plan.buys], ["600000.SH", "600001.SH", "600002.SH"])

    def test_multiple_replacements_do_not_replace_a_newly_selected_candidate(self):
        rows = {"600000.SH": self.row(60), "600001.SH": self.row(65), "600002.SH": self.row(70),
                "600003.SH": self.row(99), "600004.SH": self.row(98), "600005.SH": self.row(97)}
        plan = self.api().plan_rotation(rows, {"600000.SH", "600001.SH", "600002.SH"})
        self.assertEqual([buy.code for buy in plan.buys], ["600003.SH", "600004.SH", "600005.SH"])
        self.assertEqual([buy.replaces for buy in plan.buys], ["600000.SH", "600001.SH", "600002.SH"])

    def test_invalid_horizon_or_holdings_over_capacity_is_rejected(self):
        api = self.api()
        with self.assertRaises(ContractError):
            api.plan_rotation({}, set(), horizon=2)
        with self.assertRaises(ContractError):
            api.plan_rotation({}, {"600000.SH", "600001.SH", "600002.SH", "600003.SH"})


class RotationReplayTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sessions = pd.bdate_range("2024-01-02", periods=12).strftime("%Y%m%d").tolist()
        self.config = load_config()
        self.config["rank_rotation"] = {
            "horizon": 3, "max_names": 3,
            "risk_warning_unknown": "ALLOW_RESEARCH_ONLY",
        }

    def frames(self):
        return [(date, pd.DataFrame([
            dict(code=f"60000{index}.SH", date=date, execution_open=10., valuation_close=10.,
                 adj_factor=1., real_bar=True, roster_active=True, instrument_type="stock",
                 listing_board="MAIN_SH", list_date="20000101", delist_date=None,
                 sector_id="SAME_SECTOR", is_risk_warning=None, is_suspended=False,
                 up_limit=11., down_limit=9., no_price_limit=False, prediction_status="OK",
                 score3=score, forecast_class3="up", score5=20., forecast_class5="down",
                 overextended=True, market_breadth20=.05, expected_net_edge=-.9)
            for index, score in enumerate((90., 80., 70., 60.))
        ])) for date in self.sessions]

    def run_replay(self, frames, name="run", config=None, cost="base", actions=None):
        api = importlib.import_module("core.backtest.rank_rotation_v2")
        self.assertTrue(hasattr(api, "replay_rotation"), "rotation execution is not implemented")
        output = self.root / name
        result = api.replay_rotation(
            frames, sessions=self.sessions, config=config or self.config,
            cost_scenario=cost, output_dir=output, run_id="fixture", split="test",
            corporate_actions=actions,
        )
        return result, output

    def read(self, output, name):
        return pd.read_csv(output / f"{name}.csv.gz", dtype={"date": str, "trade_date": str})

    def test_next_open_entries_ignore_old_gates_and_have_no_fixed_expiry(self):
        result, output = self.run_replay(self.frames())
        fills = self.read(output, "fills")
        self.assertEqual(fills.code.tolist(), ["600000.SH", "600001.SH", "600002.SH"])
        self.assertEqual(fills.trade_date.tolist(), [self.sessions[1]] * 3)
        self.assertTrue(fills.side.eq("BUY").all())
        self.assertGreater(fills.fee_cents.sum(), 0)
        self.assertEqual(result["metrics"]["closed_trade_count"], 0)
        self.assertEqual(result["metrics"]["peak_position_count"], 3)
        nav = self.read(output, "daily_nav")
        self.assertEqual(nav.iloc[0].nav_cents, 100000000)
        self.assertTrue(nav.cash_cents.ge(0).all())
        self.assertTrue(nav.position_count.iloc[1:].eq(3).all())
        self.assertEqual(len(pd.read_csv(output / "remaining_positions.csv")), 3)

    def test_rotation_uses_current_not_entry_score_and_spends_only_actual_sell_proceeds(self):
        frames = self.frames()
        for _, frame in frames[1:]:
            frame.loc[frame.code.eq("600000.SH"), "score3"] = 65.
            frame.loc[frame.code.eq("600003.SH"), "score3"] = 75.
        result, output = self.run_replay(frames)
        fills = self.read(output, "fills")
        day = fills.loc[fills.trade_date.eq(self.sessions[2])]
        self.assertEqual(day.side.tolist(), ["SELL", "BUY"])
        self.assertEqual(day.code.tolist(), ["600000.SH", "600003.SH"])
        closed = self.read(output, "closed_trades")
        self.assertEqual(closed.exit_reason.tolist(), ["HIGHER_SCORE_ROTATION"])
        self.assertEqual(result["metrics"]["peak_position_count"], 3)
        self.assertTrue(self.read(output, "daily_nav").cash_cents.ge(0).all())

    def test_sell_at_down_limit_does_not_free_slot_or_fund_replacement(self):
        frames = self.frames()
        for _, frame in frames[1:]:
            frame.loc[frame.code.eq("600003.SH"), "score3"] = 95.
        for _, frame in frames[2:4]:
            frame.loc[frame.code.eq("600002.SH"), "execution_open"] = 9.
        result, output = self.run_replay(frames)
        fills = self.read(output, "fills")
        self.assertEqual(fills.loc[fills.side.eq("SELL"), "trade_date"].tolist(), [self.sessions[4]])
        self.assertEqual(fills.loc[fills.code.eq("600003.SH"), "trade_date"].tolist(), [self.sessions[4]])
        self.assertEqual(result["metrics"]["peak_position_count"], 3)
        attempts = self.read(output, "execution_attempts")
        self.assertTrue(attempts.reason.eq("REPLACEMENT_EXIT_NOT_FILLED").any())

    def test_forecast_down_exits_next_open_without_same_day_round_trip(self):
        frames = self.frames()
        for _, frame in frames[1:]:
            frame.loc[frame.code.eq("600000.SH"), ["score3", "forecast_class3"]] = [30., "down"]
        _, output = self.run_replay(frames)
        fills = self.read(output, "fills")
        trade = fills.loc[fills.code.eq("600000.SH")]
        self.assertEqual(trade.side.tolist(), ["BUY", "SELL"])
        self.assertEqual(trade.trade_date.tolist(), [self.sessions[1], self.sessions[2]])
        self.assertEqual(self.read(output, "closed_trades").exit_reason.tolist(), ["FORECAST_DOWN"])

    def test_flat_keeps_position_and_equal_up_score_does_not_churn(self):
        frames = self.frames()
        for _, frame in frames[1:]:
            frame.loc[frame.code.eq("600002.SH"), "forecast_class3"] = "flat"
            frame.loc[frame.code.eq("600003.SH"), "score3"] = 70.
        _, output = self.run_replay(frames)
        self.assertEqual(len(self.read(output, "fills")), 3)

    def test_known_st_and_non_mainboard_are_not_bought_but_unknown_is_explicit_research_mode(self):
        frames = self.frames()
        for _, frame in frames:
            frame.loc[0, "name"] = "*ST EXAMPLE"
            frame.loc[1, "code"] = "300001.SZ"
            frame.loc[1, "listing_board"] = "CHINEXT"
            frame.loc[2, "is_risk_warning"] = True
        result, output = self.run_replay(frames)
        self.assertEqual(self.read(output, "fills").code.tolist(), ["600003.SH"])
        self.assertIn("HISTORICAL_ST_UNKNOWN_ALLOWED_FOR_RESEARCH", result["metrics"]["scope_flags"])

    def test_strict_unknown_st_policy_still_blocks_unknown(self):
        config = self.config.copy()
        config["rank_rotation"] = {**self.config["rank_rotation"], "risk_warning_unknown": "BLOCK_NEW_ENTRIES"}
        result, _ = self.run_replay(self.frames(), config=config)
        self.assertEqual(result["metrics"]["filled_buy_count"], 0)
        self.assertEqual(result["metrics"]["net_return"], 0.)

    def test_strict_policy_rechecks_unknown_st_at_open_even_if_signal_day_was_known(self):
        frames = self.frames()
        for _, frame in frames:
            frame["is_risk_warning"] = False
        frames[1][1]["is_risk_warning"] = None
        config = self.config.copy()
        config["rank_rotation"] = {**self.config["rank_rotation"], "risk_warning_unknown": "BLOCK_NEW_ENTRIES"}
        _, output = self.run_replay(frames, config=config)
        self.assertFalse(self.read(output, "fills").trade_date.eq(self.sessions[1]).any())

    def test_newly_known_st_at_open_cannot_fill_a_previous_up_intent(self):
        frames = self.frames()
        frames[1][1].loc[0, "is_risk_warning"] = True
        _, output = self.run_replay(frames)
        day = self.read(output, "fills").loc[lambda frame: frame.trade_date.eq(self.sessions[1])]
        self.assertEqual(set(day.code), {"600001.SH", "600002.SH"})

    def test_implemented_cash_dividend_is_credited_once(self):
        frames = self.frames()
        for _, frame in frames[2:]:
            frame.loc[0, ["execution_open", "valuation_close"]] = 9.
        actions = pd.DataFrame([dict(event_id="dividend", code="600000.SH", record_date=self.sessions[1],
            ex_date=self.sessions[2], pay_date=self.sessions[3], cash_per_share="1", status="implemented")])
        with_dividend, output = self.run_replay(frames, "dividend", actions=actions)
        without_dividend, _ = self.run_replay(frames, "no-dividend")
        quantity = self.read(output, "fills").loc[lambda frame: frame.code.eq("600000.SH"), "quantity"].item()
        self.assertAlmostEqual(with_dividend["metrics"]["final_nav"] - without_dividend["metrics"]["final_nav"], quantity)

    def test_unlisted_bonus_shares_keep_a_slot_after_parent_is_sold(self):
        frames = self.frames()
        for _, frame in frames[1:]:
            frame.loc[2, ["score3", "forecast_class3"]] = [30., "down"]
        actions = pd.DataFrame([dict(event_id="bonus", code="600002.SH", record_date=self.sessions[1],
            ex_date=self.sessions[2], list_date=self.sessions[6], share_ratio=".1", status="implemented")])
        _, output = self.run_replay(frames, "bonus", actions=actions)
        fills = self.read(output, "fills")
        new_buy = fills.loc[fills.code.eq("600003.SH")]
        self.assertEqual(new_buy.trade_date.tolist(), [self.sessions[7]])
        self.assertTrue(self.read(output, "daily_nav").position_count.le(3).all())

    def test_end_positions_include_bonus_shares_not_yet_listed(self):
        frames = self.frames()
        for _, frame in frames[1:]:
            frame.loc[2, ["score3", "forecast_class3"]] = [30., "down"]
        actions = pd.DataFrame([dict(event_id="late-bonus", code="600002.SH", record_date=self.sessions[1],
            ex_date=self.sessions[2], list_date="20240201", share_ratio=".1", status="implemented")])
        result, output = self.run_replay(frames, "late-bonus", actions=actions)
        remaining = pd.read_csv(output / "remaining_positions.csv")
        pending = remaining.loc[remaining.code.eq("600002.SH")]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending.iloc[0].quantity, 3320)
        self.assertEqual(pending.iloc[0].asset_type, "CORPORATE_SHARE_RECEIVABLE")
        self.assertEqual(result["metrics"]["open_position_count"], 3)

    def test_open_execution_never_uses_same_day_close_score_or_volume(self):
        frames = self.frames()
        frames[1][1]["valuation_close"] = np.nan
        frames[1][1]["real_bar"] = False
        frames[1][1]["score3"] = np.nan
        frames[1][1]["forecast_class3"] = "unknown"
        result, output = self.run_replay(frames)
        self.assertEqual(result["metrics"]["filled_buy_count"], 3)
        self.assertTrue(self.read(output, "fills").trade_date.eq(self.sessions[1]).all())

    def test_up_limit_buy_is_rejected_without_counting_a_position(self):
        frames = self.frames()
        frames[1][1].loc[0, "execution_open"] = 11.
        _, output = self.run_replay(frames)
        self.assertEqual(self.read(output, "daily_nav").iloc[1].position_count, 2)
        self.assertFalse(self.read(output, "fills").loc[lambda frame: frame.trade_date.eq(self.sessions[1])].code.eq("600000.SH").any())

    def test_stress_costs_reduce_nav_and_each_slot_stays_near_one_third(self):
        base, output = self.run_replay(self.frames(), "base")
        stress, _ = self.run_replay(self.frames(), "stress", cost="stress")
        self.assertLess(stress["metrics"]["final_nav"], base["metrics"]["final_nav"])
        fills = self.read(output, "fills")
        notionals = fills.quantity * fills.price
        self.assertTrue(notionals.between(332000, 333334).all())

    def test_replay_refuses_to_overwrite_results_or_omit_calendar_days(self):
        self.run_replay(self.frames())
        with self.assertRaises(ContractError):
            self.run_replay(self.frames())
        with self.assertRaises(ContractError):
            self.run_replay(self.frames()[1:], "missing")


class RotationDataTest(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec("core.pipeline.rank_rotation_research"),
                             "isolated rotation research pipeline is not implemented")
        return importlib.import_module("core.pipeline.rank_rotation_research")

    def test_research_universe_excludes_st_and_other_boards_from_context_too(self):
        codes = ["600000.SH", "000001.SZ", "600001.SH", "688001.SH", "300001.SZ", "920001.BJ"]
        roster = pd.DataFrame({"code": codes})
        memberships = pd.DataFrame({"code": codes, "sector_id": "S"})
        actions = pd.DataFrame({"code": codes, "event_id": range(6)})
        audit = SnapshotAudit({"scope_flags": ["HISTORICAL_RISK_WARNING_UNKNOWN"]},
                              ["20240102"], roster, memberships, actions)
        names = pd.DataFrame({"code": codes, "name": ["BANK", "NORMAL", "*ST BAD", "STAR", "GEM", "BSE"]})
        filtered = self.api().filter_research_roster(audit, names)
        self.assertEqual(filtered.roster.code.tolist(), ["600000.SH", "000001.SZ"])
        self.assertEqual(filtered.memberships.code.tolist(), ["600000.SH", "000001.SZ"])
        self.assertEqual(filtered.actions.code.tolist(), ["600000.SH", "000001.SZ"])
        self.assertIn("FROZEN_NAME_FILTER_NOT_HISTORICAL_ST_CLASSIFICATION", filtered.audit["scope_flags"])
        self.assertEqual(len(audit.roster), 6)

    def test_research_config_is_separate_and_does_not_relax_production_or_old_comparison(self):
        config = self.api().load_rotation_config()
        self.assertEqual(config["rank_rotation"]["horizon"], 3)
        self.assertEqual(config["rank_rotation"]["max_names"], 3)
        self.assertEqual(config["rank_rotation"]["risk_warning_unknown"], "ALLOW_RESEARCH_ONLY")
        self.assertEqual(load_config()["data"]["risk_warning_unknown"], "BLOCK_NEW_ENTRIES")

    def prepared_fixture(self, root, config):
        fixture = RotationReplayTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        frames = fixture.frames()[:6]
        sessions = [date for date, _ in frames]
        root.mkdir()
        record = write_replay_chunk(root / "replay.parquet", pd.concat([frame for _, frame in frames]), sessions)
        write_json(root / "feature_manifest.json", {"sessions": sessions, "replay_chunks": [record]})
        write_json(root / "data_audit.json", {"scope_flags": ["SYNTHETIC_TEST_ONLY"]})
        pd.DataFrame({"code": frames[0][1].code, "name": "NORMAL"}).to_parquet(root / "roster.parquet", index=False)
        pd.DataFrame(columns=["code"]).to_parquet(root / "corporate_actions.parquet", index=False)
        manifest = {"status": "COMPLETE", "configuration_sha256": sha256_json(config),
            "snapshot": {"snapshot_sha256": "SYNTHETIC_TEST_ONLY"},
            "feature_manifest_sha256": file_sha256(root / "feature_manifest.json"),
            "roster_sha256": file_sha256(root / "roster.parquet"),
            "corporate_actions_sha256": file_sha256(root / "corporate_actions.parquet"),
            "data_audit_sha256": file_sha256(root / "data_audit.json"),
            "roster_count": 4, "windows": {"validation": sessions[:3], "test": sessions[3:]},
            "scope_flags": ["SYNTHETIC_TEST_ONLY"]}
        write_json(root / "rank_dataset_manifest.json", manifest)

    def test_complete_four_cell_pipeline_is_independent_and_reuse_checks_hashes(self):
        api = self.api()
        self.assertTrue(hasattr(api, "run_rotation"), "four-cell research replay is not implemented")
        with tempfile.TemporaryDirectory() as directory:
            root, output = Path(directory) / "data", Path(directory) / "results"
            config = api.load_rotation_config()
            self.prepared_fixture(root, config)
            result = api.run_rotation(root, config, output)
            self.assertEqual(result["status"], "COMPLETE")
            self.assertEqual(len(result["cells"]), 4)
            self.assertTrue(all(cell["filled_buy_count"] == 3 for cell in result["cells"]))
            self.assertTrue((output / "RESEARCH_REPORT.md").exists())
            self.assertTrue(api.run_rotation(root, config, output)["reused"])
            write_json(output / "test" / "base" / "metrics.json", {"tampered": True})
            with self.assertRaises(ContractError):
                api.run_rotation(root, config, output)

    def test_run_rejects_changed_prepared_roster_or_policy(self):
        api = self.api()
        self.assertTrue(hasattr(api, "run_rotation"), "four-cell research replay is not implemented")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            config = api.load_rotation_config()
            self.prepared_fixture(root, config)
            changed = {**config, "rank_rotation": {**config["rank_rotation"], "horizon": 5}}
            with self.assertRaises(ContractError):
                api.run_rotation(root, changed, Path(directory) / "different-policy")
            pd.DataFrame({"code": ["688001.SH"], "name": ["STAR"]}).to_parquet(root / "roster.parquet", index=False)
            with self.assertRaises(ContractError):
                api.run_rotation(root, config, Path(directory) / "tampered-roster")

    def test_prepare_rejects_completed_policy_change_before_touching_bound_data(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "data"
            config = api.load_rotation_config()
            self.prepared_fixture(root, config)
            before = file_sha256(root / "feature_manifest.json")
            changed = {**config, "rank_rotation": {**config["rank_rotation"], "horizon": 5}}
            with self.assertRaises(ContractError):
                api.prepare_rotation(Path(directory) / "reference", root, changed)
            self.assertEqual(file_sha256(root / "feature_manifest.json"), before)

    def test_cli_help_exposes_isolated_prepare_and_run_without_starting_production(self):
        process = subprocess.run([sys.executable, "-m", "scripts.rank_rotation_backtest", "--help"],
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("--source-experiment", process.stdout)
        self.assertIn("--experiment-root", process.stdout)

    def test_parallel_feature_chunks_match_serial_native_features(self):
        from core.pipeline.prism_compare_data import _cache_features

        self.assertIn("workers", inspect.signature(_cache_features).parameters,
                      "independent feature chunk preparation is still serial only")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source.db"
            store = V2Store(source, data_mode="real")
            store.migrate()
            dates = pd.bdate_range("2024-01-02", periods=160).strftime("%Y%m%d").tolist()
            price = 10 * np.exp(np.arange(160) * .001 + np.sin(np.arange(160)) * .003)
            codes = ["600000.SH", "600001.SH"]
            for code in codes:
                raw = pd.DataFrame(dict(code=code, date=dates, open=price, high=price * 1.01,
                    low=price * .99, close=price, volume_shares=100000, amount_cny=1e7,
                    volume_raw=100000, volume_raw_unit="shares", amount_raw=1e7, amount_raw_unit="CNY",
                    completeness="COMPLETE", data_source_mode="real", source="FIXTURE",
                    source_version="v1", retrieved_at=dates[-1], pre_close=np.nan, pct_chg=np.nan))
                store.upsert_daily_raw(raw)
                store.upsert_adjustments(pd.DataFrame(dict(code=code, date=dates, adj_factor=1.,
                    source="FIXTURE", source_version="v1", retrieved_at=dates[-1])))
            roster = pd.DataFrame(dict(code=codes, list_date="19990101", delist_date=None,
                instrument_type="stock", listing_board="MAIN_SH", context_valid_from="19990101", context_valid_to=None,
                metadata_status="AVAILABLE_LIST_DATE_HISTORY_LIMITED"))
            audit = SnapshotAudit({}, dates, roster, pd.DataFrame(), pd.DataFrame())
            config = self.api().load_rotation_config()
            config["data"]["feature_chunk_stocks"] = 1
            snapshot = {"snapshot_db": str(source), "snapshot_sha256": file_sha256(source)}
            serial = _cache_features(base / "serial", snapshot, audit, config, workers=1)
            parallel = _cache_features(base / "parallel", snapshot, audit, config, workers=2)
            for left, right in zip(serial["feature_chunks"], parallel["feature_chunks"]):
                pd.testing.assert_frame_equal(pd.read_parquet(left["path"]), pd.read_parquet(right["path"]))
            for left, right in zip(serial["label_chunks"], parallel["label_chunks"]):
                pd.testing.assert_frame_equal(pd.read_parquet(left["path"]), pd.read_parquet(right["path"]))


if __name__ == "__main__":
    unittest.main()
