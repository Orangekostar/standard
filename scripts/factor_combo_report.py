from __future__ import annotations

import argparse
import gc
import itertools
import os
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import settings
from core.backtest.metrics import annualized_return, max_drawdown, sharpe_ratio
from core.data.symbols import filter_non_chinext


FACTOR_ORDER = [
    "momentum_20",
    "reversal_5",
    "volatility_20",
    "rsi_14",
    "ma_bias_20",
    "breakout_20",
    "turnover_rate_5",
    "volume_surge_10",
    "price_volume_corr_10",
    "value_pe",
    "value_pb",
    "dividend_ratio",
    "small_cap",
    "alpha_6_turnover_cov",
    "alpha_9_reversal",
]

FACTOR_ZH = {
    "momentum_20": "20日动量",
    "reversal_5": "5日反转",
    "volatility_20": "20日波动",
    "rsi_14": "RSI(14)",
    "ma_bias_20": "20日均线乖离",
    "breakout_20": "20日突破",
    "turnover_rate_5": "5日换手率",
    "volume_surge_10": "10日放量",
    "price_volume_corr_10": "10日价量相关",
    "value_pe": "PE估值",
    "value_pb": "PB估值",
    "dividend_ratio": "股息率",
    "small_cap": "小市值",
    "alpha_6_turnover_cov": "Alpha#6 量价协方差",
    "alpha_9_reversal": "Alpha#9 价格反转",
}


def _load_panel(db_path: Path) -> pd.DataFrame:
    conn = sqlite3.connect(str(db_path))
    try:
        df = pd.read_sql_query(
            """
            SELECT
                d.ts_code,
                d.trade_date,
                d.open,
                d.high,
                d.low,
                d.close,
                d.vol,
                d.amount,
                d.turnover_rate,
                d.pe,
                d.pb,
                d.ps,
                d.dv_ratio,
                d.total_mv,
                COALESCE(b.name, '') AS name,
                COALESCE(b.industry, '') AS industry,
                COALESCE(b.market, '') AS market
            FROM daily_bars d
            LEFT JOIN stock_basic b
                ON d.ts_code = b.ts_code
            ORDER BY d.ts_code, d.trade_date
            """,
            conn,
        )
    finally:
        conn.close()

    if df is None or df.empty:
        return pd.DataFrame()

    df = filter_non_chinext(df)
    if df.empty:
        return pd.DataFrame()

    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    for col in ["open", "high", "low", "close", "vol", "amount", "turnover_rate", "pe", "pb", "ps", "dv_ratio", "total_mv"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float32")
    df["ts_code"] = df["ts_code"].astype("category")
    df["industry"] = df["industry"].astype("category")
    df["market"] = df["market"].astype("category")
    return df.dropna(subset=["ts_code", "trade_date", "close"]).sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def _past_only_zscore(series: pd.Series, by: pd.Series, window: int = 20) -> pd.Series:
    return series.groupby(by, group_keys=False).apply(
        lambda s: ((s - s.rolling(window, min_periods=5).mean().shift(1)) / (s.rolling(window, min_periods=5).std().shift(1) + 1e-12))
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0.0)
    )


def _cross_section_rank(series: pd.Series, dates: pd.Series) -> pd.Series:
    ranked = series.groupby(dates).rank(pct=True, method="average")
    return ranked.fillna(0.5)


def _compute_raw_factors(panel: pd.DataFrame) -> pd.DataFrame:
    df = panel.copy()
    grp = df.groupby("ts_code", group_keys=False)
    ret_1 = grp["close"].pct_change().fillna(0.0)
    ret_5 = grp["close"].pct_change(5)
    ret_20 = grp["close"].pct_change(20)
    ma_20 = grp["close"].transform(lambda s: s.rolling(20).mean())
    rolling_high_20 = grp["high"].transform(lambda s: s.rolling(20).max())
    rolling_low_20 = grp["low"].transform(lambda s: s.rolling(20).min())
    width_20 = (rolling_high_20 - rolling_low_20).replace(0.0, np.nan)
    vol_ma_10 = grp["vol"].transform(lambda s: s.rolling(10).mean())
    turnover_ma_5 = grp["turnover_rate"].transform(lambda s: s.rolling(5).mean()) if "turnover_rate" in df.columns else pd.Series(0.0, index=df.index)

    df["raw_momentum_20"] = ret_20.fillna(0.0)
    df["raw_reversal_5"] = (-ret_5).fillna(0.0)
    df["raw_volatility_20"] = -ret_1.groupby(df["ts_code"]).rolling(20).std().reset_index(level=0, drop=True).fillna(0.0)

    delta = grp["close"].diff().fillna(0.0)
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.groupby(df["ts_code"]).rolling(14).mean().reset_index(level=0, drop=True)
    avg_loss = loss.groupby(df["ts_code"]).rolling(14).mean().reset_index(level=0, drop=True)
    rs = avg_gain / (avg_loss + 1e-12)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    df["raw_rsi_14"] = ((rsi - 50.0) / 50.0).fillna(0.0)

    df["raw_ma_bias_20"] = ((df["close"] / (ma_20 + 1e-12)) - 1.0).fillna(0.0)
    df["raw_breakout_20"] = (((df["close"] - rolling_low_20) / width_20).fillna(0.5) - 0.5)
    df["raw_turnover_rate_5"] = turnover_ma_5.fillna(0.0)
    df["raw_volume_surge_10"] = ((df["vol"] / (vol_ma_10 + 1e-12)) - 1.0).fillna(0.0)
    df["raw_price_volume_corr_10"] = (
        ret_1.groupby(df["ts_code"]).rolling(10).corr(df["vol"].groupby(df["ts_code"]).pct_change().fillna(0.0)).reset_index(level=0, drop=True).fillna(0.0)
    )
    df["raw_value_pe"] = (-df["pe"].replace(0.0, np.nan)).fillna(0.0)
    df["raw_value_pb"] = (-df["pb"].replace(0.0, np.nan)).fillna(0.0)
    df["raw_dividend_ratio"] = df["dv_ratio"].fillna(0.0)
    df["raw_small_cap"] = (-df["total_mv"]).fillna(0.0)

    ret_lag1 = ret_1.groupby(df["ts_code"]).shift(1).fillna(0.0)
    corr20 = ret_1.groupby(df["ts_code"]).rolling(20).corr(ret_lag1).reset_index(level=0, drop=True).fillna(0.0)
    turnover_rank = _cross_section_rank(df["turnover_rate"].fillna(0.0), df["trade_date"])
    df["raw_alpha_6_turnover_cov"] = (-1.0 * corr20 * turnover_rank).fillna(0.0)

    ret5 = ret_5.fillna(0.0)
    ret5_rank = _cross_section_rank(ret5, df["trade_date"])
    df["raw_alpha_9_reversal"] = (ret5_rank * np.sign(ret5)).fillna(0.0)

    return df


def _cache_factor_scores(panel: pd.DataFrame, cache_dir: Path, progress_every: int = 1) -> dict[str, Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    df = _compute_raw_factors(panel)
    by = df["ts_code"]
    paths: dict[str, Path] = {}
    for idx, factor_name in enumerate(FACTOR_ORDER, start=1):
        raw_col = f"raw_{factor_name}"
        score = _past_only_zscore(df[raw_col].fillna(0.0).astype("float32"), by).astype("float32")
        path = cache_dir / f"{factor_name}.npy"
        np.save(path, score.to_numpy(dtype="float32"))
        paths[factor_name] = path
        if progress_every > 0 and (idx % progress_every == 0 or idx == len(FACTOR_ORDER)):
            print(f"[progress] factor_cache_done={idx}/{len(FACTOR_ORDER)} latest={factor_name}", flush=True)
        del score
        gc.collect()
    return paths


def _evaluate_combo(base_df: pd.DataFrame, score_paths: dict[str, Path], combo: tuple[str, ...], threshold: float, fee_rate: float) -> dict[str, float | int | str]:
    score_arrays = [np.load(score_paths[name], mmap_mode="r") for name in combo]
    factor_score = np.zeros(len(base_df), dtype="float32")
    for arr in score_arrays:
        factor_score += arr.astype("float32")
    factor_score /= float(len(score_arrays))

    work = base_df.copy()
    work["factor_score"] = factor_score
    work["signal"] = (work["factor_score"] > float(threshold)).astype(int)
    work["position"] = work.groupby("ts_code")["signal"].shift(1).fillna(0.0)
    work["turnover"] = work.groupby("ts_code")["position"].diff().abs().fillna(0.0)
    work["strategy_ret"] = work["position"] * work["ret"] - work["turnover"] * float(fee_rate)

    daily = (
        work.groupby("trade_date", as_index=False)
        .agg(
            strategy_ret=("strategy_ret", "mean"),
            benchmark_ret=("ret", "mean"),
            active_ratio=("position", "mean"),
            active_count=("position", "sum"),
            stock_count=("ts_code", "nunique"),
        )
        .sort_values("trade_date")
        .reset_index(drop=True)
    )
    daily["strategy_nav"] = (1 + daily["strategy_ret"]).cumprod()
    daily["benchmark_nav"] = (1 + daily["benchmark_ret"]).cumprod()

    total_return = float(daily["strategy_nav"].iloc[-1] - 1.0) if not daily.empty else 0.0
    benchmark_total_return = float(daily["benchmark_nav"].iloc[-1] - 1.0) if not daily.empty else 0.0
    return {
        "组合": " + ".join([FACTOR_ZH.get(name, name) for name in combo]),
        "组合键": ",".join(combo),
        "因子数": int(len(combo)),
        "收益率": total_return,
        "基准收益率": benchmark_total_return,
        "年化收益率": annualized_return(daily["strategy_ret"]),
        "夏普比率": sharpe_ratio(daily["strategy_ret"]),
        "最大回撤": max_drawdown(daily["strategy_nav"]),
        "平均持仓比例": float(daily["active_ratio"].mean()) if not daily.empty else 0.0,
        "平均持仓股票数": float(daily["active_count"].mean()) if not daily.empty else 0.0,
        "样本交易日": int(len(daily)),
    }


def build_combo_report(
    db_path: Path,
    min_combo_size: int = 1,
    max_combo_size: int = 2,
    threshold: float = 0.1,
    fee_rate: float = 0.0005,
    top_k: int = 20,
    progress_every: int = 10,
    sleep_seconds: float = 0.0,
    cache_dir: Path | None = None,
) -> pd.DataFrame:
    panel = _load_panel(db_path)
    if panel is None or panel.empty:
        return pd.DataFrame()
    print(f"[info] loaded panel rows={len(panel)} mem_mb={panel.memory_usage(deep=True).sum()/1024/1024:.2f}", flush=True)
    base_df = panel[["ts_code", "trade_date", "close"]].copy()
    base_df["ret"] = base_df.groupby("ts_code")["close"].pct_change().fillna(0.0).astype("float32")
    score_cache_dir = cache_dir or (db_path.parent / "factor_combo_cache")
    score_paths = _cache_factor_scores(panel, cache_dir=Path(score_cache_dir), progress_every=1)
    print(f"[info] factor scores cached factors={len(score_paths)} cache_dir={score_cache_dir}", flush=True)
    del panel
    gc.collect()

    combos: list[tuple[str, ...]] = []
    for r in range(max(1, int(min_combo_size)), max(1, int(max_combo_size)) + 1):
        combos.extend(list(itertools.combinations(FACTOR_ORDER, r)))
    print(f"[info] total_combos={len(combos)}", flush=True)

    rows = []
    started = time.time()
    for idx, combo in enumerate(combos, start=1):
        rows.append(_evaluate_combo(base_df, score_paths=score_paths, combo=combo, threshold=threshold, fee_rate=fee_rate))
        if progress_every > 0 and (idx % int(progress_every) == 0 or idx == len(combos)):
            elapsed = time.time() - started
            print(f"[progress] combos_done={idx}/{len(combos)} elapsed_s={elapsed:.1f}", flush=True)
        if sleep_seconds > 0:
            time.sleep(float(sleep_seconds))

    out = pd.DataFrame(rows).sort_values(["收益率", "年化收益率", "最大回撤"], ascending=[False, False, True]).reset_index(drop=True)
    if top_k > 0:
        out = out.head(int(top_k)).reset_index(drop=True)
    return out


def to_markdown_table(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "| 组合 | 收益率 | 最大回撤 |\n|---|---:|---:|\n| 无数据 | 0.00% | 0.00% |"

    show = df.copy()
    for col in ["收益率", "基准收益率", "年化收益率", "最大回撤", "平均持仓比例"]:
        if col in show.columns:
            show[col] = show[col].map(lambda x: f"{float(x):.2%}")
    for col in ["夏普比率", "平均持仓股票数"]:
        if col in show.columns:
            show[col] = show[col].map(lambda x: f"{float(x):.2f}")
    for col in ["因子数", "样本交易日"]:
        if col in show.columns:
            show[col] = show[col].map(lambda x: f"{int(x)}")
    show = show[["组合", "因子数", "收益率", "年化收益率", "最大回撤", "夏普比率", "平均持仓比例", "平均持仓股票数", "样本交易日"]]
    return show.to_markdown(index=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze factor combinations on the local A-share rolling database")
    parser.add_argument("--db-path", default=str(settings.market_db_path), help="Path to the SQLite market database")
    parser.add_argument("--min-combo-size", type=int, default=1, help="Minimum number of factors in a combination")
    parser.add_argument("--max-combo-size", type=int, default=2, help="Maximum number of factors in a combination")
    parser.add_argument("--threshold", type=float, default=0.1, help="Signal threshold")
    parser.add_argument("--fee-rate", type=float, default=0.0005, help="Per-turnover fee rate")
    parser.add_argument("--top-k", type=int, default=20, help="Number of top combinations to keep")
    parser.add_argument("--progress-every", type=int, default=10, help="Progress log interval")
    parser.add_argument("--sleep-seconds", type=float, default=0.0, help="Optional sleep between combos to reduce contention")
    parser.add_argument("--cache-dir", default="", help="Directory to store cached single-factor scores")
    parser.add_argument("--output", default="", help="Optional markdown output file")
    args = parser.parse_args()

    report_df = build_combo_report(
        db_path=Path(args.db_path),
        min_combo_size=int(args.min_combo_size),
        max_combo_size=int(args.max_combo_size),
        threshold=float(args.threshold),
        fee_rate=float(args.fee_rate),
        top_k=int(args.top_k),
        progress_every=int(args.progress_every),
        sleep_seconds=float(args.sleep_seconds),
        cache_dir=Path(args.cache_dir) if str(args.cache_dir or "").strip() else None,
    )
    markdown = to_markdown_table(report_df)
    print(markdown)
    if str(args.output or "").strip():
        Path(args.output).write_text(markdown + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
