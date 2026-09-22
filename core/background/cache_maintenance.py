from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

from config import settings


_DAILY_RE = re.compile(r"^(?P<code>[0-9]{6}_(SZ|SH|BJ))_(?P<start>\d{8})_(?P<end>\d{8})_csv$")
_MINUTE_RE = re.compile(r"^min_(?P<code>[0-9]{6}_(SZ|SH|BJ))_(?P<date>\d{8})_[^/]+_csv$")


KEEP_FILES = {
    "watchlist.json",
    "holdings.json",
    "holding_users.json",
    "stock_basic.csv",
}


def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _ts_to_cache_code(ts_code: str) -> str:
    text = str(ts_code or "").strip().upper()
    if "." in text:
        return text.replace(".", "_")
    if len(text) >= 8 and text[:2] in {"SH", "SZ", "BJ"}:
        return f"{text[2:8]}_{text[:2]}"
    return text


def _important_codes(cache_dir: Path) -> set[str]:
    codes: set[str] = set()

    watchlist = _load_json(cache_dir / "watchlist.json")
    if isinstance(watchlist, dict):
        groups = watchlist.get("groups")
        if isinstance(groups, dict):
            for items in groups.values():
                if isinstance(items, list):
                    for item in items:
                        codes.add(_ts_to_cache_code(str(item)))
    elif isinstance(watchlist, list):
        for item in watchlist:
            codes.add(_ts_to_cache_code(str(item)))

    holdings = _load_json(cache_dir / "holdings.json")
    rows = []
    if isinstance(holdings, dict):
        rows = holdings.get("holdings", [])
    elif isinstance(holdings, list):
        rows = holdings
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                codes.add(_ts_to_cache_code(str(row.get("ts_code", ""))))

    return {c for c in codes if c}


def cleanup_cache(
    cache_dir: Path | None = None,
    now: pd.Timestamp | None = None,
    important_keep_days: int = 35,
    normal_keep_days: int = 5,
    important_daily_keep_days: int = 120,
    normal_daily_keep_days: int = 45,
) -> dict[str, int]:
    base = Path(cache_dir or settings.cache_dir)
    base.mkdir(parents=True, exist_ok=True)
    ts_now = now or pd.Timestamp.now()

    important = _important_codes(base)

    deleted_minute = 0
    deleted_daily = 0
    deleted_misc = 0

    for path in base.iterdir():
        if not path.is_file():
            continue

        name = path.name
        if name in KEEP_FILES:
            continue
        if name.startswith("macro_"):
            continue
        if name.startswith("ak_realtime_quotes"):
            # 单文件覆盖缓存，保留
            continue
        if name.startswith("snapshots") or name.startswith("requests"):
            continue

        minute_m = _MINUTE_RE.match(name)
        if minute_m:
            code = minute_m.group("code")
            trade_date = pd.to_datetime(minute_m.group("date"), format="%Y%m%d", errors="coerce")
            if pd.isna(trade_date):
                continue
            age = int((ts_now.normalize() - trade_date.normalize()).days)
            keep_days = int(important_keep_days if code in important else normal_keep_days)
            if age > keep_days:
                try:
                    path.unlink(missing_ok=True)
                    deleted_minute += 1
                except Exception:
                    pass
            continue

        daily_m = _DAILY_RE.match(name)
        if daily_m:
            code = daily_m.group("code")
            end_date = pd.to_datetime(daily_m.group("end"), format="%Y%m%d", errors="coerce")
            if pd.isna(end_date):
                continue
            age = int((ts_now.normalize() - end_date.normalize()).days)
            keep_days = int(important_daily_keep_days if code in important else normal_daily_keep_days)
            if age > keep_days:
                try:
                    path.unlink(missing_ok=True)
                    deleted_daily += 1
                except Exception:
                    pass
            continue

        # 其他未知大文件采用 mtime 兜底清理
        try:
            mtime = pd.Timestamp(path.stat().st_mtime, unit="s")
            if (ts_now - mtime).days > 30:
                path.unlink(missing_ok=True)
                deleted_misc += 1
        except Exception:
            pass

    return {
        "deleted_minute": deleted_minute,
        "deleted_daily": deleted_daily,
        "deleted_misc": deleted_misc,
        "important_code_count": len(important),
    }
