from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from core.pipeline.prism_compare_config import STRATEGIES, load_config
from core.technical_v2.contracts import ContractError
from core.technical_v2.contracts import sha256_json


class PrismComparisonMetricsTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def nav(self, count=12, growth=1.01):
        return pd.DataFrame({
            "date": pd.bdate_range("2024-01-02", periods=count).strftime("%Y%m%d"),
            "nav_cents": 100000000 * growth ** np.arange(count),
            "status": "OK", "gross_exposure": .25, "cash_ratio": .75,
        })

    def summary(self, a=.05, b=.10, c=.04, b_drawdown=.12):
        rows = []
        for cost in ("base", "stress"):
            for strategy, result in zip(STRATEGIES, (a, b, c)):
                rows.append(dict(strategy_id=strategy, split="test", cost_scenario=cost,
                    net_return=result, max_drawdown=b_drawdown if strategy == STRATEGIES[1] else .10,
                    closed_trade_count=30, traded_dates=20, accounting_status="OK",
                    unresolved_asset_count=0, unresolved_nav_days=0))
        return pd.DataFrame(rows)

    def halves(self):
        return pd.DataFrame([dict(strategy_id=strategy, cost_scenario="base", block=block,
            net_return=.01 if strategy == STRATEGIES[0] else .02)
            for strategy in STRATEGIES[:2] for block in (1, 2)])

    def test_exposure_control_uses_validation_exposure_and_never_increases_limits(self):
        from core.backtest.prism_compare_metrics import exposure_control
        self.assertEqual(exposure_control(.4, .1, self.config)["lambda_c"], .25)
        zero = exposure_control(0., .2, self.config)
        self.assertEqual(zero["lambda_c"], 1.)
        self.assertEqual(zero["status"], "UNIDENTIFIABLE_ZERO_BASE_EXPOSURE")
        larger = exposure_control(.1, .4, self.config)
        self.assertEqual(larger["lambda_c"], 1.)
        self.assertEqual(larger["status"], "UNMATCHABLE_HIGHER_B_EXPOSURE")
        with self.assertRaises(ContractError):
            exposure_control(None, .1, self.config)

    def test_bootstrap_pairs_identical_variable_paths_instead_of_independent_dates(self):
        from core.backtest.prism_compare_metrics import paired_bootstrap
        nav = self.nav(32)
        nav["nav_cents"] = [100000000 * (1 + .001 * (index % 7)) for index in range(32)]
        result = paired_bootstrap(nav, nav.copy(), self.config)
        self.assertEqual(result["ci95_b_minus_a"], [0., 0.])
        self.assertEqual(result["point_b_minus_a"], 0.)
        self.assertEqual(result["common_sessions"], 32)
        self.assertEqual(result["repetitions"], 1000)
        self.assertEqual(result, paired_bootstrap(nav, nav.copy(), self.config))

    def test_full_length_block_preserves_hand_checked_compounded_difference(self):
        from core.backtest.prism_compare_metrics import paired_bootstrap
        config = copy.deepcopy(self.config)
        config["evaluation"].update(bootstrap_block_sessions=12, bootstrap_repetitions=37)
        result = paired_bootstrap(self.nav(growth=1.01), self.nav(growth=1.02), config)
        self.assertAlmostEqual(result["point_b_minus_a"], .1277059617293358, places=12)
        for bound in result["ci95_b_minus_a"]:
            self.assertAlmostEqual(bound, .1277059617293358, places=12)

    def test_bootstrap_rejects_different_dates_and_does_not_fill_unresolved_nav(self):
        from core.backtest.prism_compare_metrics import paired_bootstrap
        a, b = self.nav(), self.nav()
        b.loc[1, "date"] = "19990101"
        with self.assertRaises(ContractError):
            paired_bootstrap(a, b, self.config)
        b = self.nav()
        b.loc[1, "nav_cents"] = np.nan
        b.loc[1, "status"] = "NAV_UNRESOLVED_VALUATION"
        result = paired_bootstrap(a, b, self.config)
        self.assertEqual(result["status"], "ACCOUNTING_OR_DATA_INCONCLUSIVE")
        self.assertIsNone(result["ci95_b_minus_a"])

    def test_verdict_checks_validity_and_evidence_before_the_numerical_leader(self):
        from core.backtest.prism_compare_metrics import comparison_verdict
        paired = {"status": "OK", "ci95_b_minus_a": [.01, .08]}
        result = comparison_verdict(self.summary(), paired, self.halves(), self.config, [])
        self.assertEqual(result["verdict"], "B_RETURN_LEADER_WITHIN_RISK_TOLERANCE")
        self.assertTrue(result["evidence_supports_historical_leader"])
        limited = comparison_verdict(self.summary(), paired, self.halves(), self.config,
                                     ["RAW_PRICE_LEDGER_CORPORATE_ACTIONS_INCOMPLETE"])
        self.assertTrue(limited["scope_limited"])
        self.assertFalse(limited["evidence_supports_historical_leader"])
        few = self.summary()
        few.loc[few.strategy_id.eq(STRATEGIES[0]), "closed_trade_count"] = 29
        self.assertEqual(comparison_verdict(few, paired, self.halves(), self.config, [])["verdict"],
                         "INSUFFICIENT_TRADING_EVIDENCE")
        few.loc[few.strategy_id.eq(STRATEGIES[0]), "accounting_status"] = "NAV_INCOMPLETE"
        self.assertEqual(comparison_verdict(few, paired, self.halves(), self.config, [])["verdict"],
                         "ACCOUNTING_OR_DATA_INCONCLUSIVE")

    def test_extra_drawdown_and_ties_are_not_promoted_by_a_positive_ci(self):
        from core.backtest.prism_compare_metrics import comparison_verdict
        paired = {"status": "OK", "ci95_b_minus_a": [.01, .08]}
        self.assertEqual(comparison_verdict(self.summary(b_drawdown=.121), paired, self.halves(),
            self.config, [])["verdict"], "RETURN_RISK_TRADEOFF")
        self.assertEqual(comparison_verdict(self.summary(a=.1, b=.1000005), paired, self.halves(),
            self.config, [])["verdict"], "TIE")

    def test_second_test_block_uses_preceding_nav_without_resetting_the_account(self):
        from core.backtest.prism_compare_metrics import test_subperiods
        nav = self.nav(132)
        rows = test_subperiods(nav, nav.date.iloc[:126].tolist(), self.config,
                              strategy_id=STRATEGIES[0], cost_scenario="base")
        self.assertEqual(rows[0]["session_count"], 63)
        self.assertEqual(rows[1]["session_count"], 69)
        self.assertEqual(rows[1]["starting_nav_cents"], nav.nav_cents.iloc[62])
        self.assertAlmostEqual(rows[0]["net_return"], 1.01 ** 62 - 1., places=12)
        self.assertAlmostEqual(rows[1]["net_return"], 1.01 ** 69 - 1., places=12)
        self.assertTrue(rows[1]["account_continuous"])

    def test_gate_summary_uses_signal_dates_without_losing_tail_account_events(self):
        from core.pipeline.prism_compare_report import _gate_summary
        common = dict(strategy_id="A0_V2_F0", split="test", cost_scenario="base")
        funnel = pd.DataFrame([{**common, "date": "20240102", "stage": "roster", "count": 2, "reason_scope": "cumulative_gate"},
            {**common, "date": "20240103", "stage": "roster", "count": 3, "reason_scope": "cumulative_gate"},
            {**common, "date": "ALL", "stage": "fill", "count": 1, "reason_scope": "actual_events"}])
        frozen = {"split_plan": {"replay_windows": {"test": {"signal_dates": ["20240102"]}}}}
        result = _gate_summary(funnel, frozen).set_index("stage")
        self.assertEqual(result.loc["roster", "count"], 2)
        self.assertEqual(result.loc["fill", "count"], 1)
        self.assertEqual(result.loc["roster", "allowed_signal_dates"], 1)


class PrismComparisonStageTest(unittest.TestCase):
    def setUp(self):
        from core.pipeline.prism_compare_config import file_sha256, implementation_identity, write_json
        from core.pipeline.prism_compare_data import fixed_split_manifest, write_replay_chunk
        from tests.v2.test_prism_replay import PrismReplayTest
        fixture = PrismReplayTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.root / "experiment"
        self.root.mkdir()
        self.config = fixture.config
        self.config["delivery"]["artifact_root"] = str(fixture.root / "artifacts" / "prism_v2_compare")
        self.config["resources"]["parallel_replays"] = 1
        self.config_path = fixture.root / "configuration.json"
        write_json(self.config_path, self.config)
        fixture.sessions = pd.bdate_range("2023-01-02", periods=670).strftime("%Y%m%d").tolist()
        frames = pd.concat([frame for _, frame in fixture.frames(signals=664)], ignore_index=True)
        frames["score5"] = 50.
        frames.loc[frames.date.isin([fixture.sessions[475], fixture.sessions[538]]), "score5"] = 70.
        record = write_replay_chunk(self.root / "replay.parquet", frames, fixture.sessions)
        feature_sha = write_json(self.root / "feature_manifest.json", {
            "sessions": fixture.sessions, "replay_chunks": [record], "feature_identity": "UNIT_FIXTURE_ONLY"})
        model_sha = write_json(self.root / "common_return_bins.json", asdict(fixture.model()))
        pd.DataFrame(columns=["event_id", "code"]).to_parquet(self.root / "corporate_actions.parquet", index=False)
        maturity = pd.DataFrame({"as_of_trade_date": fixture.sessions[:664], "label_status": "OK"})
        split_plan, assignments = fixed_split_manifest(maturity, fixture.sessions, self.config)
        assignments.to_csv(self.root / "split.csv", index=False)
        write_json(self.root / "parameters.json", self.config)
        write_json(self.root / "data_audit.json", {"scope_flags": [], "as_of": fixture.sessions[-1],
            "old_portfolio_test_executed": "FIXTURE_ONLY", "old_test_summary_exposure": "FIXTURE_ONLY"})
        write_json(self.root / "dataset_manifest.json", {"status": "OK", "as_of": fixture.sessions[-1],
            "configuration_sha256": sha256_json(self.config), "feature_identity": "UNIT_FIXTURE_ONLY",
            "feature_manifest_sha256": feature_sha, "split_sha256": file_sha256(self.root / "split.csv"),
            "scope_flags": [], "snapshot": {"snapshot_sha256": "FIXTURE_NOT_VENDOR_DATA"},
            "common_return_bins": {"path": str(self.root / "common_return_bins.json"), "sha256": model_sha},
            "split_plan": split_plan})
        identity = implementation_identity()
        identity["source_tree_clean"] = True
        self.identity = identity
        self.identity_patch = patch("core.pipeline.prism_comparison.implementation_identity", return_value=identity)
        self.identity_patch.start()
        self.addCleanup(self.identity_patch.stop)

    def test_test_requires_a_frozen_manifest(self):
        from core.pipeline.prism_comparison import test as run_test
        with self.assertRaisesRegex(ContractError, "frozen|freeze"):
            run_test(self.root, self.root / "missing-frozen.json")
        self.assertFalse((self.root / "test_complete.json").exists())

    def test_validation_cells_are_reused_without_network_access(self):
        from core.pipeline.prism_comparison import validate
        with patch("requests.sessions.Session.request", side_effect=AssertionError("NO_NETWORK_ALLOWED")):
            first = validate(self.root, self.config_path)
            a = Path(first["artifact_dir"]) / "A0_V2_F0" / "validation" / "base" / "metrics.json"
            before = a.stat().st_mtime_ns
            second = validate(self.root, self.config_path)
        self.assertEqual(len(first["cells"]), 2)
        self.assertEqual(a.stat().st_mtime_ns, before)
        self.assertTrue(all(cell["reused"] for cell in second["cells"]))

    def test_freeze_refuses_missing_validation_and_tampered_success_files(self):
        from core.pipeline.prism_comparison import freeze, validate
        with self.assertRaisesRegex(ContractError, "validation|validate"):
            freeze(self.root, self.config_path)
        validation = validate(self.root, self.config_path)
        path = Path(validation["artifact_dir"]) / "A0_V2_F0" / "validation" / "base" / "metrics.json"
        path.write_text('{"not":"validated results"}\n')
        with self.assertRaisesRegex(ContractError, "hash|SHA256"):
            freeze(self.root, self.config_path)

    def test_full_matrix_reuses_validation_and_keeps_frozen_c_parameter(self):
        from core.pipeline.prism_compare_config import file_sha256
        from core.pipeline.prism_comparison import freeze, test as run_test, validate
        validation = validate(self.root, self.config_path)
        directory = Path(validation["artifact_dir"])
        a = directory / "A0_V2_F0" / "validation" / "base" / "metrics.json"
        before = a.stat().st_mtime_ns
        frozen_path = freeze(self.root, self.config_path)
        frozen = json.loads(Path(frozen_path).read_text())
        frozen_sha = file_sha256(frozen_path)
        self.assertTrue(frozen["source_identity"]["source_tree_clean"])
        self.assertFalse(frozen["exposure_control"]["test_returns_used"])
        result = run_test(self.root, frozen_path)
        self.assertEqual(len(result["cells"]), 12)
        self.assertEqual(sum(cell["reused"] for cell in result["cells"]), 2)
        self.assertEqual(a.stat().st_mtime_ns, before)
        self.assertEqual(file_sha256(frozen_path), frozen_sha)
        repeated = run_test(self.root, frozen_path)
        self.assertTrue(all(cell["reused"] for cell in repeated["cells"]))
        self.assertEqual(json.loads(Path(frozen_path).read_text())["exposure_control"], frozen["exposure_control"])

    def test_changed_frozen_content_or_shared_implementation_refuses_test(self):
        from core.pipeline.prism_compare_config import write_json
        from core.pipeline.prism_comparison import freeze, test as run_test, validate
        validate(self.root, self.config_path)
        path = freeze(self.root, self.config_path)
        altered = {**self.identity, "implementation_hash": "DIFFERENT_SHARED_CODE"}
        with patch("core.pipeline.prism_comparison.implementation_identity", return_value=altered):
            with self.assertRaisesRegex(ContractError, "implementation|binding"):
                run_test(self.root, path)
        data = json.loads(Path(path).read_text())
        data["exposure_control"]["lambda_c"] = .123456
        write_json(path, data)
        with self.assertRaisesRegex(ContractError, "hash|SHA256"):
            run_test(self.root, path)

    def test_insufficient_history_delivers_explicit_not_run_outputs_without_shortening(self):
        from core.pipeline.prism_compare_config import file_sha256, write_json
        from core.pipeline.prism_comparison import freeze, report, test as run_test, validate
        path = self.root / "dataset_manifest.json"
        dataset = json.loads(path.read_text())
        dataset["status"] = "INSUFFICIENT_HISTORY"
        dataset["split_plan"] = {"status": "INSUFFICIENT_HISTORY", "reason_codes": ["INSUFFICIENT_MATURE_DATES"],
            "mature_signal_date_count": 134, "required_mature_dates": 504, "missing_mature_dates": 370,
            "boundaries": {}, "warmup_sessions": {}, "replay_windows": {}}
        pd.DataFrame(columns=["signal_date", "split"]).to_csv(self.root / "split.csv", index=False)
        dataset["split_sha256"] = file_sha256(self.root / "split.csv")
        write_json(path, dataset)
        validation = validate(self.root, self.config_path)
        self.assertEqual(validation["status"], "INSUFFICIENT_HISTORY")
        self.assertEqual(validation["cells"], [])
        self.assertTrue((Path(validation["artifact_dir"]) / "validation_complete.json").exists())
        frozen_path = freeze(self.root, self.config_path)
        frozen = json.loads(Path(frozen_path).read_text())
        self.assertEqual(frozen["status"], "FROZEN_INELIGIBLE_HISTORY")
        self.assertIsNone(frozen["exposure_control"]["lambda_c"])
        result = run_test(self.root, frozen_path)
        self.assertEqual(result["status"], "NOT_RUN_INSUFFICIENT_HISTORY")
        self.assertEqual(result["cells"], [])
        self.assertFalse((Path(result["artifact_dir"]) / "test_started.json").exists())
        output = report(self.root)
        self.assertEqual(output["status"], "INSUFFICIENT_HISTORY")
        directory = Path(output["artifact_dir"])
        summary = pd.read_csv(directory / "comparison_summary.csv")
        self.assertEqual(len(summary), 12)
        self.assertTrue(summary.net_return.isna().all())
        self.assertTrue(summary.closed_trade_count.isna().all())
        self.assertTrue(summary.comparison_status.eq("INSUFFICIENT_HISTORY").all())
        verdict = json.loads((directory / "verdict.json").read_text())
        self.assertEqual(verdict["missing_mature_dates"], 370)
        self.assertFalse(verdict["test_opened"])
        manifest = json.loads((directory / "artifact_manifest.json").read_text())
        for name, record in manifest["files"].items():
            self.assertEqual(hashlib.sha256((directory / name).read_bytes()).hexdigest(), record["sha256"])
        metrics = json.loads((directory / "A0_V2_F0/test/base/metrics.json").read_text())
        self.assertEqual(metrics["status"], "NOT_RUN_INSUFFICIENT_HISTORY")
        self.assertIsNone(metrics["net_return"])

    def test_report_exports_exact_summary_fields_real_hashes_and_three_nonblank_figures(self):
        from PIL import Image
        from core.pipeline.prism_comparison import freeze, report, test as run_test, validate
        validation = validate(self.root, self.config_path)
        directory = Path(validation["artifact_dir"])
        with self.assertRaisesRegex(ContractError, "test|complete|freeze"):
            report(self.root)
        run_test(self.root, freeze(self.root, self.config_path))
        result = report(self.root)
        self.assertEqual(result["status"], "COMPLETE")
        summary = pd.read_csv(directory / "comparison_summary.csv")
        self.assertEqual(len(summary), 12)
        self.assertEqual(summary.columns.tolist(), ["strategy_id", "split", "cost_scenario", "start_date", "end_date",
            "initial_nav", "final_nav", "net_return", "annualized_return", "max_drawdown", "sharpe", "daily_turnover",
            "total_turnover", "average_gross_exposure", "average_cash_ratio", "filled_buy_count", "closed_trade_count",
            "traded_dates", "closed_trade_win_rate", "profit_factor", "fees_total", "modeled_slippage_total",
            "rejected_candidate_count", "unfilled_order_ratio", "open_position_count", "unresolved_asset_count",
            "data_scope_status", "comparison_status"])
        required = ["protocol_frozen.json", "dataset_manifest.json", "data_audit.json", "split.csv", "parameters.json",
            "shared_fixes.md", "baseline_identity.json", "comparison_summary.csv", "paired_bootstrap.json", "verdict.json",
            "regime_summary.csv", "sector_summary.csv", "gate_funnel.csv", "test_subperiods.csv", "RESEARCH_REPORT.md",
            "TEST_REPORT.md", "figures/equity.png", "figures/drawdown.png", "figures/exposure.png"]
        manifest = json.loads((directory / "artifact_manifest.json").read_text())
        for name in required:
            path = directory / name
            self.assertEqual(manifest["files"][name]["bytes"], path.stat().st_size)
            self.assertEqual(manifest["files"][name]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        for name in ("equity", "drawdown", "exposure"):
            with Image.open(directory / "figures" / f"{name}.png") as picture:
                self.assertGreaterEqual(picture.width, 1000)
                self.assertGreater(np.asarray(picture.convert("RGB")).std(), 3.)
        self.assertEqual(len(pd.read_csv(directory / "test_subperiods.csv")), 12)
        self.assertTrue(json.loads((directory / "verdict.json").read_text())["prediction_claim"].startswith("IDENTICAL_RAW_F0"))
        audit_path = self.root / "data_audit.json"
        audit = json.loads(audit_path.read_text())
        audit["as_of"] = "20990101"
        audit_path.write_text(json.dumps(audit))
        with self.assertRaisesRegex(ContractError, "binding|audit|SHA256"):
            report(self.root)


class PrismComparisonCliTest(unittest.TestCase):
    def test_required_commands_are_executable_and_test_without_freeze_fails(self):
        help_result = subprocess.run([sys.executable, "-m", "scripts.compare_prism_v2", "--help"],
                                     capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        for command in ("prepare", "validate", "freeze", "test", "report"):
            self.assertIn(command, help_result.stdout)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-m", "scripts.compare_prism_v2", "test",
                "--experiment-root", directory, "--frozen-manifest", str(Path(directory) / "missing.json")],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "FAILED")
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
