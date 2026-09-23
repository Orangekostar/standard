from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from core.background.snapshot_store import read_latest_manifest
from core.pipeline.technical_v2 import (
    ArtifactMismatch,
    TechnicalV2Pipeline,
    build_prediction_contract,
)
from core.technical_v2.contracts import ContractError


class PredictionContractTest(unittest.TestCase):
    def test_full_coverage_includes_failed_entity_rows(self) -> None:
        universe = pd.DataFrame(
            [
                {"code": "000001.SZ", "name": "A"},
                {"code": "600000.SH", "name": "B"},
            ]
        )
        partial = pd.DataFrame(
            [
                {
                    "entity_type": "stock",
                    "entity_id": "000001.SZ",
                    "as_of_trade_date": "20260922",
                    "horizon": 5,
                    "method": "formula",
                    "prediction_status": "OK",
                    "forecast_class": "up",
                }
            ]
        )

        rows = build_prediction_contract(
            universe,
            partial,
            as_of_trade_date="20260922",
            missing_status_by_method={"formula": "NOT_EVALUATED", "jev": "PENDING"},
        )

        self.assertEqual(len(rows), len(universe) * 2 * 3)
        missing = rows.loc[rows["entity_id"].eq("600000.SH")]
        self.assertEqual(set(missing.loc[missing["method"].eq("formula"), "prediction_status"]), {"NOT_EVALUATED"})
        self.assertEqual(set(missing.loc[missing["method"].eq("jev"), "prediction_status"]), {"PENDING"})
        provided = rows.loc[
            rows["entity_id"].eq("000001.SZ")
            & rows["method"].eq("formula")
            & rows["horizon"].eq(5)
        ].iloc[0]
        self.assertEqual(provided["prediction_status"], "OK")

    def test_empty_universe_keeps_contract_schema(self) -> None:
        rows = build_prediction_contract(
            pd.DataFrame(columns=["code", "name"]),
            pd.DataFrame(),
            as_of_trade_date="20260922",
        )

        self.assertTrue(rows.empty)
        self.assertTrue(
            {
                "entity_type",
                "entity_id",
                "as_of_trade_date",
                "horizon",
                "method",
                "prediction_status",
            }.issubset(rows.columns)
        )


class PipelinePublicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "artifacts"
        self.pipeline = TechnicalV2Pipeline(self.root)
        self.universe = pd.DataFrame([{"code": "000001.SZ", "name": "A"}])
        self.formula = pd.DataFrame(
            [
                {
                    "entity_type": "stock",
                    "entity_id": "000001.SZ",
                    "as_of_trade_date": "20260922",
                    "horizon": horizon,
                    "method": "formula",
                    "prediction_status": "OK",
                    "forecast_class": "up",
                    "formula_score": 70 + horizon,
                }
                for horizon in (1, 3, 5)
            ]
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, jev: pd.DataFrame | None = None):
        return self.pipeline.run(
            run_id="run-20260922",
            as_of_trade_date="20260922",
            information_cutoff="2026-09-22T16:10:00+08:00",
            mode="EOD_FINAL",
            data_source_mode="test",
            data_hash="data-hash-1",
            config_hash="config-hash-1",
            code_hash="code-hash-1",
            universe=self.universe,
            formula_predictions=self.formula,
            jev_predictions=jev,
        )

    def test_previous_jev_cannot_be_republished_under_new_as_of(self) -> None:
        stale = self.formula.assign(
            method="jev",
            as_of_trade_date="20260921",
            p_raw_up=0.6,
            p_raw_flat=0.3,
            p_raw_down=0.1,
        )

        with self.assertRaises(ArtifactMismatch):
            self._run(stale)

        self.assertIsNone(read_latest_manifest(self.root))

    def test_formula_can_publish_first_then_jev_appends_new_publication(self) -> None:
        first = self._run()
        first_path = self.root / "publications" / f"{first.publication_id}.json"
        first_bytes = first_path.read_bytes()
        jev = self.formula.assign(
            method="jev",
            formula_score=None,
            p_raw_up=0.6,
            p_raw_flat=0.3,
            p_raw_down=0.1,
        )

        second = self._run(jev)
        latest = read_latest_manifest(self.root)

        self.assertNotEqual(first.publication_id, second.publication_id)
        self.assertEqual(first_path.read_bytes(), first_bytes)
        self.assertEqual(latest["publication_id"], second.publication_id)
        self.assertEqual(latest["routes"]["formula"]["status"], "OK")
        self.assertEqual(latest["routes"]["jev"]["status"], "OK")
        publication_files = sorted((self.root / "publications").glob("*.json"))
        self.assertEqual(len(publication_files), 2)

        repeated_formula_only = self._run()
        latest_after_repeat = read_latest_manifest(self.root)
        self.assertEqual(repeated_formula_only.publication_id, second.publication_id)
        self.assertEqual(latest_after_repeat["publication_id"], second.publication_id)

    def test_first_recorded_prediction_is_immutable(self) -> None:
        jev = self.formula.assign(
            method="jev",
            formula_score=None,
            p_raw_up=0.6,
            p_raw_flat=0.3,
            p_raw_down=0.1,
        )
        self._run(jev)
        changed = jev.copy()
        changed.loc[changed["horizon"].eq(5), "p_raw_up"] = 0.7

        with self.assertRaises(ArtifactMismatch):
            self._run(changed)

        stored = list((self.root / "run-20260922" / "predictions" / "jev").glob("*.json"))
        self.assertEqual(len(stored), 3)
        self.assertEqual(json.loads(stored[0].read_text(encoding="utf-8"))["method"], "jev")

    def test_route_status_uses_frozen_universe_denominator(self) -> None:
        partial_formula = self.formula.loc[self.formula["horizon"].eq(5)].copy()

        result = self.pipeline.run(
            run_id="run-partial",
            as_of_trade_date="20260922",
            information_cutoff="2026-09-22T16:10:00+08:00",
            mode="EOD_FINAL",
            data_source_mode="test",
            data_hash="data-hash-partial",
            config_hash="config-hash-1",
            code_hash="code-hash-1",
            universe=self.universe,
            formula_predictions=partial_formula,
        )

        self.assertEqual(result.route_statuses["formula"], "PARTIAL")

    def test_pipeline_publishes_independent_stock_and_sector_coverage(self) -> None:
        sector_universe = pd.DataFrame(
            [{"entity_id": "SW_L1:801010.SI", "sector_id": "801010.SI"}]
        )
        sector_formula = pd.DataFrame(
            [
                {
                    "entity_type": "sector",
                    "entity_id": "SW_L1:801010.SI",
                    "as_of_trade_date": "20260922",
                    "horizon": horizon,
                    "method": "formula",
                    "prediction_status": "OK",
                    "forecast_class": "up",
                    "formula_score": 60 + horizon,
                }
                for horizon in (1, 3, 5)
            ]
        )

        result = self.pipeline.run(
            run_id="run-stock-sector",
            as_of_trade_date="20260922",
            information_cutoff="2026-09-22T16:10:00+08:00",
            mode="EOD_FINAL",
            data_source_mode="test",
            data_hash="data-hash-sector",
            config_hash="config-hash-1",
            code_hash="code-hash-1",
            universe=self.universe,
            sector_universe=sector_universe,
            formula_predictions=pd.concat([self.formula, sector_formula], ignore_index=True),
        )

        self.assertEqual(result.coverage_rows, 12)
        coverage_files = list((self.root / result.run_id / "coverage").glob("*.json"))
        rows = json.loads(coverage_files[0].read_text(encoding="utf-8"))["rows"]
        self.assertEqual({row["entity_type"] for row in rows}, {"stock", "sector"})
        self.assertEqual(
            len([row for row in rows if row["entity_type"] == "sector"]),
            6,
        )

    def test_run_id_cannot_escape_artifact_root(self) -> None:
        with self.assertRaises(ContractError):
            self.pipeline.run(
                run_id="../outside",
                as_of_trade_date="20260922",
                information_cutoff="2026-09-22T16:10:00+08:00",
                mode="EOD_FINAL",
                data_source_mode="test",
                data_hash="data-hash-1",
                config_hash="config-hash-1",
                code_hash="code-hash-1",
                universe=self.universe,
                formula_predictions=self.formula,
            )


if __name__ == "__main__":
    unittest.main()
