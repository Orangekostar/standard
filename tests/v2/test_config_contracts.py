from __future__ import annotations

import json
import math
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from core.technical_v2.config import TechnicalV2Config
from core.technical_v2.contracts import ContractError, RunStatus, canonical_json, json_safe, sha256_json


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class TechnicalV2ConfigTest(unittest.TestCase):
    def test_checked_in_config_preserves_frozen_values(self) -> None:
        cfg = TechnicalV2Config.load(PROJECT_ROOT / "configs" / "technical_v2.json")

        self.assertEqual(cfg.schema_version, "standard.technical-v2.plan.v1")
        self.assertEqual(cfg.data.default_mode, "real")
        self.assertEqual(cfg.data.factor_count, 15)
        self.assertEqual(cfg.data.risk_count, 6)
        self.assertEqual(cfg.targets.horizons, (1, 3, 5))
        self.assertEqual(cfg.jev.model, "jev-1.13.0")
        self.assertEqual(cfg.jev.development_budget_usd, 15)
        self.assertEqual(cfg.jev.daily_budget_usd, 5)
        self.assertFalse(cfg.portfolio.connect_real_broker)

    def test_config_rejects_non_normalized_formula_weights(self) -> None:
        source = json.loads((PROJECT_ROOT / "configs" / "technical_v2.json").read_text(encoding="utf-8"))
        source["factors"]["base_weights"]["5"]["T"] = 0.99

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "bad.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "base_weights.*horizon 5"):
                TechnicalV2Config.load(path)

    def test_config_rejects_live_broker_connection(self) -> None:
        source = json.loads((PROJECT_ROOT / "configs" / "technical_v2.json").read_text(encoding="utf-8"))
        source["portfolio"]["connect_real_broker"] = True

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "bad.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "connect_real_broker"):
                TechnicalV2Config.load(path)


class JsonContractTest(unittest.TestCase):
    def test_canonical_json_rejects_non_finite_numbers(self) -> None:
        for value in (math.nan, math.inf, -math.inf, np.float64(np.nan)):
            with self.subTest(value=value):
                with self.assertRaises(ContractError):
                    canonical_json({"probability": value})

    def test_json_safe_normalizes_supported_scalar_types(self) -> None:
        converted = json_safe(
            {
                "amount": Decimal("12.30"),
                "count": np.int64(3),
                "ratio": np.float64(0.25),
                "as_of": pd.Timestamp("2026-09-22 16:10:00", tz="Asia/Shanghai"),
                "missing": pd.NA,
            }
        )

        self.assertEqual(converted["amount"], "12.30")
        self.assertEqual(converted["count"], 3)
        self.assertEqual(converted["ratio"], 0.25)
        self.assertEqual(converted["as_of"], "2026-09-22T16:10:00+08:00")
        self.assertIsNone(converted["missing"])

    def test_hash_is_independent_of_mapping_order(self) -> None:
        self.assertEqual(sha256_json({"b": 2, "a": 1}), sha256_json({"a": 1, "b": 2}))

    def test_run_status_serializes_without_optional_details(self) -> None:
        status = RunStatus(status="PARTIAL", code="DATA_INSUFFICIENT", message="history too short")

        self.assertEqual(
            status.to_dict(),
            {
                "status": "PARTIAL",
                "code": "DATA_INSUFFICIENT",
                "message": "history too short",
                "details": {},
            },
        )


if __name__ == "__main__":
    unittest.main()
