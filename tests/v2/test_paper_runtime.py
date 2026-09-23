from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from core.backtest.execution_v2 import (
    FeeSchedule,
    MarketOpen,
    PaperOrder,
    SecurityRule,
    execute_open_orders,
)
from core.backtest.portfolio_v2 import PaperPortfolio
from core.data.v2_store import V2Store
from core.pipeline.paper_runtime_v2 import evaluate_matured_predictions, run_paper_cycle
from scripts.v2 import build_parser, dispatch


class PaperRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "demo.db"
        self.artifact_root = self.root / "artifacts"
        parser = build_parser()
        sync = parser.parse_args(
            [
                "sync",
                "--mode",
                "demo",
                "--db-path",
                str(self.db_path),
                "--artifact-root",
                str(self.artifact_root),
                "--history-sessions",
                "90",
                "--as-of",
                "20260922",
                "--seed",
                "20260923",
            ]
        )
        analyze = parser.parse_args(
            [
                "analyze",
                "--mode",
                "demo",
                "--db-path",
                str(self.db_path),
                "--artifact-root",
                str(self.artifact_root),
                "--methods",
                "formula,jev",
            ]
        )
        self.assertEqual(dispatch(sync).exit_code, 0)
        self.analysis = dispatch(analyze).payload
        self.store = V2Store(self.db_path, data_mode="demo")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_cycle_creates_isolated_accounts_valuations_and_auditable_decisions(self) -> None:
        first = run_paper_cycle(
            self.store,
            run_id=self.analysis["run_id"],
            as_of_trade_date="20260922",
            methods=("formula", "jev"),
        )
        second = run_paper_cycle(
            self.store,
            run_id=self.analysis["run_id"],
            as_of_trade_date="20260922",
            methods=("formula", "jev"),
        )

        self.assertEqual(first.next_trade_date, "20260923")
        self.assertEqual(first.decision_rows, 12)
        self.assertEqual(second.decision_rows, 0)
        decisions = self.store.read_paper_decisions()
        self.assertEqual(set(decisions["account_id"]), {"formula-paper", "jev-shadow-paper"})
        self.assertTrue(decisions["reason_codes_json"].str.contains("NET_EDGE_BELOW_MINIMUM|NOT_TRADE_ELIGIBLE").any())
        valuations = self.store.read_paper_valuations()
        self.assertEqual(set(valuations["account_id"]), {"formula-paper", "jev-shadow-paper"})
        self.assertTrue((valuations["nav_cents"] == 100_000_000).all())
        self.assertEqual(len(self.store.read_paper_orders()), first.orders_created)

    def test_cli_paper_and_matured_commands_use_runtime_services(self) -> None:
        parser = build_parser()
        common = [
            "--mode",
            "demo",
            "--db-path",
            str(self.db_path),
            "--artifact-root",
            str(self.artifact_root),
        ]

        paper = dispatch(parser.parse_args(["paper", *common, "--methods", "formula,jev"]))
        matured = dispatch(parser.parse_args(["evaluate-matured", *common]))

        self.assertEqual(paper.exit_code, 0)
        self.assertEqual(paper.payload["decision_rows"], 12)
        self.assertEqual(paper.payload["next_trade_date"], "20260923")
        self.assertEqual(matured.exit_code, 0)
        self.assertEqual(matured.payload["status"], "NO_NEW_MATURE_LABELS")
        self.assertGreater(matured.payload["label_rows"], 0)
        self.assertEqual(matured.payload["total_evaluated_rows"], 0)

    def test_matured_predictions_are_joined_to_labels_once(self) -> None:
        predictions = self.store.read_prediction_rows(self.analysis["run_id"])
        formula = predictions.loc[
            predictions["method"].eq("formula")
            & predictions["entity_type"].eq("stock")
            & predictions["horizon"].eq(1)
        ].iloc[0]
        payload = json.loads(formula["payload_json"])
        target = str(payload["forecast_class"])
        self.store.upsert_label_rows(
            [
                {
                    "entity_type": "stock",
                    "entity_id": formula["entity_id"],
                    "as_of_trade_date": payload["as_of_trade_date"],
                    "horizon": 1,
                    "label_end_date": "20260923",
                    "label_status": "OK",
                    "target_class": target,
                    "realized_return": 0.01,
                    "payload": {"source": "fixture"},
                }
            ]
        )

        first = evaluate_matured_predictions(self.store)
        second = evaluate_matured_predictions(self.store)

        self.assertEqual(first.evaluated_rows, 1)
        self.assertEqual(second.evaluated_rows, 0)
        evaluated = self.store.read_evaluation_rows(self.analysis["run_id"])
        self.assertEqual(len(evaluated), 1)
        result = json.loads(evaluated.loc[0, "payload_json"])
        self.assertTrue(result["direction_correct"])
        self.assertEqual(result["realized_return"], 0.01)

    def test_cycle_creates_next_open_exit_for_lot_reaching_planned_date(self) -> None:
        account = PaperPortfolio(self.store, "formula-paper")
        account.open_account(method="formula", initial_cash_cents=100_000_000)
        raw = self.store.read_daily_raw("20260921", "20260921", ["600000.SH"]).iloc[0]
        pre_close = Decimal(str(raw["pre_close"]))
        rule = SecurityRule(100, Decimal("0.01"), "19900101", None, "fixture-rule")
        buy = PaperOrder(
            order_id="paper-runtime-entry",
            run_id="entry-run",
            account_id="formula-paper",
            code="600000.SH",
            side="BUY",
            quantity=100,
            earliest_trade_date="20260921",
            reference_price=Decimal(str(raw["close"])),
            price_ceiling_floor=Decimal(str(raw["open"])) * Decimal("1.02"),
            planned_exit_date="20260923",
            sector_id="DEMO_TECHNICAL",
        )
        execute_open_orders(
            self.store,
            [buy],
            {
                "600000.SH": MarketOpen(
                    "600000.SH",
                    "20260921",
                    Decimal(str(raw["open"])),
                    pre_close * Decimal("1.10"),
                    pre_close * Decimal("0.90"),
                    False,
                    False,
                )
            },
            fee_schedule=FeeSchedule.research_defaults("19900101", source="fixture-fees"),
            security_rules={"600000.SH": rule},
        )

        cycle = run_paper_cycle(
            self.store,
            run_id=self.analysis["run_id"],
            as_of_trade_date="20260922",
            methods=("formula",),
        )
        orders = self.store.read_paper_orders("formula-paper")
        exits = orders.loc[orders["side"].eq("SELL")]

        self.assertEqual(cycle.orders_created, 1)
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits.iloc[0]["earliest_trade_date"], "20260923")
        self.assertEqual(exits.iloc[0]["quantity"], 100)
        self.assertIn("PLANNED_EXIT_DUE", json.loads(exits.iloc[0]["reason_codes_json"]))


if __name__ == "__main__":
    unittest.main()
