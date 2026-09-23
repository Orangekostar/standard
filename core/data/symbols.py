from __future__ import annotations

import re

import pandas as pd


def normalize_ts_code(value: str) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        raise ValueError("股票代码不能为空")

    raw = raw.replace("_", ".").replace("-", ".")
    raw = re.sub(r"\s+", ".", raw)
    if raw.startswith(("SH", "SZ", "BJ")) and len(raw) >= 8:
        raw = f"{raw[2:8]}.{raw[:2]}"

    if "." in raw:
        code, suffix = raw.split(".", 1)
        code = re.sub(r"\D", "", code)
        suffix = suffix[:2].upper()
        if len(code) == 6 and suffix in {"SH", "SZ", "BJ"}:
            return f"{code}.{suffix}"
        raise ValueError(f"无法识别股票代码：{value}")

    code = re.sub(r"\D", "", raw)
    if len(code) != 6:
        raise ValueError(f"无法识别股票代码：{value}")

    if code.startswith(("60", "68", "90")):
        suffix = "SH"
    elif code.startswith(("00", "20", "30")):
        suffix = "SZ"
    elif code.startswith(("43", "83", "87", "88", "92")):
        suffix = "BJ"
    else:
        suffix = "SZ"
    return f"{code}.{suffix}"


def is_chinext_ts_code(value: str) -> bool:
    try:
        code = normalize_ts_code(value).split(".", 1)[0]
    except ValueError:
        code = re.sub(r"\D", "", str(value or ""))
    return code.startswith(("300", "301"))


def is_buyable_mainboard_ts_code(value: str) -> bool:
    try:
        normalized = normalize_ts_code(value)
    except ValueError:
        return False
    code, suffix = normalized.split(".", 1)
    if suffix == "BJ":
        return False
    if code.startswith(("300", "301", "688", "689", "8", "4")):
        return False
    return suffix in {"SH", "SZ"} and code.startswith(("00", "60"))


def classify_listing_board(value: str) -> str | None:
    try:
        code, suffix = normalize_ts_code(value).split(".", 1)
    except ValueError:
        return None
    if suffix == "SH" and code.startswith(("600", "601", "603", "605")):
        return "MAIN_SH"
    if suffix == "SH" and code.startswith(("688", "689")):
        return "STAR"
    if suffix == "SZ" and code.startswith(("000", "001", "002", "003")):
        return "MAIN_SZ"
    if suffix == "SZ" and code.startswith(("300", "301")):
        return "CHINEXT"
    return None


def is_analysis_universe_ts_code(value: str) -> bool:
    return classify_listing_board(value) is not None


def is_risk_warning_name(value: str) -> bool:
    name = str(value or "").strip().upper().replace(" ", "")
    if not name:
        return False
    return name.startswith(("*ST", "ST", "S*ST", "SST")) or "退市" in name or name.endswith("退")


def filter_non_chinext(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "ts_code" not in df.columns:
        return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    mask = ~df["ts_code"].astype(str).map(is_chinext_ts_code)
    return df.loc[mask].reset_index(drop=True)


def filter_non_risk_warning(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "name" not in df.columns:
        return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    mask = ~df["name"].astype(str).map(is_risk_warning_name)
    return df.loc[mask].reset_index(drop=True)


def filter_buyable_mainboard(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "ts_code" not in df.columns:
        return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    out = df.copy()
    mask = out["ts_code"].astype(str).map(is_buyable_mainboard_ts_code)
    if "name" in out.columns:
        mask = mask & ~out["name"].astype(str).map(is_risk_warning_name)
    return out.loc[mask].reset_index(drop=True)
