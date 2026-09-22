from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from core.data.data_manager import DataManager


class DataManagerRuntimeTest(unittest.TestCase):
    def test_initializes_and_reads_cached_daily_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir)
            cache_file = cache_dir / "000001_SZ_20260101_20260131_csv"
            cache_file.write_text(
                "trade_date,open,high,low,close,vol,amount\n"
                "20260102,10,11,9,10.5,1000,10500\n",
                encoding="utf-8",
            )

            dm = DataManager(cache_dir=cache_dir)
            out = dm.get_daily_data("000001.SZ", "2026-01-01", "2026-01-31", use_cache=True)

        self.assertEqual(out.iloc[0]["ts_code"], "000001.SZ")
        self.assertEqual(float(out.iloc[0]["close"]), 10.5)

    def test_empty_recommendation_methods_return_dataframes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dm = DataManager(cache_dir=Path(tmpdir))
            self.assertIsInstance(dm.recommend_scientific_candidates(), pd.DataFrame)
            self.assertIsInstance(dm.recommend_dragon_candidates(), pd.DataFrame)
            self.assertIsInstance(dm.recommend_ma5_pullback_candidates(), pd.DataFrame)


if __name__ == "__main__":
    unittest.main()
