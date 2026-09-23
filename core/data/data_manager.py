from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from config import settings
from core.data.symbols import filter_buyable_mainboard, normalize_ts_code
from core.data.tushare_client import get_pro_api
from core.strategies.factor_selection import FactorSelectionStrategy


DAILY_COLUMNS = ["ts_code", "trade_date", "open", "high", "low", "close", "pct_chg", "vol", "amount"]


def _compact_date(value: Any) -> str:
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return str(value or "").replace("-", "")[:8]
    return ts.strftime("%Y%m%d")


def _cache_code(ts_code: str) -> str:
    return normalize_ts_code(ts_code).replace(".", "_")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    return out if np.isfinite(out) else default


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "1" if default else "0") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@contextmanager
def _eastmoney_proxy_context():
    proxy_keys = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]
    old_env = {key: os.environ.get(key) for key in proxy_keys}
    if _env_flag("QUANT_EASTMONEY_DIRECT", default=True):
        for key in proxy_keys:
            os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _num_col(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index)
    raw = df[col].astype(str).str.replace("%", "", regex=False).str.replace(",", "", regex=False)
    return pd.to_numeric(raw, errors="coerce").fillna(default)


class MarketDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.db_path)

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (str(table_name),),
        ).fetchone()
        return row is not None

    def read_sector_fund_flow(self, indicator: str, sector_type: str) -> pd.DataFrame:
        if not self.db_path.exists():
            return pd.DataFrame()
        try:
            with self._connect() as conn:
                query = "SELECT * FROM sector_fund_flow WHERE 资金流周期 = ? AND 板块资金流类型 = ?"
                return pd.read_sql_query(query, conn, params=[indicator, sector_type])
        except Exception:
            return pd.DataFrame()

    def write_daily_bars(self, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        work = df.copy()
        if "ts_code" not in work.columns or "trade_date" not in work.columns:
            return 0
        work["ts_code"] = work["ts_code"].astype(str).map(normalize_ts_code)
        work["trade_date"] = work["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
        work = work.dropna(subset=["ts_code", "trade_date"])
        work = work[work["ts_code"].astype(str).str.len() > 0]
        work = work[work["trade_date"].astype(str).str.len() > 0]
        for col in [
            "turnover_rate",
            "pe",
            "pb",
            "dv_ratio",
            "total_mv",
            "pct_chg",
            "vol",
            "amount",
        ]:
            if col not in work.columns:
                work[col] = 0.0
        work = work.drop_duplicates(subset=["ts_code", "trade_date"], keep="last").reset_index(drop=True)
        if work.empty:
            return 0
        keys = list(work[["ts_code", "trade_date"]].itertuples(index=False, name=None))
        with self._connect() as conn:
            if self._table_exists(conn, "daily_bars"):
                conn.executemany("DELETE FROM daily_bars WHERE ts_code = ? AND trade_date = ?", keys)
            work.to_sql("daily_bars", conn, if_exists="append", index=False)
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_bars_code_date ON daily_bars(ts_code, trade_date)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_bars_trade_date ON daily_bars(trade_date)")
            except Exception:
                pass
        return int(len(work))

    def write_stock_basic(self, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        keep_cols = ["ts_code", "symbol", "name", "area", "industry", "market", "list_date"]
        work = df.copy()
        for col in keep_cols:
            if col not in work.columns:
                work[col] = ""
        work = work[keep_cols].drop_duplicates(subset=["ts_code"], keep="last")
        with self._connect() as conn:
            work.to_sql("stock_basic", conn, if_exists="replace", index=False)
        return int(len(work))

    def read_daily_panel(self, start_date: str, end_date: str, universe_size: int = 0) -> pd.DataFrame:
        if not self.db_path.exists():
            return pd.DataFrame()
        limit_sql = ""
        params: list[Any] = [str(start_date), str(end_date)]
        if universe_size and universe_size > 0:
            limit_sql = "LIMIT ?"
            params.append(int(universe_size))
        query = f"""
            WITH latest_codes AS (
                SELECT ts_code, MAX(trade_date) AS latest_trade_date
                FROM daily_bars
                WHERE trade_date >= ? AND trade_date <= ?
                GROUP BY ts_code
                ORDER BY latest_trade_date DESC, ts_code
                {limit_sql}
            )
            SELECT
                d.ts_code,
                d.trade_date,
                d.open,
                d.high,
                d.low,
                d.close,
                d.pct_chg,
                d.vol,
                d.amount,
                d.turnover_rate,
                d.pe,
                d.pb,
                d.dv_ratio,
                d.total_mv,
                COALESCE(b.name, '') AS name,
                COALESCE(b.industry, '') AS industry,
                COALESCE(b.market, '') AS market
            FROM daily_bars d
            INNER JOIN latest_codes c ON d.ts_code = c.ts_code
            LEFT JOIN stock_basic b ON d.ts_code = b.ts_code
            WHERE d.trade_date >= ? AND d.trade_date <= ?
            ORDER BY d.ts_code, d.trade_date
        """
        params = params + [str(start_date), str(end_date)]
        try:
            with self._connect() as conn:
                return pd.read_sql_query(query, conn, params=params)
        except Exception:
            return pd.DataFrame()

    def daily_date_counts(self, start_date: str, end_date: str) -> dict[str, int]:
        if not self.db_path.exists():
            return {}
        try:
            with self._connect() as conn:
                if not self._table_exists(conn, "daily_bars"):
                    return {}
                rows = conn.execute(
                    """
                    SELECT trade_date, COUNT(DISTINCT ts_code) AS stock_count
                    FROM daily_bars
                    WHERE trade_date >= ? AND trade_date <= ?
                    GROUP BY trade_date
                    """,
                    (str(start_date), str(end_date)),
                ).fetchall()
            return {str(day): int(count or 0) for day, count in rows}
        except Exception:
            return {}

    def recent_trade_dates(self, end_date: str, limit: int) -> list[str]:
        if not self.db_path.exists():
            return []
        try:
            with self._connect() as conn:
                if not self._table_exists(conn, "daily_bars"):
                    return []
                rows = conn.execute(
                    """
                    SELECT DISTINCT trade_date FROM daily_bars
                    WHERE trade_date <= ?
                    ORDER BY trade_date DESC LIMIT ?
                    """,
                    (str(end_date), max(1, int(limit))),
                ).fetchall()
            return sorted(str(row[0]) for row in rows)
        except Exception:
            return []

    def status(self) -> dict[str, Any]:
        base = {
            "db_path": str(self.db_path),
            "exists": self.db_path.exists(),
            "row_count": 0,
            "stock_count": 0,
            "latest_trade_date": "",
            "latest_trade_date_stock_count": 0,
        }
        if not self.db_path.exists():
            return base
        try:
            with self._connect() as conn:
                if not self._table_exists(conn, "daily_bars"):
                    return base
                row = conn.execute(
                    "SELECT COUNT(*) AS rows, COUNT(DISTINCT ts_code) AS stocks, MAX(trade_date) AS latest FROM daily_bars"
                ).fetchone()
                latest = str(row[2] or "") if row else ""
                latest_count = 0
                if latest:
                    latest_count = int(
                        conn.execute(
                            "SELECT COUNT(DISTINCT ts_code) FROM daily_bars WHERE trade_date = ?",
                            (latest,),
                        ).fetchone()[0]
                        or 0
                    )
                base.update(
                    {
                        "row_count": int(row[0] or 0) if row else 0,
                        "stock_count": int(row[1] or 0) if row else 0,
                        "latest_trade_date": latest,
                        "latest_trade_date_stock_count": latest_count,
                    }
                )
        except Exception:
            return base
        return base


class DataManager:
    def __init__(
        self,
        cache_dir: str | Path | None = None,
        market_db_path: str | Path | None = None,
        data_mode: str | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir is not None else settings.cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        db_path = Path(market_db_path) if market_db_path is not None else settings.market_db_path
        self.market_db = MarketDB(db_path)
        self.data_mode = str(data_mode or settings.data_mode or "real").strip().lower()
        if self.data_mode not in {"real", "demo", "test"}:
            raise ValueError(f"unsupported DATA_MODE: {self.data_mode}")

    def _daily_cache_path(self, ts_code: str, start_date: str, end_date: str) -> Path:
        return self.cache_dir / f"{_cache_code(ts_code)}_{_compact_date(start_date)}_{_compact_date(end_date)}_csv"

    def _read_daily_cache(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        exact = self._daily_cache_path(ts_code, start_date, end_date)
        paths = [exact] if exact.exists() else []
        if not paths:
            paths = sorted(self.cache_dir.glob(f"{_cache_code(ts_code)}_*_csv"), key=lambda p: p.stat().st_mtime, reverse=True)
        frames: list[pd.DataFrame] = []
        for path in paths[:20]:
            try:
                df = pd.read_csv(path)
            except Exception:
                continue
            if not df.empty:
                frames.append(df)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True, sort=False)
        return self._normalize_daily_df(out, ts_code, start_date, end_date)

    def _normalize_daily_df(self, df: pd.DataFrame, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame(columns=DAILY_COLUMNS)
        out = df.copy()
        if "trade_date" not in out.columns and "date" in out.columns:
            out = out.rename(columns={"date": "trade_date"})
        if "ts_code" not in out.columns:
            out["ts_code"] = normalize_ts_code(ts_code)
        out["ts_code"] = out["ts_code"].astype(str).map(lambda x: normalize_ts_code(x) if x else normalize_ts_code(ts_code))
        trade_date_raw = out["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
        parsed_trade_date = pd.to_datetime(trade_date_raw, format="%Y%m%d", errors="coerce")
        fallback_trade_date = pd.to_datetime(out["trade_date"], errors="coerce")
        out["trade_date"] = parsed_trade_date.fillna(fallback_trade_date)
        out = out.dropna(subset=["trade_date"])
        start_ts = pd.to_datetime(_compact_date(start_date), format="%Y%m%d", errors="coerce")
        end_ts = pd.to_datetime(_compact_date(end_date), format="%Y%m%d", errors="coerce")
        if pd.notna(start_ts):
            out = out[out["trade_date"] >= start_ts]
        if pd.notna(end_ts):
            out = out[out["trade_date"] <= end_ts]
        for col in ["open", "high", "low", "close", "pct_chg", "vol", "amount", "turnover_rate", "pe", "pb", "dv_ratio", "total_mv"]:
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
        out = out.sort_values(["ts_code", "trade_date"]).drop_duplicates(["ts_code", "trade_date"], keep="last")
        out["trade_date"] = out["trade_date"].dt.strftime("%Y%m%d")
        for col in DAILY_COLUMNS:
            if col not in out.columns:
                out[col] = 0.0 if col not in {"ts_code", "trade_date"} else ""
        return out.reset_index(drop=True)

    def _mock_daily_data(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        start_ts = pd.to_datetime(_compact_date(start_date), format="%Y%m%d", errors="coerce")
        end_ts = pd.to_datetime(_compact_date(end_date), format="%Y%m%d", errors="coerce")
        if pd.isna(start_ts) or pd.isna(end_ts) or start_ts > end_ts:
            end_ts = pd.Timestamp.today().normalize()
            start_ts = end_ts - pd.Timedelta(days=180)
        days = pd.bdate_range(start_ts, end_ts)
        if len(days) == 0:
            days = pd.bdate_range(end_ts - pd.Timedelta(days=30), end_ts)
        seed = int(hashlib.sha256(normalize_ts_code(ts_code).encode("utf-8")).hexdigest()[:8], 16)
        rng = np.random.default_rng(seed)
        returns = rng.normal(0.0008, 0.018, len(days))
        close = np.maximum(1.0, 10.0 * np.cumprod(1.0 + returns))
        open_px = close * (1.0 + rng.normal(0, 0.006, len(days)))
        high = np.maximum(open_px, close) * (1.0 + rng.random(len(days)) * 0.015)
        low = np.minimum(open_px, close) * (1.0 - rng.random(len(days)) * 0.015)
        vol = rng.integers(50_000, 500_000, len(days)).astype(float)
        amount = vol * close
        out = pd.DataFrame(
            {
                "ts_code": normalize_ts_code(ts_code),
                "trade_date": days.strftime("%Y%m%d"),
                "open": open_px,
                "high": high,
                "low": low,
                "close": close,
                "pct_chg": pd.Series(close).pct_change().fillna(0).to_numpy() * 100.0,
                "vol": vol,
                "amount": amount,
            }
        )
        out["data_source_mode"] = self.data_mode
        out["_source"] = "SYNTHETIC_FIXTURE"
        return out

    def _fetch_daily_tushare(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        pro = get_pro_api()
        if pro is None:
            return pd.DataFrame()
        try:
            df = pro.daily(ts_code=normalize_ts_code(ts_code), start_date=_compact_date(start_date), end_date=_compact_date(end_date))
        except Exception:
            return pd.DataFrame()
        return self._normalize_daily_df(df, ts_code, start_date, end_date)

    def _fetch_daily_tushare_by_trade_days(self, trade_days: list[str]) -> pd.DataFrame:
        days = [str(day) for day in trade_days if str(day or "").strip()]
        if not days:
            return pd.DataFrame()
        pro = get_pro_api()
        if pro is None:
            return pd.DataFrame()
        frames: list[pd.DataFrame] = []
        for day in days:
            try:
                df = pro.daily(trade_date=day)
            except Exception:
                continue
            if df is not None and not df.empty:
                frames.append(df)
        if not frames:
            return pd.DataFrame()
        raw = pd.concat(frames, ignore_index=True, sort=False)
        return self._normalize_daily_df(raw, "", min(days), max(days))

    def get_daily_data(self, ts_code: str, start_date: str, end_date: str, use_cache: bool = True) -> pd.DataFrame:
        normalized = normalize_ts_code(ts_code)
        if use_cache:
            cached = self._read_daily_cache(normalized, start_date, end_date)
            if not cached.empty:
                return cached
        fetched = self._fetch_daily_tushare(normalized, start_date, end_date)
        if fetched.empty and self.data_mode in {"demo", "test"}:
            fetched = self._mock_daily_data(normalized, start_date, end_date)
        if not fetched.empty:
            try:
                fetched.to_csv(self._daily_cache_path(normalized, start_date, end_date), index=False)
            except Exception:
                pass
        return fetched

    def get_stock_basic(self, use_cache: bool = True) -> pd.DataFrame:
        path = self.cache_dir / "stock_basic.csv"
        if use_cache and path.exists():
            try:
                return pd.read_csv(path)
            except Exception:
                pass
        pro = get_pro_api()
        df = pd.DataFrame()
        if pro is not None:
            try:
                df = pro.stock_basic(exchange="", list_status="L", fields="ts_code,symbol,name,area,industry,market,list_date")
            except Exception:
                df = pd.DataFrame()
        if df.empty and self.data_mode in {"demo", "test"}:
            df = pd.DataFrame(
                [
                    {"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行"},
                    {"ts_code": "600519.SH", "symbol": "600519", "name": "贵州茅台"},
                    {"ts_code": "600986.SH", "symbol": "600986", "name": "浙文互联"},
                ]
            )
            df["data_source_mode"] = self.data_mode
            df["_source"] = "SYNTHETIC_FIXTURE"
        try:
            df.to_csv(path, index=False)
        except Exception:
            pass
        return df

    def get_realtime_prices_akshare(
        self,
        ts_codes: Iterable[str],
        refresh_seconds: int = 120,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        codes = [normalize_ts_code(c) for c in ts_codes if str(c or "").strip()]
        if not codes:
            return pd.DataFrame(columns=["ts_code", "name", "realtime_price", "quote_time", "price_source"])
        cache_path = self.cache_dir / "ak_realtime_quotes.csv"
        if (not force_refresh) and cache_path.exists():
            age = pd.Timestamp.now().timestamp() - cache_path.stat().st_mtime
            if age <= max(1, int(refresh_seconds)):
                try:
                    cached = pd.read_csv(cache_path)
                    cached = cached[cached["ts_code"].astype(str).isin(codes)]
                    if not cached.empty:
                        return cached.reset_index(drop=True)
                except Exception:
                    pass
        df = pd.DataFrame()
        try:
            import akshare as ak

            spot = ak.stock_zh_a_spot_em()
            if spot is not None and not spot.empty:
                code_col = "代码" if "代码" in spot.columns else "code"
                name_col = "名称" if "名称" in spot.columns else "name"
                price_col = "最新价" if "最新价" in spot.columns else "close"
                spot = spot.copy()
                spot["ts_code"] = spot[code_col].astype(str).map(normalize_ts_code)
                df = pd.DataFrame(
                    {
                        "ts_code": spot["ts_code"],
                        "name": spot.get(name_col, ""),
                        "realtime_price": pd.to_numeric(spot.get(price_col, 0), errors="coerce").fillna(0.0),
                        "quote_time": pd.Timestamp.now().isoformat(),
                        "price_source": "akshare_live",
                    }
                )
                df = df[df["ts_code"].isin(codes)].reset_index(drop=True)
        except Exception:
            df = pd.DataFrame()
        if df.empty:
            rows = []
            today = pd.Timestamp.now().strftime("%Y%m%d")
            start = (pd.Timestamp.now() - pd.Timedelta(days=30)).strftime("%Y%m%d")
            for code in codes:
                daily = self.get_daily_data(code, start, today, use_cache=True)
                price = float(daily.sort_values("trade_date").iloc[-1]["close"]) if not daily.empty else 0.0
                rows.append(
                    {
                        "ts_code": code,
                        "name": "",
                        "realtime_price": price,
                        "quote_time": pd.Timestamp.now().isoformat(),
                        "price_source": "daily_close",
                    }
                )
            df = pd.DataFrame(rows)
        try:
            df.to_csv(cache_path, index=False)
        except Exception:
            pass
        return df

    def get_minute_data(
        self,
        ts_code: str,
        trade_date: str,
        freq: str = "1min",
        source: str = "akshare",
        use_cache: bool = True,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        code = normalize_ts_code(ts_code)
        period = "".join(ch for ch in str(freq) if ch.isdigit()) or "1"
        cache_path = self.cache_dir / f"min_{_cache_code(code)}_{_compact_date(trade_date)}_{period}_csv"
        if use_cache and (not force_refresh) and cache_path.exists():
            try:
                return pd.read_csv(cache_path)
            except Exception:
                pass
        df = pd.DataFrame()
        if source == "akshare":
            try:
                import akshare as ak

                raw = ak.stock_zh_a_hist_min_em(symbol=code[:6], period=period, adjust="")
                if raw is not None and not raw.empty:
                    time_col = "时间" if "时间" in raw.columns else raw.columns[0]
                    close_col = "收盘" if "收盘" in raw.columns else "close"
                    open_col = "开盘" if "开盘" in raw.columns else close_col
                    high_col = "最高" if "最高" in raw.columns else close_col
                    low_col = "最低" if "最低" in raw.columns else close_col
                    vol_col = "成交量" if "成交量" in raw.columns else None
                    amount_col = "成交额" if "成交额" in raw.columns else None
                    df = pd.DataFrame(
                        {
                            "ts_code": code,
                            "trade_time": pd.to_datetime(raw[time_col], errors="coerce"),
                            "open": pd.to_numeric(raw[open_col], errors="coerce"),
                            "high": pd.to_numeric(raw[high_col], errors="coerce"),
                            "low": pd.to_numeric(raw[low_col], errors="coerce"),
                            "close": pd.to_numeric(raw[close_col], errors="coerce"),
                            "vol": pd.to_numeric(raw[vol_col], errors="coerce") if vol_col else 0.0,
                            "amount": pd.to_numeric(raw[amount_col], errors="coerce") if amount_col else 0.0,
                        }
                    )
                    target = _compact_date(trade_date)
                    df = df[df["trade_time"].dt.strftime("%Y%m%d") == target]
            except Exception:
                df = pd.DataFrame()
        if not df.empty:
            df["trade_date"] = pd.to_datetime(df["trade_time"], errors="coerce").dt.strftime("%Y%m%d")
            df["minute_time"] = pd.to_datetime(df["trade_time"], errors="coerce")
            df = df.dropna(subset=["minute_time", "close"]).reset_index(drop=True)
            try:
                df.to_csv(cache_path, index=False)
            except Exception:
                pass
        return df

    def _recent_trade_days(self, end_date: str, lookback_trade_days: int = 60) -> list[str]:
        end_ts = pd.to_datetime(_compact_date(end_date), format="%Y%m%d", errors="coerce")
        if pd.isna(end_ts):
            end_ts = pd.Timestamp.today().normalize()
        periods = max(1, int(lookback_trade_days))
        if self.data_mode in {"demo", "test"}:
            return pd.bdate_range(end=end_ts, periods=periods).strftime("%Y%m%d").tolist()
        local_sessions = self.market_db.recent_trade_dates(end_ts.strftime("%Y%m%d"), periods)
        if len(local_sessions) >= periods:
            return local_sessions
        pro = get_pro_api()
        if pro is None:
            return local_sessions
        start_ts = end_ts - pd.Timedelta(days=max(30, periods * 2 + 20))
        try:
            calendar = pro.trade_cal(
                exchange="SSE",
                start_date=start_ts.strftime("%Y%m%d"),
                end_date=end_ts.strftime("%Y%m%d"),
                is_open="1",
            )
        except Exception:
            return local_sessions
        if calendar is None or calendar.empty or "cal_date" not in calendar.columns:
            return local_sessions
        open_mask = pd.to_numeric(calendar.get("is_open", 1), errors="coerce").fillna(0).eq(1)
        sessions = sorted(calendar.loc[open_mask, "cal_date"].dropna().astype(str).unique().tolist())
        return sorted(set(local_sessions).union(sessions))[-periods:]

    def _prepare_db_priority_panel(
        self,
        end_date: str,
        use_days: list[str],
        universe_size: int = 0,
        remote_fields: str = "",
        force_refresh: bool = False,
    ) -> tuple[pd.DataFrame, list[str], dict[str, Any]]:
        start = min(use_days) if use_days else (pd.Timestamp.now() - pd.Timedelta(days=120)).strftime("%Y%m%d")
        end = max(use_days) if use_days else _compact_date(end_date)
        db_panel = self.market_db.read_daily_panel(start_date=start, end_date=end, universe_size=universe_size)
        if db_panel is not None and not db_panel.empty and not force_refresh:
            codes = db_panel["ts_code"].dropna().astype(str).drop_duplicates().tolist()
            return db_panel.reset_index(drop=True), codes, {"source": "market_db"}

        codes = self.get_stock_basic(use_cache=True).get("ts_code", pd.Series(dtype=str)).astype(str).tolist()
        if universe_size and universe_size > 0:
            codes = codes[: int(universe_size)]
        frames = []
        for code in codes[: max(1, int(universe_size or len(codes) or 3))]:
            frames.append(self.get_daily_data(code, start, end, use_cache=not force_refresh))
        panel = pd.concat([f for f in frames if f is not None and not f.empty], ignore_index=True, sort=False) if frames else pd.DataFrame()
        return panel, codes, {"source": "cache_or_tushare"}

    def _generate_signal_panel_from_panel(
        self,
        panel: pd.DataFrame,
        threshold: float = 0.1,
        enabled_factors: list[str] | None = None,
        factor_weights: dict[str, float] | None = None,
    ) -> pd.DataFrame:
        if panel is None or panel.empty or "ts_code" not in panel.columns:
            return pd.DataFrame()
        strategy = FactorSelectionStrategy(
            threshold=threshold,
            enabled_factors=enabled_factors or ["momentum_20"],
            factor_weights=factor_weights or {},
        )
        parts = []
        for code, hist in panel.groupby("ts_code", sort=False):
            try:
                parts.append(strategy.generate_signals(hist.sort_values("trade_date").copy()))
            except Exception:
                continue
        return pd.concat(parts, ignore_index=True, sort=False) if parts else pd.DataFrame()

    def get_market_db_status(self) -> dict[str, Any]:
        status = self.market_db.status()
        status["cache_dir"] = str(self.cache_dir)
        return status

    def sync_market_data_window_db(self, end_date: str, lookback_trade_days: int = 60, force_refresh: bool = False) -> dict[str, Any]:
        days = self._recent_trade_days(end_date, lookback_trade_days)
        start = min(days) if days else _compact_date(end_date)
        end = max(days) if days else _compact_date(end_date)
        stock_basic = self.get_stock_basic(use_cache=True)
        stock_basic_codes = stock_basic.get("ts_code", pd.Series(dtype=str)).astype(str).map(normalize_ts_code).drop_duplicates().tolist()
        buyable_count = int(len(filter_buyable_mainboard(stock_basic))) if stock_basic is not None and not stock_basic.empty else len(stock_basic_codes)
        existing_status = self.market_db.status()
        date_counts = self.market_db.daily_date_counts(start, end)
        min_expected_count = max(1, int((buyable_count or len(stock_basic_codes) or 1) * 0.75))
        needs_bootstrap = int(existing_status.get("stock_count", 0) or 0) < min_expected_count
        fetch_days = list(days) if force_refresh or needs_bootstrap else [day for day in days if int(date_counts.get(str(day), 0) or 0) < min_expected_count]

        panel = self._fetch_daily_tushare_by_trade_days(fetch_days)
        meta = {
            "source": "tushare_daily_by_trade_date" if not panel.empty else "market_db_or_cache",
            "fetch_days": fetch_days,
            "min_expected_count": min_expected_count,
            "existing_stock_count": int(existing_status.get("stock_count", 0) or 0),
        }
        codes = stock_basic_codes
        if panel.empty:
            panel, codes, fallback_meta = self._prepare_db_priority_panel(
                end_date=end_date,
                use_days=days,
                universe_size=0,
                force_refresh=bool(force_refresh),
            )
            meta.update(fallback_meta)
        rows = 0
        stock_basic_rows = 0
        try:
            rows = self.market_db.write_daily_bars(panel)
            stock_basic_rows = self.market_db.write_stock_basic(stock_basic)
        except Exception:
            rows = 0
        if force_refresh:
            sync_reason = "force"
        elif rows > 0 and needs_bootstrap:
            sync_reason = "bootstrap"
        elif rows > 0:
            sync_reason = "catchup"
        else:
            sync_reason = "cache"
        return {
            "status": "ok",
            "sync_reason": sync_reason,
            "rows": rows,
            "stock_basic_rows": stock_basic_rows,
            "codes": len(codes),
            "meta": meta,
            "market_db_status": self.market_db.status(),
        }

    def get_sector_fund_flow_rank(
        self,
        indicator: str = "10日",
        sector_type: str = "行业资金流",
        refresh_hours: int = 8,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        if not force_refresh:
            db_df = self.market_db.read_sector_fund_flow(indicator=indicator, sector_type=sector_type)
            if db_df is not None and not db_df.empty:
                return db_df
        df = pd.DataFrame()
        ak_indicator = "今日" if str(indicator) == "当日" else str(indicator)
        try:
            import akshare as ak

            with _eastmoney_proxy_context():
                raw = ak.stock_sector_fund_flow_rank(indicator=ak_indicator, sector_type=sector_type)
            if raw is not None and not raw.empty:
                df = raw.copy()
                if "名称" in df.columns and "industry" not in df.columns:
                    df = df.rename(columns={"名称": "industry"})
                df["资金流周期"] = indicator
                df["板块资金流类型"] = sector_type
                df["资金流来源"] = "akshare_live"
                df["更新时间"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            df = pd.DataFrame()
        if df.empty:
            df = self._fetch_ths_sector_fund_flow(indicator=indicator, sector_type=sector_type)
        if df.empty:
            df = self._local_sector_fund_flow_proxy(indicator=indicator, sector_type=sector_type)
        return df

    def _fetch_ths_sector_fund_flow(self, indicator: str, sector_type: str) -> pd.DataFrame:
        if str(sector_type) != "行业资金流":
            return pd.DataFrame()
        symbol_map = {"当日": "即时", "今日": "即时", "3日": "3日排行", "5日": "5日排行", "10日": "10日排行"}
        symbol = symbol_map.get(str(indicator))
        if not symbol:
            return pd.DataFrame()
        try:
            import akshare as ak

            with _eastmoney_proxy_context():
                raw = ak.stock_fund_flow_industry(symbol=symbol)
        except Exception:
            return pd.DataFrame()
        if raw is None or raw.empty:
            return pd.DataFrame()

        work = raw.copy()
        name_col = "行业" if "行业" in work.columns else "名称" if "名称" in work.columns else ""
        if not name_col:
            return pd.DataFrame()
        pct_col = "行业-涨跌幅" if "行业-涨跌幅" in work.columns else "阶段涨跌幅" if "阶段涨跌幅" in work.columns else "涨跌幅"
        out = pd.DataFrame(
            {
                "industry": work[name_col].astype(str),
                "涨跌幅": _num_col(work, pct_col),
                "主力净流入净额": _num_col(work, "净额"),
                "流入资金": _num_col(work, "流入资金"),
                "流出资金": _num_col(work, "流出资金"),
                "成分股数": _num_col(work, "公司家数"),
            }
        )
        denom = (out["流入资金"].abs() + out["流出资金"].abs()).replace(0.0, np.nan)
        out["主力净流入净占比"] = (out["主力净流入净额"] / denom * 100.0).fillna(0.0)
        out["资金流周期"] = indicator
        out["板块资金流类型"] = sector_type
        out["资金流来源"] = "akshare_ths_live"
        out["更新时间"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
        out = out.sort_values(["主力净流入净额", "涨跌幅"], ascending=[False, False]).reset_index(drop=True)
        return out

    def _local_sector_fund_flow_proxy(self, indicator: str, sector_type: str) -> pd.DataFrame:
        now_text = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")
        fallback = pd.DataFrame(
            [
                {
                    "industry": "缓存代理",
                    "涨跌幅": 0.0,
                    "主力净流入净额": 0.0,
                    "主力净流入净占比": 0.0,
                    "资金流周期": indicator,
                    "板块资金流类型": sector_type,
                    "资金流来源": "empty_fallback",
                    "更新时间": now_text,
                }
            ]
        )
        db_path = getattr(self.market_db, "db_path", None)
        if db_path is None or not db_path.exists():
            return fallback

        try:
            with self.market_db._connect() as conn:
                dates = pd.read_sql_query(
                    "SELECT DISTINCT trade_date FROM daily_bars ORDER BY trade_date DESC LIMIT 12",
                    conn,
                )["trade_date"].astype(str).tolist()
        except Exception:
            return fallback
        if len(dates) < 2:
            return fallback

        periods = {"当日": 1, "今日": 1, "3日": 3, "5日": 5, "10日": 10}
        lag = min(int(periods.get(str(indicator), 10)), len(dates) - 1)
        latest_date = dates[0]
        base_date = dates[lag]
        try:
            with self.market_db._connect() as conn:
                df = pd.read_sql_query(
                    """
                    SELECT
                        d.ts_code,
                        d.trade_date,
                        d.close,
                        d.amount,
                        COALESCE(b.industry, '') AS industry
                    FROM daily_bars d
                    LEFT JOIN stock_basic b ON d.ts_code = b.ts_code
                    WHERE d.trade_date IN (?, ?)
                    """,
                    conn,
                    params=[latest_date, base_date],
                )
        except Exception:
            return fallback
        if df is None or df.empty:
            return fallback

        work = df.copy()
        work["industry"] = work["industry"].fillna("").astype(str).str.strip()
        work.loc[work["industry"].isin(["", "nan", "None"]), "industry"] = "未分类"
        work["close"] = pd.to_numeric(work["close"], errors="coerce")
        work["amount"] = pd.to_numeric(work["amount"], errors="coerce").fillna(0.0)
        latest_rows = work[work["trade_date"].astype(str).eq(str(latest_date))][["ts_code", "industry", "close", "amount"]].rename(
            columns={"close": "latest_close", "amount": "latest_amount"}
        )
        base_rows = work[work["trade_date"].astype(str).eq(str(base_date))][["ts_code", "close", "amount"]].rename(
            columns={"close": "base_close", "amount": "base_amount"}
        )
        merged = latest_rows.merge(base_rows, on="ts_code", how="inner")
        if merged.empty:
            return fallback
        merged["涨跌幅"] = (pd.to_numeric(merged["latest_close"], errors="coerce") / (pd.to_numeric(merged["base_close"], errors="coerce") + 1e-12) - 1.0) * 100.0
        merged["成交额变化"] = pd.to_numeric(merged["latest_amount"], errors="coerce").fillna(0.0) - pd.to_numeric(merged["base_amount"], errors="coerce").fillna(0.0)
        grouped = merged.groupby("industry", as_index=False).agg(
            涨跌幅=("涨跌幅", "mean"),
            主力净流入净额=("成交额变化", "sum"),
            最新成交额=("latest_amount", "sum"),
            成分股数=("ts_code", "nunique"),
        )
        if grouped.empty:
            return fallback
        grouped["主力净流入净占比"] = grouped["主力净流入净额"] / (grouped["最新成交额"].abs() + 1e-12) * 100.0
        grouped["资金流周期"] = indicator
        grouped["板块资金流类型"] = sector_type
        grouped["资金流来源"] = "local_market_proxy"
        grouped["更新时间"] = now_text
        grouped = grouped.sort_values(["主力净流入净额", "涨跌幅"], ascending=[False, False]).reset_index(drop=True)
        return grouped[
            [
                "industry",
                "涨跌幅",
                "主力净流入净额",
                "主力净流入净占比",
                "最新成交额",
                "成分股数",
                "资金流周期",
                "板块资金流类型",
                "资金流来源",
                "更新时间",
            ]
        ]

    @staticmethod
    def _flow_mode(source: str) -> str:
        source = str(source or "")
        if "akshare" in source or "live" in source:
            return "真实资金流"
        if source:
            return "代理资金流"
        return "未知"

    def build_sector_fund_flow_snapshot(
        self,
        indicators: list[str] | None = None,
        sector_type: str = "行业资金流",
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        indicators = indicators or ["当日", "3日", "10日"]
        flow_map: dict[str, pd.DataFrame] = {}
        summary_map: dict[str, dict[str, Any]] = {}
        today_df = pd.DataFrame()
        for indicator in indicators:
            df = self.get_sector_fund_flow_rank(indicator=indicator, sector_type=sector_type, force_refresh=force_refresh)
            if df is None:
                df = pd.DataFrame()
            df = df.copy()
            if not df.empty:
                if "industry" not in df.columns:
                    first = df.columns[0]
                    df = df.rename(columns={first: "industry"})
                source = str(df.iloc[0].get("资金流来源", "") or "")
                df["资金流模式"] = self._flow_mode(source)
                if indicator in {"当日", "今日"}:
                    today_df = df.copy()
                if indicator not in {"当日", "今日"} and not today_df.empty:
                    today_cols = today_df[["industry", "资金流来源", "资金流模式"]].rename(
                        columns={"资金流来源": "当日资金流来源", "资金流模式": "当日资金流模式"}
                    )
                    df = df.merge(today_cols, on="industry", how="left")
                flow_map[indicator] = df
                summary_map[indicator] = {
                    "资金流周期": indicator,
                    "板块资金流类型": sector_type,
                    "资金流来源": source,
                    "资金流模式": self._flow_mode(source),
                    "更新时间": str(df.iloc[0].get("更新时间", "")),
                    "当日资金流来源": str(df.iloc[0].get("当日资金流来源", "")),
                    "当日资金流模式": str(df.iloc[0].get("当日资金流模式", "")),
                    "当日更新时间": str(df.iloc[0].get("当日更新时间", "")),
                    "板块数": int(len(df)),
                }
            else:
                flow_map[indicator] = pd.DataFrame()
                summary_map[indicator] = {"资金流周期": indicator, "板块数": 0}
        return {
            "available_indicators": indicators,
            "sector_fund_flow_df_map": flow_map,
            "sector_fund_flow_summary_map": summary_map,
        }

    def _empty_candidates(self, top_n: int = 0) -> pd.DataFrame:
        return pd.DataFrame(columns=["ts_code", "name", "推荐评分", "建议仓位%", "候选来源"]).head(top_n)

    @staticmethod
    def _latest_feature_rows(panel: pd.DataFrame) -> pd.DataFrame:
        if panel is None or panel.empty or "ts_code" not in panel.columns:
            return pd.DataFrame()
        rows = []
        for code, hist in panel.groupby("ts_code", sort=False):
            hist = hist.sort_values("trade_date").copy()
            if len(hist) < 20:
                continue
            close = pd.to_numeric(hist["close"], errors="coerce")
            high = pd.to_numeric(hist.get("high", close), errors="coerce")
            low = pd.to_numeric(hist.get("low", close), errors="coerce")
            amount = pd.to_numeric(hist.get("amount", 0.0), errors="coerce").fillna(0.0)
            ma5 = close.rolling(5).mean()
            ma10 = close.rolling(10).mean()
            ma20 = close.rolling(20).mean()
            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            dif = ema12 - ema26
            dea = dif.ewm(span=9, adjust=False).mean()
            macd = 2 * (dif - dea)
            latest = hist.iloc[-1].copy()
            latest["momentum_20"] = close.pct_change(20).iloc[-1]
            latest["momentum_5"] = close.pct_change(5).iloc[-1]
            latest["ma5"] = ma5.iloc[-1]
            latest["ma10"] = ma10.iloc[-1]
            latest["ma20"] = ma20.iloc[-1]
            latest["ma5_bias"] = close.iloc[-1] / (ma5.iloc[-1] + 1e-12) - 1.0
            latest["ma10_bias"] = close.iloc[-1] / (ma10.iloc[-1] + 1e-12) - 1.0
            latest["amount_5"] = amount.tail(5).mean()
            latest["macd"] = macd.iloc[-1]
            latest["macd_prev"] = macd.iloc[-2] if len(macd) >= 2 else 0.0
            latest["range_20_pos"] = (close.iloc[-1] - low.rolling(20).min().iloc[-1]) / (
                high.rolling(20).max().iloc[-1] - low.rolling(20).min().iloc[-1] + 1e-12
            )
            rows.append(latest)
        return pd.DataFrame(rows).reset_index(drop=True) if rows else pd.DataFrame()

    @staticmethod
    def _minmax_score(series: pd.Series, neutral: float = 50.0) -> pd.Series:
        values = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
        if values.notna().sum() <= 1:
            return pd.Series(neutral, index=series.index)
        lo = float(values.min())
        hi = float(values.max())
        if abs(hi - lo) < 1e-12:
            return pd.Series(neutral, index=series.index)
        return ((values - lo) / (hi - lo) * 100.0).fillna(neutral)

    def recommend_scientific_candidates(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        end_date = str(kwargs.get("end_date") or pd.Timestamp.now().strftime("%Y%m%d"))
        lookback_days = int(kwargs.get("lookback_days", 90) or 90)
        top_n = int(kwargs.get("top_n", 8) or 8)
        universe_size = int(kwargs.get("universe_size", 200) or 200)
        total_position_pct = float(kwargs.get("total_position_pct", 60.0) or 60.0)
        max_single_position_pct = float(kwargs.get("max_single_position_pct", 10.0) or 10.0)
        start = (pd.to_datetime(_compact_date(end_date), format="%Y%m%d", errors="coerce") - pd.Timedelta(days=lookback_days * 2)).strftime("%Y%m%d")
        days = self._recent_trade_days(end_date, lookback_trade_days=max(40, lookback_days))
        panel, _, _ = self._prepare_db_priority_panel(end_date=end_date, use_days=days or [start, _compact_date(end_date)], universe_size=universe_size)
        latest = self._latest_feature_rows(panel)
        if latest.empty:
            return self._empty_candidates(top_n)

        latest = latest.copy()
        score = (
            0.45 * self._minmax_score(latest["momentum_20"])
            + 0.20 * self._minmax_score(latest["momentum_5"])
            + 0.20 * self._minmax_score(latest["range_20_pos"])
            + 0.15 * self._minmax_score(np.log1p(pd.to_numeric(latest["amount_5"], errors="coerce").fillna(0.0)))
        )
        latest["推荐评分"] = score.round(2)
        latest["建议仓位%"] = np.minimum(max_single_position_pct, total_position_pct / max(1, top_n)).round(2)
        latest["候选来源"] = "科学选股"
        latest["20日动量%"] = (pd.to_numeric(latest["momentum_20"], errors="coerce") * 100.0).round(2)
        latest["5日动量%"] = (pd.to_numeric(latest["momentum_5"], errors="coerce") * 100.0).round(2)
        latest["最新收盘"] = pd.to_numeric(latest["close"], errors="coerce").round(3)
        latest["成交额5日均值"] = pd.to_numeric(latest["amount_5"], errors="coerce").round(2)
        out_cols = [
            "ts_code",
            "name",
            "industry",
            "trade_date",
            "最新收盘",
            "推荐评分",
            "建议仓位%",
            "20日动量%",
            "5日动量%",
            "成交额5日均值",
            "候选来源",
        ]
        out = latest.sort_values("推荐评分", ascending=False).head(top_n).reset_index(drop=True)
        for col in out_cols:
            if col not in out.columns:
                out[col] = ""
        out.attrs["sector_rotation_df"] = pd.DataFrame()
        out.attrs["sector_rotation_summary"] = {}
        out.attrs["macro_regime"] = {}
        out.attrs["macro_detail_df"] = pd.DataFrame()
        return out[out_cols]

    def list_recent_sector_entry_signals(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        end_date = str(kwargs.get("end_date") or pd.Timestamp.now().strftime("%Y%m%d"))
        lookback_days = int(kwargs.get("lookback_days", 120) or 120)
        recent_signal_days = int(kwargs.get("recent_signal_days", 10) or 10)
        universe_size = int(kwargs.get("universe_size", 0) or 0)
        threshold = float(kwargs.get("threshold", 0.1) or 0.1)
        enabled_factors = list(kwargs.get("enabled_factors") or ["momentum_20"])
        factor_weights = dict(kwargs.get("factor_weights") or {k: 1.0 for k in enabled_factors})

        days = self._recent_trade_days(end_date, lookback_trade_days=max(45, lookback_days))
        panel, _, _ = self._prepare_db_priority_panel(
            end_date=end_date,
            use_days=days,
            universe_size=universe_size,
            force_refresh=bool(kwargs.get("force_refresh", False)),
        )
        panel = filter_buyable_mainboard(panel)
        if panel.empty:
            return pd.DataFrame()

        signals = self._generate_signal_panel_from_panel(
            panel=panel,
            threshold=threshold,
            enabled_factors=enabled_factors,
            factor_weights=factor_weights,
        )
        if signals.empty or "signal" not in signals.columns:
            return pd.DataFrame()

        work = filter_buyable_mainboard(signals)
        if work.empty:
            return pd.DataFrame()
        work["trade_date"] = pd.to_datetime(work["trade_date"], errors="coerce")
        work = work.dropna(subset=["trade_date"]).sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
        work["signal"] = pd.to_numeric(work["signal"], errors="coerce").fillna(0).astype(int)
        work["prev_signal"] = work.groupby("ts_code")["signal"].shift(1).fillna(0).astype(int)
        entry = work[(work["signal"].eq(1)) & (~work["prev_signal"].eq(1))].copy()
        if entry.empty:
            return pd.DataFrame()

        latest_allowed = pd.to_datetime(_compact_date(end_date), format="%Y%m%d", errors="coerce")
        if pd.isna(latest_allowed):
            latest_allowed = work["trade_date"].max()
        earliest_allowed = latest_allowed - pd.Timedelta(days=max(1, recent_signal_days) * 2)
        entry = entry[(entry["trade_date"] <= latest_allowed) & (entry["trade_date"] >= earliest_allowed)].copy()
        if entry.empty:
            return pd.DataFrame()

        entry = entry.sort_values(["ts_code", "trade_date"]).groupby("ts_code", as_index=False).tail(1).copy()
        for col in ["close", "factor_score"]:
            if col not in entry.columns:
                entry[col] = 0.0
            entry[col] = pd.to_numeric(entry[col], errors="coerce")

        rows = []
        panel_sorted = panel.copy()
        panel_sorted["trade_date"] = pd.to_datetime(panel_sorted["trade_date"], errors="coerce")
        for _, row in entry.iterrows():
            code = str(row.get("ts_code", "") or "")
            signal_date = pd.to_datetime(row.get("trade_date"), errors="coerce")
            hist = panel_sorted[(panel_sorted["ts_code"].astype(str) == code) & (panel_sorted["trade_date"] <= signal_date)].sort_values("trade_date")
            close = pd.to_numeric(hist.get("close", pd.Series(dtype=float)), errors="coerce")
            latest_close = float(close.iloc[-1]) if len(close) else float(row.get("close", 0.0) or 0.0)
            ret_5 = float(close.iloc[-1] / (close.iloc[-6] + 1e-12) - 1.0) if len(close) >= 6 else 0.0
            ret_20 = float(close.iloc[-1] / (close.iloc[-21] + 1e-12) - 1.0) if len(close) >= 21 else 0.0
            rows.append(
                {
                    "分组": "全市场刚出现建仓信号",
                    "信号日期": signal_date.strftime("%Y-%m-%d") if pd.notna(signal_date) else "",
                    "ts_code": code,
                    "name": str(row.get("name", "") or ""),
                    "industry": str(row.get("industry", "") or ""),
                    "market": str(row.get("market", "") or ""),
                    "最新收盘": latest_close,
                    "最新因子得分": float(row.get("factor_score", 0.0) or 0.0),
                    "近5日涨跌": ret_5,
                    "近20日涨跌": ret_20,
                }
            )

        out = pd.DataFrame(rows)
        if out.empty:
            return out
        out["信号日期"] = pd.to_datetime(out["信号日期"], errors="coerce")
        out = out.sort_values(["信号日期", "最新因子得分"], ascending=[False, False]).reset_index(drop=True)
        out["信号日期"] = out["信号日期"].dt.strftime("%Y-%m-%d")
        return out

    def recommend_dragon_candidates(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        return self._empty_candidates(int(kwargs.get("top_n", 0) or 0))

    def recommend_ma5_pullback_candidates(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        end_date = str(kwargs.get("end_date") or pd.Timestamp.now().strftime("%Y%m%d"))
        lookback_days = int(kwargs.get("lookback_days", 120) or 120)
        top_n = int(kwargs.get("top_n", 10) or 10)
        universe_size = int(kwargs.get("universe_size", 220) or 220)
        tolerance = float(kwargs.get("pullback_tolerance", 0.015) or 0.015)
        min_factor_score = float(kwargs.get("min_factor_score", 0.05) or 0.05)
        days = self._recent_trade_days(end_date, lookback_trade_days=max(40, lookback_days))
        panel, _, _ = self._prepare_db_priority_panel(end_date=end_date, use_days=days, universe_size=universe_size)
        latest = self._latest_feature_rows(panel)
        if latest.empty:
            return self._empty_candidates(top_n)

        latest = latest.copy()
        for col in ["close", "ma5", "ma10", "ma20", "ma5_bias", "momentum_20", "macd", "macd_prev"]:
            latest[col] = pd.to_numeric(latest[col], errors="coerce")
        latest["trend_ok"] = (latest["ma5"] >= latest["ma10"] * 0.995) & (latest["ma10"] >= latest["ma20"] * 0.99)
        latest["near_ma5"] = latest["ma5_bias"].abs() <= max(tolerance * 3, 0.05)
        latest["macd改善"] = latest["macd"] >= latest["macd_prev"]
        latest["pullback_score"] = (
            45.0 * (1.0 - (latest["ma5_bias"].abs() / max(tolerance * 3, 0.05)).clip(0, 1))
            + 25.0 * latest["trend_ok"].astype(float)
            + 20.0 * self._minmax_score(latest["momentum_20"]) / 100.0
            + 10.0 * latest["macd改善"].astype(float)
        )
        floor = max(0.0, min_factor_score * 100.0)
        ranked = latest[latest["pullback_score"] >= floor].copy()
        if ranked.empty:
            ranked = latest.copy()
        ranked["推荐评分"] = pd.to_numeric(ranked["pullback_score"], errors="coerce").round(2)
        ranked["建议仓位%"] = round(min(10.0, 100.0 / max(1, top_n)), 2)
        ranked["候选来源"] = "5日线回踩"
        ranked["最新收盘"] = pd.to_numeric(ranked["close"], errors="coerce").round(3)
        ranked["MA5"] = pd.to_numeric(ranked["ma5"], errors="coerce").round(3)
        ranked["MA10"] = pd.to_numeric(ranked["ma10"], errors="coerce").round(3)
        ranked["5日线偏离%"] = pd.to_numeric(ranked["ma5_bias"], errors="coerce")
        ranked["20日动量%"] = (pd.to_numeric(ranked["momentum_20"], errors="coerce") * 100.0).round(2)
        out_cols = [
            "ts_code",
            "name",
            "industry",
            "trade_date",
            "最新收盘",
            "MA5",
            "MA10",
            "5日线偏离%",
            "20日动量%",
            "推荐评分",
            "建议仓位%",
            "候选来源",
        ]
        out = ranked.sort_values(["推荐评分", "20日动量%"], ascending=[False, False]).head(top_n).reset_index(drop=True)
        for col in out_cols:
            if col not in out.columns:
                out[col] = ""
        return out[out_cols]

    def recommend_right_side_confirmation_candidates(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        return self._empty_candidates(int(kwargs.get("top_n", 0) or 0))

    def recommend_right_side_watch_candidates(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame()

    def recommend_bottom_rebound_candidates(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        end_date = str(kwargs.get("end_date") or pd.Timestamp.now().strftime("%Y%m%d"))
        lookback_days = int(kwargs.get("lookback_days", 90) or 90)
        top_n = int(kwargs.get("top_n", 20) or 20)
        universe_size = int(kwargs.get("universe_size", 220) or 220)
        days = self._recent_trade_days(end_date, lookback_trade_days=max(40, lookback_days))
        panel, _, _ = self._prepare_db_priority_panel(end_date=end_date, use_days=days, universe_size=universe_size)
        latest = self._latest_feature_rows(panel)
        if latest.empty:
            return self._empty_candidates(top_n)

        latest = latest.copy()
        for col in ["close", "momentum_5", "momentum_20", "range_20_pos", "ma5_bias"]:
            latest[col] = pd.to_numeric(latest[col], errors="coerce")
        latest["超跌幅度%"] = (pd.to_numeric(latest["momentum_20"], errors="coerce").clip(upper=0.0) * 100.0).round(2)
        latest["低位回升幅度%"] = (pd.to_numeric(latest["range_20_pos"], errors="coerce").clip(lower=0.0, upper=1.0) * 8.0).round(2)
        oversold = (-latest["momentum_20"]).clip(lower=0.0)
        rebound = latest["momentum_5"].clip(lower=0.0)
        near_ma = 1.0 - (latest["ma5_bias"].abs() / 0.08).clip(0.0, 1.0)
        latest["反弹确认分"] = (
            45.0 * self._minmax_score(oversold) / 100.0
            + 35.0 * self._minmax_score(rebound) / 100.0
            + 20.0 * near_ma
        ).round(2)
        latest["信号新鲜度"] = 80.0
        latest["板块轮动评分"] = 0.0
        latest["康波阶段"] = ""
        latest["轮动状态"] = "观察"
        latest["板块波浪阶段"] = "修复观察"
        latest["建仓提示"] = "适合建仓"
        latest["反弹形态"] = "超跌反弹"
        latest["候选来源"] = "抄底反弹候选"
        latest["买点评分"] = latest["反弹确认分"]
        latest["过热惩罚分"] = np.where(latest["momentum_5"] > 0.10, 25.0, 0.0)
        latest["连续下跌天数"] = np.where(latest["momentum_20"] < 0, 3.0, 0.0)
        latest["高位追涨标记"] = 0.0
        latest["建议仓位%"] = round(min(10.0, 100.0 / max(1, top_n)), 2)
        out_cols = [
            "ts_code",
            "name",
            "industry",
            "trade_date",
            "候选来源",
            "反弹形态",
            "反弹确认分",
            "超跌幅度%",
            "低位回升幅度%",
            "康波阶段",
            "轮动状态",
            "板块波浪阶段",
            "建仓提示",
            "建议仓位%",
            "买点评分",
            "过热惩罚分",
            "信号新鲜度",
            "板块轮动评分",
            "连续下跌天数",
            "高位追涨标记",
        ]
        out = latest.sort_values(["反弹确认分", "低位回升幅度%"], ascending=[False, False]).head(top_n).reset_index(drop=True)
        for col in out_cols:
            if col not in out.columns:
                out[col] = ""
        return out[out_cols]

    def recommend_bottom_rebound_watch_candidates(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
        kwargs = dict(kwargs)
        kwargs["top_n"] = int(kwargs.get("top_n", 20) or 20)
        out = self.recommend_bottom_rebound_candidates(*args, **kwargs)
        if out.empty:
            return out
        out = out.copy()
        out["候选来源"] = "副策略观察"
        return out
