from __future__ import annotations

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from core.background.snapshot_store import write_immutable_json
from core.pipeline.technical_v2 import TechnicalV2Pipeline
from ui.technical_v2 import (
    build_display_row,
    build_snapshot_export,
    format_ratio,
    load_technical_v2_snapshot,
    split_entity_views,
)


class TechnicalV2UiContractTest(unittest.TestCase):
    def test_ratio_formats_once_and_formula_score_is_not_probability(self) -> None:
        self.assertEqual(format_ratio(0.012), "1.20%")
        row = build_display_row({"formula_score": 78, "p_cal_up": 0.61})

        self.assertEqual(row["formula_score"], "78.0 分")
        self.assertEqual(row["p_cal_up"], "61.00%")

    def test_export_preserves_complete_snapshot_in_csv_and_json(self) -> None:
        rows = pd.DataFrame(
            [
                {"entity_id": "000001.SZ", "method": "formula", "horizon": 5},
                {"entity_id": "000001.SZ", "method": "jev", "horizon": 5},
            ]
        )

        exported = build_snapshot_export(rows)

        self.assertEqual(len(pd.read_csv(StringIO(exported.csv_text))), 2)
        self.assertEqual(len(json.loads(exported.json_text)), 2)

    def test_snapshot_loader_is_read_only_and_never_executes_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pipeline = TechnicalV2Pipeline(root)
            universe = pd.DataFrame([{"code": "600000.SH", "name": "A"}])
            formula = pd.DataFrame(
                [
                    {
                        "entity_type": "stock",
                        "entity_id": "600000.SH",
                        "as_of_trade_date": "20260922",
                        "horizon": horizon,
                        "method": "formula",
                        "prediction_status": "OK",
                        "formula_score": 70.0,
                    }
                    for horizon in (1, 3, 5)
                ]
            )
            pipeline.run(
                run_id="ui-fixture",
                as_of_trade_date="20260922",
                information_cutoff="2026-09-22T20:10:00+08:00",
                mode="EOD_FINAL",
                data_source_mode="test",
                data_hash="data",
                config_hash="config",
                code_hash="code",
                universe=universe,
                formula_predictions=formula,
            )

            with patch.object(TechnicalV2Pipeline, "run", side_effect=AssertionError("must not run")):
                snapshot = load_technical_v2_snapshot(root)

        self.assertEqual(snapshot.manifest["run_id"], "ui-fixture")
        self.assertEqual(len(snapshot.rows), 6)

    def test_missing_publication_returns_explicit_empty_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot = load_technical_v2_snapshot(Path(tmpdir))

        self.assertEqual(snapshot.status, "NO_PUBLICATION")
        self.assertTrue(snapshot.rows.empty)

    def test_loader_and_views_keep_sector_predictions_as_real_entities(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pipeline = TechnicalV2Pipeline(root)
            universe = pd.DataFrame([{"code": "600000.SH", "name": "A"}])
            sectors = pd.DataFrame(
                [
                    {
                        "entity_id": "SW2021:银行",
                        "namespace": "SW2021",
                        "sector_id": "银行",
                        "sector_name": "银行",
                    }
                ]
            )
            rows = []
            for entity_type, entity_id in (("stock", "600000.SH"), ("sector", "SW2021:银行")):
                for horizon in (1, 3, 5):
                    rows.append(
                        {
                            "entity_type": entity_type,
                            "entity_id": entity_id,
                            "as_of_trade_date": "20260922",
                            "horizon": horizon,
                            "method": "formula",
                            "prediction_status": "OK",
                            "formula_score": 70.0,
                            "sector_namespace": "SW2021",
                            "sector_id": "银行",
                        }
                    )
            predictions = pd.DataFrame(rows)
            pipeline.run(
                run_id="ui-sector-fixture",
                as_of_trade_date="20260922",
                information_cutoff="2026-09-22T20:10:00+08:00",
                mode="EOD_FINAL",
                data_source_mode="test",
                data_hash="data",
                config_hash="config",
                code_hash="code",
                universe=universe,
                sector_universe=sectors,
                formula_predictions=predictions,
            )
            write_immutable_json(
                root / "ui-sector-fixture" / "features" / "latest.json",
                {
                    "schema_version": "technical-v2-features.v1",
                    "run_id": "ui-sector-fixture",
                    "as_of_trade_date": "20260922",
                    "stock_rows": [{"code": "600000.SH", "name": "A"}],
                    "sector_rows": sectors.to_dict(orient="records"),
                },
            )

            snapshot = load_technical_v2_snapshot(root)
            stocks, sector_predictions = split_entity_views(snapshot.rows)

        self.assertEqual(set(stocks["entity_id"]), {"600000.SH"})
        self.assertEqual(set(sector_predictions["entity_id"]), {"SW2021:银行"})
        self.assertEqual(set(sector_predictions["method"]), {"formula", "jev"})
        self.assertEqual(
            set(sector_predictions.loc[sector_predictions["method"].eq("jev"), "prediction_status"]),
            {"PENDING"},
        )
        self.assertIn("sector_name", snapshot.features.columns)


if __name__ == "__main__":
    unittest.main()
