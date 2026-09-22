from __future__ import annotations

import unittest

import pandas as pd

from core.data.symbols import (
    filter_non_chinext,
    filter_non_risk_warning,
    is_chinext_ts_code,
    is_risk_warning_name,
    normalize_ts_code,
)


class SymbolFilterTest(unittest.TestCase):
    def test_identifies_chinext_codes(self) -> None:
        self.assertTrue(is_chinext_ts_code("300750.SZ"))
        self.assertTrue(is_chinext_ts_code("301045.SZ"))
        self.assertTrue(is_chinext_ts_code("sz300308"))
        self.assertFalse(is_chinext_ts_code("002821.SZ"))
        self.assertFalse(is_chinext_ts_code("688012.SH"))
        self.assertFalse(is_chinext_ts_code("920047.BJ"))

    def test_filters_chinext_rows(self) -> None:
        df = pd.DataFrame(
            {
                "ts_code": ["300750.SZ", "301045.SZ", "002821.SZ", "688012.SH", "920047.BJ"],
                "name": ["创业板A", "创业板B", "主板", "科创", "北交"],
            }
        )
        out = filter_non_chinext(df)
        self.assertEqual(out["ts_code"].tolist(), ["002821.SZ", "688012.SH", "920047.BJ"])

    def test_normalize_keeps_existing_code_behavior(self) -> None:
        self.assertEqual(normalize_ts_code("300750"), "300750.SZ")

    def test_filters_risk_warning_names(self) -> None:
        self.assertTrue(is_risk_warning_name("*ST样本"))
        self.assertTrue(is_risk_warning_name("ST样本"))
        self.assertTrue(is_risk_warning_name("S*ST样本"))
        self.assertTrue(is_risk_warning_name("退市样本"))
        self.assertTrue(is_risk_warning_name("样本退"))
        self.assertFalse(is_risk_warning_name("正常样本"))

        df = pd.DataFrame(
            {
                "ts_code": ["000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ"],
                "name": ["正常样本", "*ST样本", "退市样本", "样本退"],
            }
        )
        out = filter_non_risk_warning(df)
        self.assertEqual(out["ts_code"].tolist(), ["000001.SZ"])


if __name__ == "__main__":
    unittest.main()
