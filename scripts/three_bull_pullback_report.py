from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import settings
from core.data.symbols import filter_non_chinext, filter_non_risk_warning
from core.strategies.three_bull_pullback import (
    ThreeBullPullbackConfig,
    append_forward_returns,
    build_three_bull_pullback_signals,
    rolling_backtest_summary,
    summarize_score_bins,
    summarize_three_bull_backtest,
)


def load_market_panel(db_path: Path, start_date: str = "", end_date: str = "") -> pd.DataFrame:
    where = []
    params: list[str] = []
    if start_date:
        where.append("d.trade_date >= ?")
        params.append(start_date)
    if end_date:
        where.append("d.trade_date <= ?")
        params.append(end_date)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    query = f"""
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
            d.total_mv,
            COALESCE(b.name, '') AS name,
            COALESCE(b.industry, '') AS industry,
            COALESCE(b.market, '') AS market
        FROM daily_bars d
        LEFT JOIN stock_basic b
            ON d.ts_code = b.ts_code
        {where_sql}
        ORDER BY d.ts_code, d.trade_date
    """
    conn = sqlite3.connect(str(db_path))
    try:
        df = pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()
    if df is None or df.empty:
        return pd.DataFrame()
    df = filter_non_chinext(df)
    df = filter_non_risk_warning(df)
    if df.empty:
        return pd.DataFrame()
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    return df.dropna(subset=["ts_code", "trade_date", "close"]).reset_index(drop=True)


def _format_report_table(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        if col.endswith("胜率") or col.endswith("收益") or col.endswith("达标率") or col.endswith("可控率"):
            out[col] = out[col].map(lambda v: "" if pd.isna(v) else f"{float(v):.2%}")
        elif col in {"平均评分", "日均候选数"}:
            out[col] = out[col].map(lambda v: "" if pd.isna(v) else f"{float(v):.2f}")
    return out


def build_markdown_report(
    summary_df: pd.DataFrame,
    score_bins_df: pd.DataFrame,
    rolling_df: pd.DataFrame,
    latest_top_df: pd.DataFrame,
    signal_count: int,
    trade_count: int,
    date_min: str,
    date_max: str,
) -> str:
    lines = [
        "# 三阳回踩 + 催化强化策略滚动回测",
        "",
        f"- 回测区间：{date_min} 至 {date_max}",
        f"- 原始信号数：{signal_count}",
        f"- 可交易样本数：{trade_count}",
        "",
        "## TopN 汇总",
        "",
        _format_report_table(summary_df).to_markdown(index=False) if not summary_df.empty else "无数据",
        "",
        "## 评分分组",
        "",
        _format_report_table(score_bins_df).to_markdown(index=False) if not score_bins_df.empty else "无数据",
        "",
        "## 滚动窗口",
        "",
        _format_report_table(rolling_df.tail(12)).to_markdown(index=False) if not rolling_df.empty else "无数据",
        "",
        "## 最新 Top20 候选",
        "",
        latest_top_df.to_markdown(index=False) if not latest_top_df.empty else "无数据",
        "",
    ]
    return "\n".join(lines)


def latest_top_candidates(signals: pd.DataFrame, top_n: int = 20) -> pd.DataFrame:
    if signals is None or signals.empty:
        return pd.DataFrame()
    work = signals.copy()
    work["trade_date"] = pd.to_datetime(work["trade_date"], errors="coerce")
    latest_date = work["trade_date"].max()
    if pd.isna(latest_date):
        return pd.DataFrame()
    latest = work[work["trade_date"].eq(latest_date)].copy()
    if latest.empty:
        return pd.DataFrame()
    if "候选优先级" not in latest.columns:
        latest["候选优先级"] = latest["候选来源"].astype(str).eq("右侧确认候选").astype(int)
    latest = latest.sort_values(
        ["候选优先级", "策略评分", "执行确认分"],
        ascending=[False, False, False],
    ).head(int(top_n))
    show_cols = [
        "trade_date",
        "ts_code",
        "name",
        "industry",
        "交易动作",
        "候选来源",
        "策略评分",
        "当日排名",
        "pct_chg",
        "量能比",
        "市场环境分",
        "板块强度分",
        "题材催化分",
        "执行确认分",
        "回踩天数",
        "回踩幅度%",
        "买点检查",
        "入选说明",
    ]
    show_cols = [col for col in show_cols if col in latest.columns]
    out = latest[show_cols].copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return out.reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest three-bull pullback plus catalyst ranking on the local market DB")
    parser.add_argument("--db-path", default=str(settings.market_db_path), help="SQLite market DB path")
    parser.add_argument("--start-date", default="", help="Inclusive start date, YYYY-MM-DD")
    parser.add_argument("--end-date", default="", help="Inclusive end date, YYYY-MM-DD")
    parser.add_argument("--warmup-calendar-days", type=int, default=180, help="Extra calendar days loaded before start-date for rolling features")
    parser.add_argument("--min-score", type=float, default=60.0, help="Minimum strategy score for raw signals")
    parser.add_argument("--top-n", default="3,5,10", help="Comma-separated TopN groups")
    parser.add_argument("--horizons", default="1,3,5", help="Comma-separated forward horizons")
    parser.add_argument("--horizon-days", type=int, default=3, help="Horizon used for summary hit rates")
    parser.add_argument("--target-gain-pct", type=float, default=3.0, help="Intraperiod high hit threshold")
    parser.add_argument("--max-drawdown-pct", type=float, default=3.0, help="Allowed intraperiod drawdown")
    parser.add_argument("--fee-rate", type=float, default=0.0005, help="One-way fee/slippage rate")
    parser.add_argument("--rolling-window-days", type=int, default=20, help="Rolling window length in signal days")
    parser.add_argument("--rolling-step-days", type=int, default=5, help="Rolling window step in signal days")
    parser.add_argument("--output-dir", default="cache/backtests/three_bull_pullback", help="Directory for CSV and markdown outputs")
    parser.add_argument("--latest-top-n", type=int, default=20, help="Latest candidates to export")
    parser.add_argument("--include-watch", action="store_true", help="Also include observation candidates in backtest outputs")
    parser.add_argument("--no-watch", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--min-confirm-volume", type=float, default=1.20, help="Minimum amount/20-day average for confirmed buy points")
    parser.add_argument("--min-confirm-gain-pct", type=float, default=1.5, help="Minimum daily gain for confirmed buy points")
    parser.add_argument("--max-confirm-gain-pct", type=float, default=7.5, help="Maximum daily gain for confirmed buy points")
    parser.add_argument("--min-market-score", type=float, default=0.0, help="Optional hard floor for market environment score")
    parser.add_argument("--min-sector-score", type=float, default=0.0, help="Optional hard floor for sector strength score")
    args = parser.parse_args()

    db_path = Path(args.db_path)
    top_n_values = [int(x) for x in str(args.top_n).split(",") if str(x).strip()]
    horizons = [int(x) for x in str(args.horizons).split(",") if str(x).strip()]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    requested_start = str(args.start_date or "").strip()
    load_start = requested_start
    if requested_start:
        start_ts = pd.to_datetime(requested_start, errors="coerce")
        if pd.notna(start_ts):
            load_start = (start_ts - pd.Timedelta(days=max(0, int(args.warmup_calendar_days)))).strftime("%Y-%m-%d")
    panel = load_market_panel(db_path=db_path, start_date=load_start, end_date=str(args.end_date or ""))
    if panel.empty:
        raise SystemExit(f"No market data found in {db_path}")

    include_watch = bool(args.include_watch) and not bool(args.no_watch)
    config = ThreeBullPullbackConfig(
        min_score=float(args.min_score),
        include_watch=include_watch,
        min_confirm_volume_ratio=float(args.min_confirm_volume),
        min_confirm_gain=float(args.min_confirm_gain_pct) / 100.0,
        max_confirm_gain=float(args.max_confirm_gain_pct) / 100.0,
        min_market_score=float(args.min_market_score),
        min_sector_score=float(args.min_sector_score),
    )
    print(f"[info] loaded rows={len(panel)} stocks={panel['ts_code'].nunique()} dates={panel['trade_date'].nunique()}", flush=True)
    signals = build_three_bull_pullback_signals(panel, config=config)
    latest_config = ThreeBullPullbackConfig(
        min_score=float(args.min_score),
        include_watch=True,
        min_confirm_volume_ratio=float(args.min_confirm_volume),
        min_confirm_gain=float(args.min_confirm_gain_pct) / 100.0,
        max_confirm_gain=float(args.max_confirm_gain_pct) / 100.0,
        min_market_score=float(args.min_market_score),
        min_sector_score=float(args.min_sector_score),
    )
    latest_signals = signals if include_watch else build_three_bull_pullback_signals(panel, config=latest_config)
    if requested_start and not signals.empty:
        signal_start_ts = pd.to_datetime(requested_start, errors="coerce")
        if pd.notna(signal_start_ts):
            signals = signals[signals["trade_date"] >= signal_start_ts].reset_index(drop=True)
            if latest_signals is not None and not latest_signals.empty:
                latest_signals = latest_signals[latest_signals["trade_date"] >= signal_start_ts].reset_index(drop=True)
    trades = append_forward_returns(signals, panel, horizons=horizons, fee_rate=float(args.fee_rate))
    latest_top_df = latest_top_candidates(latest_signals, top_n=int(args.latest_top_n))
    latest_buy_df = latest_top_candidates(signals, top_n=int(args.latest_top_n))
    summary_df = summarize_three_bull_backtest(
        trades,
        top_n_values=top_n_values,
        target_gain_pct=float(args.target_gain_pct),
        max_drawdown_pct=float(args.max_drawdown_pct),
        horizon_days=int(args.horizon_days),
    )
    score_bins_df = summarize_score_bins(
        trades,
        target_gain_pct=float(args.target_gain_pct),
        max_drawdown_pct=float(args.max_drawdown_pct),
        horizon_days=int(args.horizon_days),
    )
    rolling_df = rolling_backtest_summary(
        trades,
        top_n=max(top_n_values) if top_n_values else 5,
        window_days=int(args.rolling_window_days),
        step_days=int(args.rolling_step_days),
    )

    signals.to_csv(output_dir / "signals.csv", index=False)
    trades.to_csv(output_dir / "trades.csv", index=False)
    latest_top_df.to_csv(output_dir / "latest_top20.csv", index=False)
    latest_buy_df.to_csv(output_dir / "latest_buy_pool.csv", index=False)
    summary_df.to_csv(output_dir / "summary.csv", index=False)
    score_bins_df.to_csv(output_dir / "score_bins.csv", index=False)
    rolling_df.to_csv(output_dir / "rolling.csv", index=False)

    date_min = pd.to_datetime(panel["trade_date"], errors="coerce").min().strftime("%Y-%m-%d")
    date_max = pd.to_datetime(panel["trade_date"], errors="coerce").max().strftime("%Y-%m-%d")
    markdown = build_markdown_report(
        summary_df=summary_df,
        score_bins_df=score_bins_df,
        rolling_df=rolling_df,
        latest_top_df=latest_top_df,
        signal_count=int(len(signals)),
        trade_count=int(len(trades)),
        date_min=date_min,
        date_max=date_max,
    )
    report_path = output_dir / "report.md"
    report_path.write_text(markdown + "\n", encoding="utf-8")
    print(markdown)
    print(f"[info] wrote outputs to {output_dir}", flush=True)


if __name__ == "__main__":
    main()
