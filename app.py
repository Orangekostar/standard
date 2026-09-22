from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
from io import StringIO
from datetime import date, timedelta
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import streamlit as st

from config import settings
from core.background.task_rules import (
    latest_success_timestamp,
    should_hide_legacy_timeout_error,
    snapshot_is_successful,
)
from core.background.snapshot_store import (
    clear_request,
    has_pending_request,
    read_request,
    read_snapshot,
    read_task_progress_log,
    read_task_status,
    read_worker_status,
    request_refresh,
)
from core.backtest.engine import BacktestEngine
from core.data.data_manager import DataManager
from core.data.symbols import filter_non_chinext, filter_non_risk_warning, normalize_ts_code
from core.pipeline.runner import build_strategies
from core.strategies.factor_selection import FactorSelectionStrategy


_BACKTEST_BUTTON_RENDER_COUNTER = itertools.count()


WATCHLIST_FILE = settings.cache_dir / "watchlist.json"
HOLDINGS_FILE = settings.cache_dir / "holdings.json"
HOLDING_USERS_FILE = settings.cache_dir / "holding_users.json"
HOLDING_QUOTE_REFRESH_SECONDS = 120
HOLDING_FORCE_REFRESH_INTERVAL_SECONDS = 120
THREE_BULL_BACKTEST_DIR = settings.cache_dir / "backtests" / "three_bull_pullback"
THREE_BULL_BACKTEST_TRADES_FILE = THREE_BULL_BACKTEST_DIR / "trades.csv"
THREE_BULL_LATEST_TOP20_FILE = THREE_BULL_BACKTEST_DIR / "latest_top20.csv"
THREE_BULL_SUMMARY_FILE = THREE_BULL_BACKTEST_DIR / "summary.csv"
THREE_BULL_SCORE_BINS_FILE = THREE_BULL_BACKTEST_DIR / "score_bins.csv"
SECTOR_FUND_FLOW_AUTO_REFRESH_SECONDS = 180
SMART_PICK_AUTO_REFRESH_SECONDS = 180
DRAGON_RADAR_AUTO_REFRESH_SECONDS = 180
MA5_PULLBACK_AUTO_REFRESH_SECONDS = 180
AUTO_REFRESH_SCHEDULE_HOURS: tuple[int, ...] = (9, 16, 20)
AUTO_REFRESH_SCHEDULE_LABEL = "09:00 / 16:00 / 20:00"
SCHEDULED_REFRESH_STALE_SECONDS = 10 * 3600
WORKER_HEARTBEAT_STALE_SECONDS = 90
DEFAULT_GROUPS = {
    "默认": ["000001.SZ", "600519.SH", "600986.SH"],
    "短线": [],
    "中线": [],
}

TECHNICAL_FACTORS = [
    "momentum_20",
    "reversal_5",
    "volatility_20",
    "rsi_14",
    "ma_bias_20",
    "breakout_20",
    "alpha_9_reversal",
]
VOLUME_PRICE_FACTORS = [
    "turnover_rate_5",
    "volume_surge_10",
    "price_volume_corr_10",
    "alpha_6_turnover_cov",
]
FUNDAMENTAL_FACTORS = [
    "value_pe",
    "value_pb",
    "dividend_ratio",
    "small_cap",
]
ALL_FACTORS = TECHNICAL_FACTORS + VOLUME_PRICE_FACTORS + FUNDAMENTAL_FACTORS

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
    "alpha_6_turnover_cov": "Alpha#6量价协方差",
    "alpha_9_reversal": "Alpha#9价格反转",
    "value_pe": "PE估值",
    "value_pb": "PB估值",
    "dividend_ratio": "股息率",
    "small_cap": "小市值",
}

PROFESSIONAL_PLAYBOOKS = {
    "职业操作1（趋势+估值共振）": {
        "summary": "先看中期趋势，再叠加低估值过滤，偏中线配置。",
        "threshold": 0.08,
        "factors": ["momentum_20", "breakout_20", "value_pe", "value_pb", "dividend_ratio"],
        "weights": {
            "momentum_20": 1.0,
            "breakout_20": 0.8,
            "value_pe": 0.7,
            "value_pb": 0.7,
            "dividend_ratio": 0.4,
        },
    },
    "职业操作2（资金驱动+事件催化）": {
        "summary": "重视放量与价量联动，强调事件触发后的顺势跟随。",
        "threshold": 0.12,
        "factors": ["volume_surge_10", "turnover_rate_5", "price_volume_corr_10", "breakout_20", "momentum_20"],
        "weights": {
            "volume_surge_10": 1.0,
            "turnover_rate_5": 0.8,
            "price_volume_corr_10": 0.9,
            "breakout_20": 0.7,
            "momentum_20": 0.5,
        },
    },
    "职业操作3（低估反转防守）": {
        "summary": "偏防守风格，侧重估值修复与超跌反转，抑制高波动标的。",
        "threshold": 0.03,
        "factors": ["reversal_5", "alpha_9_reversal", "value_pe", "value_pb", "dividend_ratio"],
        "weights": {
            "reversal_5": 0.7,
            "alpha_9_reversal": 1.0,
            "value_pe": 0.8,
            "value_pb": 0.8,
            "dividend_ratio": 0.5,
            "volatility_20": -0.7,
        },
    },
    "职业操作4（波段趋势轮动）": {
        "summary": "趋势与均线乖离结合，适配波段轮动与仓位动态调整。",
        "threshold": 0.10,
        "factors": ["momentum_20", "ma_bias_20", "breakout_20", "turnover_rate_5", "volatility_20"],
        "weights": {
            "momentum_20": 1.0,
            "ma_bias_20": 0.7,
            "breakout_20": 0.8,
            "turnover_rate_5": 0.4,
            "volatility_20": -0.5,
        },
    },
}


def _is_cn_trading_session(now_ts: pd.Timestamp) -> bool:
    minute_of_day = now_ts.hour * 60 + now_ts.minute
    morning = 9 * 60 + 30 <= minute_of_day <= 11 * 60 + 30
    afternoon = 13 * 60 <= minute_of_day <= 15 * 60
    return bool(morning or afternoon)


def _latest_scheduled_refresh_slot(now_ts: pd.Timestamp) -> pd.Timestamp:
    day_start = now_ts.normalize()
    valid_hours = sorted({int(h) for h in AUTO_REFRESH_SCHEDULE_HOURS if 0 <= int(h) <= 23})
    if not valid_hours:
        return day_start

    for hour in reversed(valid_hours):
        slot = day_start + pd.Timedelta(hours=hour)
        if slot <= now_ts:
            return slot

    prev_day = day_start - pd.Timedelta(days=1)
    return prev_day + pd.Timedelta(hours=valid_hours[-1])


def _build_auto_refresh_tag(now_ts: pd.Timestamp, refresh_seconds: int | None = None) -> str:
    slot = _latest_scheduled_refresh_slot(now_ts)
    return f"slot-{slot.strftime('%Y%m%d-%H')}"


def _snapshot_covers_refresh_slot(snapshot: dict | None, now_ts: pd.Timestamp) -> bool:
    if not isinstance(snapshot, dict):
        return False
    slot = _latest_scheduled_refresh_slot(now_ts)
    generated_at = pd.to_datetime(snapshot.get("generated_at"), errors="coerce")
    if pd.isna(generated_at):
        return False
    return bool(generated_at >= slot)


def _resolve_realtime_end_date(selected_end_dt: date, now_ts: pd.Timestamp) -> str:
    today = now_ts.date()
    effective = selected_end_dt if selected_end_dt <= today else today
    return effective.strftime("%Y%m%d")


def _snapshot_payload_df(snapshot: dict, key: str) -> pd.DataFrame:
    if not isinstance(snapshot, dict):
        return pd.DataFrame()
    payload = snapshot.get("payload")
    if not isinstance(payload, dict):
        return pd.DataFrame()
    value = payload.get(key)
    if isinstance(value, pd.DataFrame):
        return value
    return pd.DataFrame()


def _snapshot_payload_dict(snapshot: dict, key: str) -> dict:
    if not isinstance(snapshot, dict):
        return {}
    payload = snapshot.get("payload")
    if not isinstance(payload, dict):
        return {}
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _snapshot_payload_df_dict(snapshot: dict, key: str) -> dict[str, pd.DataFrame]:
    value = _snapshot_payload_dict(snapshot, key)
    out: dict[str, pd.DataFrame] = {}
    for item_key, item_value in value.items():
        if isinstance(item_value, pd.DataFrame):
            out[str(item_key)] = item_value
    return out


def _snapshot_age_seconds(snapshot: dict) -> float | None:
    if not isinstance(snapshot, dict):
        return None
    generated_at = snapshot.get("generated_at")
    if not generated_at:
        return None
    ts = pd.to_datetime(generated_at, errors="coerce")
    if pd.isna(ts):
        return None
    return float((pd.Timestamp.now() - ts).total_seconds())


def _is_snapshot_stale(snapshot: dict, stale_seconds: int) -> bool:
    age = _snapshot_age_seconds(snapshot)
    if age is None:
        return True
    return age > max(30, int(stale_seconds))


def _request_age_seconds(request_data: dict | None) -> float | None:
    if not isinstance(request_data, dict):
        return None
    requested_at = request_data.get("requested_at")
    if not requested_at:
        return None
    ts = pd.to_datetime(requested_at, errors="coerce")
    if pd.isna(ts):
        return None
    return float((pd.Timestamp.now() - ts).total_seconds())


def _worker_status_age_seconds(worker_status: dict | None) -> float | None:
    if not isinstance(worker_status, dict):
        return None
    updated_at = worker_status.get("updated_at")
    if not updated_at:
        return None
    ts = pd.to_datetime(updated_at, errors="coerce")
    if pd.isna(ts):
        return None
    return float((pd.Timestamp.now() - ts).total_seconds())


def _is_worker_healthy(worker_status: dict | None, stale_seconds: int = WORKER_HEARTBEAT_STALE_SECONDS) -> bool:
    age = _worker_status_age_seconds(worker_status)
    if age is None:
        return False
    return age <= max(30, int(stale_seconds))


def _task_status_age_seconds(task_status: dict | None) -> float | None:
    if not isinstance(task_status, dict):
        return None
    updated_at = task_status.get("updated_at")
    if not updated_at:
        return None
    ts = pd.to_datetime(updated_at, errors="coerce")
    if pd.isna(ts):
        return None
    return float((pd.Timestamp.now() - ts).total_seconds())


def _text_progress_bar(progress_pct: int | float, width: int = 18) -> str:
    try:
        pct = int(float(progress_pct))
    except Exception:
        pct = 0
    pct = max(0, min(100, pct))
    width = max(10, int(width or 0))
    filled = int(round((pct / 100.0) * width))
    filled = max(0, min(width, filled))
    return f"[{'#' * filled}{'-' * (width - filled)}] {pct}%"


def _resolve_task_progress(status: str, task_extra: dict[str, Any]) -> tuple[int, str, str]:
    timeout_seconds = int(task_extra.get("timeout_seconds", 0) or 0)
    elapsed_seconds = int(task_extra.get("elapsed_seconds", task_extra.get("task_elapsed_seconds", 0)) or 0)

    progress_pct_raw = task_extra.get("progress_pct")
    if progress_pct_raw is None:
        if status in {"ok", "error"}:
            progress_pct_raw = 100
        elif status == "running" and timeout_seconds > 0:
            progress_pct_raw = min(95, int((elapsed_seconds / max(1, timeout_seconds)) * 100))
        else:
            progress_pct_raw = 0

    try:
        progress_pct = int(float(progress_pct_raw))
    except Exception:
        progress_pct = 0
    progress_pct = max(0, min(100, progress_pct))

    progress_bar = str(task_extra.get("progress_bar", "") or _text_progress_bar(progress_pct))
    progress_message = str(task_extra.get("progress_message", "") or "")
    return progress_pct, progress_bar, progress_message


def _is_task_running(task_status: dict | None, stale_seconds: int = WORKER_HEARTBEAT_STALE_SECONDS) -> bool:
    if not isinstance(task_status, dict):
        return False
    if str(task_status.get("status", "") or "") != "running":
        return False
    extra = task_status.get("extra", {}) if isinstance(task_status, dict) else {}
    child_pid = int((extra or {}).get("child_pid", 0) or 0)
    worker_pid = int((extra or {}).get("pid", 0) or 0)
    candidate_pids = [pid for pid in [child_pid, worker_pid] if pid > 0]
    if candidate_pids and not any(Path(f"/proc/{pid}").exists() for pid in candidate_pids):
        return False
    age = _task_status_age_seconds(task_status)
    if age is None:
        return False
    return age <= max(30, int(stale_seconds))


def _request_newer_than_snapshot(request_data: dict | None, snapshot: dict | None) -> bool:
    if not isinstance(request_data, dict):
        return False
    req_ts = pd.to_datetime(request_data.get("requested_at"), errors="coerce")
    snap_ts = pd.to_datetime((snapshot or {}).get("generated_at"), errors="coerce")
    if pd.isna(req_ts):
        return False
    if pd.isna(snap_ts):
        return True
    return bool(req_ts > snap_ts)


def _background_status_message(task_name: str, label: str, snapshot: dict | None, stale_seconds: int) -> str | None:
    request_data = read_request(task_name)
    task_status = read_task_status(task_name)
    worker_status = read_worker_status()
    worker_healthy = _is_worker_healthy(worker_status)
    task_running = _is_task_running(task_status)
    request_age = _request_age_seconds(request_data)
    task_extra = task_status.get("extra", {}) if isinstance(task_status, dict) else {}
    elapsed = int(task_extra.get("elapsed_seconds", task_extra.get("task_elapsed_seconds", 0)) or 0)

    if isinstance(request_data, dict):
        if not _request_newer_than_snapshot(request_data, snapshot):
            clear_request(task_name)
            request_data = None
            request_age = None
        elif request_age is not None and request_age > max(120, int(stale_seconds)):
            clear_request(task_name)
            if task_running or worker_healthy:
                return f"{label}后台请求已超时并已清理（Worker 未在有效时间内完成该任务）。"
            return f"{label}后台请求已超时并已清理，Worker 心跳异常，请重启后台进程。"

    if isinstance(request_data, dict):
        if task_running:
            suffix = f"（已运行 {elapsed}s）" if elapsed > 0 else ""
            return f"{label}后台任务处理中，请稍后自动刷新查看。{suffix}"
        if worker_healthy:
            return f"{label}后台任务已排队，等待后台 Worker 调度。"
        return f"{label}后台任务仍在排队，但 Worker 心跳异常，请检查后台进程。"

    if _is_snapshot_stale(snapshot or {}, stale_seconds):
        if task_running:
            suffix = f"（已运行 {elapsed}s）" if elapsed > 0 else ""
            return f"{label}后台任务处理中，请稍后自动刷新查看。{suffix}"
        if worker_healthy:
            return f"{label}快照较旧，等待后台 Worker 下一轮刷新。"
        return f"{label}快照较旧，后台 Worker 心跳异常，请重启后台进程。"

    if isinstance(task_status, dict) and str(task_status.get("status", "") or "") == "error":
        if should_hide_legacy_timeout_error(
            task_status,
            worker_status,
            worker_stale_seconds=WORKER_HEARTBEAT_STALE_SECONDS,
        ):
            if snapshot_is_successful(snapshot):
                return None
            return f"{label}上次后台刷新超时，旧失败状态已忽略，等待后台 Worker 下一轮刷新。"
        err = str(task_status.get("error", "") or "")
        if err:
            return f"{label}后台任务失败：{err}"
    return None


def _can_auto_enqueue(task_name: str, stale_seconds: int = WORKER_HEARTBEAT_STALE_SECONDS) -> bool:
    request_data = read_request(task_name)
    if isinstance(request_data, dict):
        return False
    task_status = read_task_status(task_name)
    if _is_task_running(task_status, stale_seconds=stale_seconds):
        return False
    return True


def _enqueue_manual_refresh_once(
    *,
    task_name: str,
    label: str,
    payload: dict[str, Any],
    queued_message: str,
    stale_seconds: int = WORKER_HEARTBEAT_STALE_SECONDS,
) -> str:
    request_data = read_request(task_name)
    if isinstance(request_data, dict):
        return f"{label}后台刷新请求已在队列中，本次未重复提交。"
    task_status = read_task_status(task_name)
    if _is_task_running(task_status, stale_seconds=stale_seconds):
        return f"{label}后台任务正在执行中，本次未重复排队。"
    request_refresh(task_name, payload=payload, force=True)
    return queued_message


def _background_status_panel() -> tuple[dict, pd.DataFrame]:
    worker_status = read_worker_status() or {}
    task_names = ["market_db_sync", "sector_fund_flow", "smart_pick", "dragon_radar", "ma5_pullback", "three_bull_pullback", "today_entry", "rebound_entry", "holding_advice", "cache_cleanup"]
    label_map = {
        "market_db_sync": "全市场60日数据库",
        "sector_fund_flow": "板块资金流",
        "smart_pick": "科学选股",
        "dragon_radar": "龙头雷达",
        "ma5_pullback": "5日线回踩",
        "three_bull_pullback": "三阳回踩策略",
        "today_entry": "当日建仓",
        "rebound_entry": "副策略建仓",
        "holding_advice": "持仓建议",
        "cache_cleanup": "缓存清理",
    }
    source_map = {
        "market_db_sync": "写入 rolling 60T market_db",
        "sector_fund_flow": "AkShare/Eastmoney，失败回退量价代理",
        "smart_pick": "market_db优先，远端补充",
        "dragon_radar": "market_db优先，消息/财报/分钟补充",
        "ma5_pullback": "market_db优先，远端兜底",
        "three_bull_pullback": "rolling market_db + 本地滚动回测",
        "today_entry": "依赖 smart_pick 快照",
        "rebound_entry": "rolling market_db + 板块轮动修复",
        "holding_advice": "持仓+实时行情/日线",
        "cache_cleanup": "本地缓存分层清理",
    }
    rows: list[dict] = []
    for task_name in task_names:
        task_status = read_task_status(task_name) or {}
        snapshot = read_snapshot(task_name) or {}
        request_data = read_request(task_name) or {}
        status = str(task_status.get("status", "") or "")
        task_running = _is_task_running(task_status)
        if status == "running" and (not task_running):
            if request_data:
                status = "queued"
            elif snapshot:
                status = str(snapshot.get("status", "") or "idle")
            else:
                status = "idle"
        elif not status:
            if request_data:
                status = "queued"
            elif snapshot:
                status = str(snapshot.get("status", "") or "unknown")
            else:
                status = "idle"

        task_extra = task_status.get("extra", {}) if isinstance(task_status, dict) else {}
        payload = snapshot.get("payload", {}) if isinstance(snapshot, dict) else {}
        record_count = 0
        extra_note = ""
        for value in payload.values():
            if isinstance(value, pd.DataFrame):
                record_count = max(record_count, int(len(value)))
        if task_name == "market_db_sync" and isinstance(payload.get("market_db_status"), dict):
            db_status = payload.get("market_db_status") or {}
            record_count = int(db_status.get("row_count", 0) or 0)
            extra_note = f"股票数={int(db_status.get('stock_count', 0) or 0)}"
        duration_seconds = int(
            task_extra.get(
                "duration_seconds",
                task_extra.get("elapsed_seconds", task_extra.get("task_elapsed_seconds", 0)),
            )
            or 0
        )
        legacy_timeout_error = should_hide_legacy_timeout_error(
            task_status,
            worker_status,
            worker_stale_seconds=WORKER_HEARTBEAT_STALE_SECONDS,
        )
        progress_task_extra = dict(task_extra)
        if legacy_timeout_error:
            if request_data:
                status = "queued"
            elif snapshot_is_successful(snapshot):
                status = "ok"
            else:
                status = "idle"
            duration_seconds = 0
            progress_task_extra = {}
            if status == "queued":
                progress_task_extra["progress_message"] = "等待后台 Worker 调度"
            elif status == "ok":
                progress_task_extra["progress_pct"] = 100
                progress_task_extra["progress_message"] = "沿用上次成功快照"
            else:
                progress_task_extra["progress_message"] = "等待后台 Worker 下一轮刷新"
        progress_pct, progress_bar, progress_message = _resolve_task_progress(status=status, task_extra=progress_task_extra)
        task_error = str(task_status.get("error", "") or "")
        snapshot_error = str(snapshot.get("error", "") or "")
        if legacy_timeout_error:
            task_error = ""
            snapshot_error = ""
        # Avoid showing stale snapshot errors while a newer run is queued/running.
        effective_error = task_error if status in {"queued", "running", "starting"} else (task_error or snapshot_error)
        rows.append(
            {
                "任务": label_map.get(task_name, task_name),
                "任务键": task_name,
                "状态": status,
                "进度": progress_bar,
                "进度值%": progress_pct,
                "进度说明": progress_message,
                "耗时秒数": duration_seconds,
                "记录数": record_count,
                "附加信息": extra_note,
                "数据源": source_map.get(task_name, ""),
                "最近完成": str(task_extra.get("finished_at", "") or snapshot.get("generated_at", "") or ""),
                "最近请求": str(request_data.get("requested_at", "") or ""),
                "错误": effective_error,
            }
        )

    worker_summary = {
        "状态": str(worker_status.get("status", "") or "未知"),
        "当前任务": str(worker_status.get("current_task", "") or "-"),
        "心跳时间": str(worker_status.get("updated_at", "") or ""),
        "并发数": int(((worker_status.get("extra") or {}) if isinstance(worker_status, dict) else {}).get("running_count", 0) or 0),
        "最大并发": int(((worker_status.get("extra") or {}) if isinstance(worker_status, dict) else {}).get("max_concurrency", 0) or 0),
        "健康": "正常" if _is_worker_healthy(worker_status) else "异常",
    }
    return worker_summary, pd.DataFrame(rows)


def _background_progress_log_panel(limit: int = 80) -> pd.DataFrame:
    label_map = {
        "market_db_sync": "全市场60日数据库",
        "sector_fund_flow": "板块资金流",
        "smart_pick": "科学选股",
        "dragon_radar": "龙头雷达",
        "ma5_pullback": "5日线回踩",
        "three_bull_pullback": "三阳回踩策略",
        "today_entry": "当日建仓",
        "rebound_entry": "副策略建仓",
        "holding_advice": "持仓建议",
        "cache_cleanup": "缓存清理",
    }
    events = read_task_progress_log(limit=limit)
    if not events:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        task_name = str(event.get("task_name", "") or "")
        status = str(event.get("status", "") or "")
        message = str(event.get("message", "") or "")
        event_at = str(event.get("event_at", "") or "")
        extra = event.get("extra", {}) if isinstance(event.get("extra"), dict) else {}

        try:
            progress_pct = int(float(event.get("progress_pct", 0) or 0))
        except Exception:
            progress_pct = 0
        progress_pct = max(0, min(100, progress_pct))
        progress_bar = str(extra.get("progress_bar", "") or _text_progress_bar(progress_pct))

        rows.append(
            {
                "时间": event_at,
                "任务": label_map.get(task_name, task_name),
                "状态": status,
                "进度": progress_bar,
                "日志": message,
            }
        )

    return pd.DataFrame(rows)


def _task_latest_success_time(task_name: str) -> str:
    task_status = read_task_status(task_name) or {}
    snapshot = read_snapshot(task_name) or {}
    return latest_success_timestamp(task_status, snapshot)


def _market_db_caption(display_count: int | None = None) -> str:
    try:
        db_status = DataManager().get_market_db_status()
    except Exception:
        db_status = {}
    stock_count = int(db_status.get("stock_count", 0) or 0)
    latest_trade_date = str(db_status.get("latest_trade_date", "") or "")
    pieces = [f"筛选范围：全市场数据库 {stock_count} 只", f"当前有效交易日：{latest_trade_date or '-'}"]
    if display_count is not None:
        pieces.append(f"当前展示条数：{int(display_count)}")
    return " | ".join(pieces)


def _sync_background_snapshot_state(
    *,
    enable_smart_picker: bool,
    enable_dragon_radar: bool,
    enable_ma5_pullback: bool,
) -> dict[str, dict | None]:
    smart_snapshot = read_snapshot("smart_pick") if enable_smart_picker else None
    if isinstance(smart_snapshot, dict):
        smart_status = str(smart_snapshot.get("status", "") or "")
        smart_generated_at = str(smart_snapshot.get("generated_at", "") or "")
        smart_task_status = read_task_status("smart_pick")
        worker_status = read_worker_status()
        if smart_status == "ok":
            st.session_state["smart_pick_df"] = _snapshot_payload_df(smart_snapshot, "smart_pick_df")
            st.session_state["smart_sector_rotation_df"] = _snapshot_payload_df(smart_snapshot, "sector_rotation_df")
            st.session_state["smart_sector_rotation_summary"] = _snapshot_payload_dict(smart_snapshot, "sector_rotation_summary")
            st.session_state["smart_macro_regime"] = _snapshot_payload_dict(smart_snapshot, "macro_regime")
            st.session_state["smart_macro_detail_df"] = _snapshot_payload_df(smart_snapshot, "macro_detail_df")
            st.session_state["smart_recent_entry_signal_df"] = _snapshot_payload_df(smart_snapshot, "recent_entry_signal_df")
            st.session_state["smart_pick_msg"] = f"科学选股快照已加载（后台更新时间：{smart_generated_at or '未知时间'}）。"
        elif smart_status == "error" and not should_hide_legacy_timeout_error(
            smart_task_status,
            worker_status,
            worker_stale_seconds=WORKER_HEARTBEAT_STALE_SECONDS,
        ):
            st.session_state["smart_pick_msg"] = f"科学选股后台任务失败：{smart_snapshot.get('error', '')}"
        smart_runtime_msg = _background_status_message(
            "smart_pick",
            "科学选股",
            smart_snapshot,
            SCHEDULED_REFRESH_STALE_SECONDS,
        )
        if smart_runtime_msg:
            st.session_state["smart_pick_msg"] = smart_runtime_msg

    sector_flow_snapshot = read_snapshot("sector_fund_flow") if enable_smart_picker else None
    if isinstance(sector_flow_snapshot, dict):
        sector_flow_status = str(sector_flow_snapshot.get("status", "") or "")
        sector_flow_generated_at = str(sector_flow_snapshot.get("generated_at", "") or "")
        if sector_flow_status == "ok":
            st.session_state["sector_fund_flow_df_map"] = _snapshot_payload_df_dict(sector_flow_snapshot, "sector_fund_flow_df_map")
            st.session_state["sector_fund_flow_summary_map"] = _snapshot_payload_dict(sector_flow_snapshot, "sector_fund_flow_summary_map")
            st.session_state["sector_fund_flow_msg"] = f"板块资金流快照已加载（后台更新时间：{sector_flow_generated_at or '未知时间'}）。"
        elif sector_flow_status == "error":
            st.session_state["sector_fund_flow_df_map"] = {}
            st.session_state["sector_fund_flow_summary_map"] = {}
            st.session_state["sector_fund_flow_msg"] = f"板块资金流后台任务失败：{sector_flow_snapshot.get('error', '')}"
        sector_flow_runtime_msg = _background_status_message(
            "sector_fund_flow",
            "板块资金流",
            sector_flow_snapshot,
            SCHEDULED_REFRESH_STALE_SECONDS,
        )
        if sector_flow_runtime_msg:
            st.session_state["sector_fund_flow_msg"] = sector_flow_runtime_msg

    dragon_snapshot = read_snapshot("dragon_radar") if enable_dragon_radar else None
    if isinstance(dragon_snapshot, dict):
        dragon_status = str(dragon_snapshot.get("status", "") or "")
        dragon_generated_at = str(dragon_snapshot.get("generated_at", "") or "")
        if dragon_status == "ok":
            st.session_state["dragon_radar_df"] = _snapshot_payload_df(dragon_snapshot, "dragon_radar_df")
            st.session_state["dragon_radar_msg"] = f"龙头雷达快照已加载（后台更新时间：{dragon_generated_at or '未知时间'}）。"
        elif dragon_status == "error":
            st.session_state["dragon_radar_df"] = pd.DataFrame()
            st.session_state["dragon_radar_msg"] = f"龙头雷达后台任务失败：{dragon_snapshot.get('error', '')}"
        dragon_runtime_msg = _background_status_message(
            "dragon_radar",
            "龙头雷达",
            dragon_snapshot,
            SCHEDULED_REFRESH_STALE_SECONDS,
        )
        if dragon_runtime_msg:
            st.session_state["dragon_radar_msg"] = dragon_runtime_msg

    ma5_pullback_snapshot = read_snapshot("ma5_pullback") if (enable_smart_picker and enable_ma5_pullback) else None
    if isinstance(ma5_pullback_snapshot, dict):
        status = str(ma5_pullback_snapshot.get("status", "") or "")
        generated_at = str(ma5_pullback_snapshot.get("generated_at", "") or "")
        if status == "ok":
            st.session_state["ma5_pullback_df"] = _snapshot_payload_df(ma5_pullback_snapshot, "ma5_pullback_df")
            st.session_state["ma5_pullback_msg"] = f"5日线回踩快照已加载（后台更新时间：{generated_at or '未知时间'}）。"
        elif status == "error":
            st.session_state["ma5_pullback_df"] = pd.DataFrame()
            st.session_state["ma5_pullback_msg"] = f"5日线回踩后台任务失败：{ma5_pullback_snapshot.get('error', '')}"
        ma5_runtime_msg = _background_status_message(
            "ma5_pullback",
            "5日线回踩",
            ma5_pullback_snapshot,
            SCHEDULED_REFRESH_STALE_SECONDS,
        )
        if ma5_runtime_msg:
            st.session_state["ma5_pullback_msg"] = ma5_runtime_msg

    today_snapshot = read_snapshot("today_entry") if enable_smart_picker else None
    if isinstance(today_snapshot, dict):
        today_status = str(today_snapshot.get("status", "") or "")
        today_generated_at = str(today_snapshot.get("generated_at", "") or "")
        if today_status == "ok":
            st.session_state["today_entry_df"] = _snapshot_payload_df(today_snapshot, "today_entry_df")
            st.session_state["today_entry_summary"] = _snapshot_payload_dict(today_snapshot, "today_entry_summary")
            st.session_state["right_side_watch_df"] = _snapshot_payload_df(today_snapshot, "right_side_watch_df")
            st.session_state["today_entry_msg"] = f"当日建仓快照已加载（后台更新时间：{today_generated_at or '未知时间'}）。"
        elif today_status == "error":
            st.session_state["today_entry_df"] = pd.DataFrame()
            st.session_state["today_entry_summary"] = {}
            st.session_state["right_side_watch_df"] = pd.DataFrame()
            st.session_state["today_entry_msg"] = f"当日建仓后台任务失败：{today_snapshot.get('error', '')}"
        today_runtime_msg = _background_status_message(
            "today_entry",
            "当日建仓",
            today_snapshot,
            SCHEDULED_REFRESH_STALE_SECONDS,
        )
        if today_runtime_msg:
            st.session_state["today_entry_msg"] = today_runtime_msg

    rebound_snapshot = read_snapshot("rebound_entry") if enable_smart_picker else None
    if isinstance(rebound_snapshot, dict):
        rebound_status = str(rebound_snapshot.get("status", "") or "")
        rebound_generated_at = str(rebound_snapshot.get("generated_at", "") or "")
        if rebound_status == "ok":
            st.session_state["rebound_entry_df"] = _snapshot_payload_df(rebound_snapshot, "rebound_entry_df")
            st.session_state["rebound_entry_summary"] = _snapshot_payload_dict(rebound_snapshot, "rebound_entry_summary")
            st.session_state["rebound_watch_df"] = _snapshot_payload_df(rebound_snapshot, "rebound_watch_df")
            st.session_state["rebound_entry_msg"] = f"副策略建仓快照已加载（后台更新时间：{rebound_generated_at or '未知时间'}）。"
        elif rebound_status == "error":
            st.session_state["rebound_entry_df"] = pd.DataFrame()
            st.session_state["rebound_entry_summary"] = {}
            st.session_state["rebound_watch_df"] = pd.DataFrame()
            st.session_state["rebound_entry_msg"] = f"副策略建仓后台任务失败：{rebound_snapshot.get('error', '')}"
        rebound_runtime_msg = _background_status_message(
            "rebound_entry",
            "副策略建仓",
            rebound_snapshot,
            SCHEDULED_REFRESH_STALE_SECONDS,
        )
        if rebound_runtime_msg:
            st.session_state["rebound_entry_msg"] = rebound_runtime_msg

    return {
        "smart_pick": smart_snapshot if isinstance(smart_snapshot, dict) else None,
        "sector_fund_flow": sector_flow_snapshot if isinstance(sector_flow_snapshot, dict) else None,
        "dragon_radar": dragon_snapshot if isinstance(dragon_snapshot, dict) else None,
        "ma5_pullback": ma5_pullback_snapshot if isinstance(ma5_pullback_snapshot, dict) else None,
        "today_entry": today_snapshot if isinstance(today_snapshot, dict) else None,
        "rebound_entry": rebound_snapshot if isinstance(rebound_snapshot, dict) else None,
    }


def load_watchlist_groups() -> dict[str, list[str]]:
    if WATCHLIST_FILE.exists():
        try:
            data = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "groups" in data and isinstance(data["groups"], dict):
                groups = {}
                for group_name, items in data["groups"].items():
                    if not isinstance(items, list):
                        continue
                    cleaned = []
                    for item in items:
                        try:
                            cleaned.append(normalize_ts_code(str(item)))
                        except ValueError:
                            continue
                    groups[str(group_name)] = sorted(set(cleaned))
                if groups:
                    return groups
            if isinstance(data, list):
                cleaned = []
                for item in data:
                    try:
                        cleaned.append(normalize_ts_code(str(item)))
                    except ValueError:
                        continue
                if cleaned:
                    return {"默认": sorted(set(cleaned)), "短线": [], "中线": []}
        except Exception:
            pass
    return {k: sorted(set(v)) for k, v in DEFAULT_GROUPS.items()}


def save_watchlist_groups(groups: dict[str, list[str]]) -> None:
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    WATCHLIST_FILE.write_text(
        json.dumps({"groups": groups}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def flatten_watchlist(groups: dict[str, list[str]]) -> list[str]:
    merged = []
    for items in groups.values():
        merged.extend(items)
    return sorted(set(merged))


def load_holding_users(holdings_df: pd.DataFrame | None = None) -> list[str]:
    users: list[str] = ["默认用户"]

    if HOLDING_USERS_FILE.exists():
        try:
            raw = json.loads(HOLDING_USERS_FILE.read_text(encoding="utf-8"))
            records = raw.get("users", raw) if isinstance(raw, dict) else raw
            if isinstance(records, list):
                for item in records:
                    name = str(item or "").strip()
                    if name:
                        users.append(name)
        except Exception:
            pass

    if holdings_df is not None and not holdings_df.empty and "user_name" in holdings_df.columns:
        users.extend(
            holdings_df["user_name"].astype(str).str.strip().replace("", "默认用户").tolist()
        )

    cleaned = sorted(set([u for u in users if str(u).strip()]))
    if "默认用户" not in cleaned:
        cleaned = ["默认用户"] + cleaned
    return cleaned


def save_holding_users(users: list[str]) -> None:
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    cleaned = sorted(set([str(u or "").strip() for u in users if str(u or "").strip()]))
    if "默认用户" not in cleaned:
        cleaned = ["默认用户"] + cleaned
    HOLDING_USERS_FILE.write_text(
        json.dumps({"users": cleaned}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_holdings() -> pd.DataFrame:
    columns = ["user_name", "ts_code", "name", "shares", "buy_price", "manual_return_pct"]
    if not HOLDINGS_FILE.exists():
        return pd.DataFrame(columns=columns)

    try:
        raw = json.loads(HOLDINGS_FILE.read_text(encoding="utf-8"))
        records = raw.get("holdings", raw) if isinstance(raw, dict) else raw
        if not isinstance(records, list):
            return pd.DataFrame(columns=columns)

        cleaned = []
        for item in records:
            if not isinstance(item, dict):
                continue
            try:
                ts_code = normalize_ts_code(str(item.get("ts_code", "")))
            except ValueError:
                continue
            name = str(item.get("name", "")).strip()
            user_name = str(item.get("user_name", "默认用户") or "默认用户").strip() or "默认用户"
            shares = float(item.get("shares", 0) or 0)
            buy_price = float(item.get("buy_price", 0) or 0)
            manual_return_pct = float(item.get("manual_return_pct", 0) or 0)
            if shares <= 0 or buy_price <= 0:
                continue
            cleaned.append(
                {
                    "user_name": user_name,
                    "ts_code": ts_code,
                    "name": name,
                    "shares": shares,
                    "buy_price": buy_price,
                    "manual_return_pct": manual_return_pct,
                }
            )
        if not cleaned:
            return pd.DataFrame(columns=columns)
        return pd.DataFrame(cleaned, columns=columns)
    except Exception:
        return pd.DataFrame(columns=columns)


def save_holdings(holdings_df: pd.DataFrame) -> None:
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    cols = ["user_name", "ts_code", "name", "shares", "buy_price", "manual_return_pct"]
    out = holdings_df.copy()
    for col in cols:
        if col not in out.columns:
            if col in {"shares", "buy_price", "manual_return_pct"}:
                out[col] = 0.0
            elif col == "user_name":
                out[col] = "默认用户"
            else:
                out[col] = ""
    out["user_name"] = out["user_name"].astype(str).str.strip().replace("", "默认用户")
    out = out[cols]
    HOLDINGS_FILE.write_text(
        json.dumps({"holdings": out.to_dict(orient="records")}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_holding_action_advice(
    data_manager: DataManager,
    holdings_df: pd.DataFrame,
    end_date: str,
    selected_factor_codes: list[str],
    factor_weights: dict[str, float],
    threshold: float,
    stop_profit_pct: float,
    stop_loss_pct: float,
    quote_force_refresh: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if holdings_df is None or holdings_df.empty:
        return pd.DataFrame(), holdings_df.copy()

    strategy = FactorSelectionStrategy(
        threshold=float(threshold),
        enabled_factors=selected_factor_codes if selected_factor_codes else ["momentum_20"],
        factor_weights=factor_weights,
    )

    rows = []
    updated_holdings = holdings_df.copy().reset_index(drop=True)
    today_str = pd.Timestamp.now().strftime("%Y%m%d")
    calc_end_date = today_str
    holding_codes = updated_holdings["ts_code"].astype(str).str.strip().tolist()
    trade_days = data_manager._recent_trade_days(calc_end_date, lookback_trade_days=140)
    holding_panel = pd.DataFrame()
    if trade_days:
        panel, _, _ = data_manager._prepare_db_priority_panel(
            end_date=calc_end_date,
            use_days=trade_days,
            universe_size=0,
            remote_fields="ts_code,trade_date,open,high,low,close,pct_chg,vol,amount,turnover_rate,pe,pb,dv_ratio,total_mv",
            force_refresh=bool(quote_force_refresh),
        )
        if panel is not None and not panel.empty:
            holding_panel = panel[panel["ts_code"].astype(str).isin(holding_codes)].copy()
    signal_panel = data_manager._generate_signal_panel_from_panel(
        holding_panel,
        threshold=float(threshold),
        enabled_factors=selected_factor_codes if selected_factor_codes else ["momentum_20"],
        factor_weights=factor_weights,
    )
    latest_signal_map: dict[str, pd.Series] = {}
    prev_signal_map: dict[str, int] = {}
    if signal_panel is not None and not signal_panel.empty:
        signal_panel = signal_panel.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
        for ts_code, hist in signal_panel.groupby("ts_code", sort=False):
            hist = hist.reset_index(drop=True)
            latest_signal_map[str(ts_code)] = hist.iloc[-1]
            prev_signal_map[str(ts_code)] = int(pd.to_numeric(hist["signal"], errors="coerce").shift(1).fillna(0).iloc[-1])

    quote_fetch_ok = True
    try:
        quote_df = data_manager.get_realtime_prices_akshare(
            ts_codes=updated_holdings["ts_code"].astype(str).tolist(),
            refresh_seconds=HOLDING_QUOTE_REFRESH_SECONDS,
            force_refresh=bool(quote_force_refresh),
        )
    except Exception:
        quote_fetch_ok = False
        quote_df = pd.DataFrame(columns=["ts_code", "name", "realtime_price", "quote_time", "price_source"])
    quote_map = {}
    source_map = {}
    quote_time_map = {}
    if quote_df is not None and not quote_df.empty:
        for _, qrow in quote_df.iterrows():
            q_code = str(qrow.get("ts_code", "")).strip().upper()
            q_price = float(qrow.get("realtime_price", 0) or 0)
            q_source = str(qrow.get("price_source", "")).strip()
            q_time = pd.to_datetime(qrow.get("quote_time"), errors="coerce")
            if q_code and q_price > 0:
                quote_map[q_code] = q_price
                source_map[q_code] = q_source
                quote_time_map[q_code] = q_time

    for idx, row in updated_holdings.iterrows():
        ts_code = str(row.get("ts_code", "")).strip()
        stock_name = str(row.get("name", "")).strip()
        shares = float(row.get("shares", 0) or 0)
        buy_price = float(row.get("buy_price", 0) or 0)
        manual_return_pct = float(row.get("manual_return_pct", 0) or 0)

        if not ts_code or shares <= 0 or buy_price <= 0:
            continue

        try:
            latest = latest_signal_map.get(ts_code)
            if latest is None:
                continue

            prev_signal = int(prev_signal_map.get(ts_code, 0))
            curr_signal = int(latest.get("signal", 0))

            close_px = float(latest.get("close", 0.0) or 0.0)
            close_source = "tushare_daily"
            realtime_px = float(quote_map.get(ts_code, 0.0) or 0.0)
            quote_time = quote_time_map.get(ts_code)

            eval_px = close_px
            price_source = "daily_close"

            if realtime_px > 0:
                eval_px = realtime_px
                price_source = source_map.get(ts_code, "akshare_live") or "akshare_live"
            factor_score = float(latest.get("factor_score", 0.0) or 0.0)
            trade_date = pd.to_datetime(latest.get("trade_date"), errors="coerce")

            floating_ret = eval_px / buy_price - 1.0 if eval_px > 0 and buy_price > 0 else 0.0
            pnl_amount = shares * (eval_px - buy_price)

            updated_holdings.loc[idx, "manual_return_pct"] = floating_ret * 100.0

            if floating_ret >= float(stop_profit_pct):
                advice = "止盈减仓"
            elif floating_ret <= -float(stop_loss_pct):
                advice = "止损减仓/离场"
            elif prev_signal == 1 and curr_signal == 0:
                advice = "信号转弱，建议减仓"
            elif prev_signal == 0 and curr_signal == 1:
                advice = "信号转强，可考虑加仓"
            elif curr_signal == 1:
                advice = "继续持有"
            else:
                advice = "观望或降低仓位"

            rows.append(
                {
                    "股票代码": ts_code,
                    "股票名称": stock_name,
                    "持股数": shares,
                    "买入价": buy_price,
                    "更新后收益%": floating_ret * 100.0,
                    "最新价来源": price_source,
                    "行情时间": quote_time.strftime("%Y-%m-%d %H:%M:%S") if pd.notna(quote_time) else "",
                    "最新收盘": close_px,
                    "最新收盘来源": close_source,
                    "当前价": eval_px,
                    "浮动收益%": floating_ret * 100.0,
                    "浮盈亏": pnl_amount,
                    "因子得分": factor_score,
                    "信号": curr_signal,
                    "当日建议": advice,
                    "最近交易日": trade_date.strftime("%Y-%m-%d") if pd.notna(trade_date) else "",
                }
            )
        except Exception:
            rows.append(
                {
                    "股票代码": ts_code,
                    "股票名称": stock_name,
                    "持股数": shares,
                    "买入价": buy_price,
                    "更新后收益%": manual_return_pct,
                    "最新价来源": "error",
                    "行情时间": "",
                    "最新收盘": np.nan,
                    "最新收盘来源": "error",
                    "当前价": np.nan,
                    "浮动收益%": np.nan,
                    "浮盈亏": np.nan,
                    "因子得分": np.nan,
                    "信号": np.nan,
                    "当日建议": "数据拉取失败，请稍后重试",
                    "最近交易日": "",
                }
            )

    advice_df = pd.DataFrame(rows)
    advice_df.attrs["quote_fetch_ok"] = quote_fetch_ok
    advice_df.attrs["quote_force_refresh"] = bool(quote_force_refresh)
    if not advice_df.empty and "最新价来源" in advice_df.columns:
        advice_df.attrs["quote_source_stats"] = advice_df["最新价来源"].astype(str).value_counts().to_dict()
    else:
        advice_df.attrs["quote_source_stats"] = {}
    if not advice_df.empty and "行情时间" in advice_df.columns:
        qts = pd.to_datetime(advice_df["行情时间"], errors="coerce").dropna()
        advice_df.attrs["latest_quote_time"] = qts.max().strftime("%Y-%m-%d %H:%M:%S") if not qts.empty else ""
    else:
        advice_df.attrs["latest_quote_time"] = ""
    return advice_df, updated_holdings


def build_label_map(stock_df) -> dict[str, str]:
    if stock_df is None or stock_df.empty:
        return {}
    label_map = {}
    for _, row in stock_df.iterrows():
        code = str(row.get("ts_code", "")).strip()
        name = str(row.get("name", "")).strip()
        if code:
            label_map[code] = f"{code} - {name}" if name else code
    return label_map


def factor_display_name(factor_code: str) -> str:
    zh = FACTOR_ZH.get(factor_code)
    return f"{factor_code}（{zh}）" if zh else factor_code


def factor_code_from_display(display_name: str) -> str:
    if "（" in display_name:
        return display_name.split("（", 1)[0]
    return display_name


def add_action_hints(detail_df: pd.DataFrame, stop_profit_pct: float, stop_loss_pct: float, enable_risk_hints: bool) -> pd.DataFrame:
    df = detail_df.copy().reset_index(drop=True)
    if df.empty:
        return df

    if "signal" not in df.columns:
        df["交易动作"] = "无信号列"
        return df

    prev_signal = df["signal"].shift(1).fillna(0).astype(int)
    curr_signal = df["signal"].fillna(0).astype(int)

    actions: list[str] = []
    risk_flags: list[str] = []
    hold_ret_list: list[float] = []

    in_trade = False
    entry_price = None

    for idx, row in df.iterrows():
        close_px = float(row.get("close", 0.0) or 0.0)
        p_sig = int(prev_signal.iloc[idx])
        c_sig = int(curr_signal.iloc[idx])

        action = "空仓观望"
        risk_flag = ""
        hold_ret = 0.0

        if p_sig == 0 and c_sig == 1:
            action = "建仓信号（下一交易日生效）"
            in_trade = True
            entry_price = close_px if close_px > 0 else None
        elif p_sig == 1 and c_sig == 0:
            action = "卖出信号（下一交易日生效）"
            in_trade = False
            entry_price = None
        elif c_sig == 1:
            action = "持有"

        if in_trade and entry_price and entry_price > 0 and close_px > 0:
            hold_ret = close_px / entry_price - 1.0
            if enable_risk_hints:
                if hold_ret >= stop_profit_pct:
                    risk_flag = "止盈提示"
                elif hold_ret <= -stop_loss_pct:
                    risk_flag = "止损提示"

        actions.append(action)
        risk_flags.append(risk_flag)
        hold_ret_list.append(hold_ret)

    df["交易动作"] = actions
    df["风控提示"] = risk_flags
    df["持仓浮动收益率"] = hold_ret_list
    return df


def format_detail_table(detail_df: pd.DataFrame) -> pd.DataFrame:
    detail_col_map = {
        "ts_code": "股票代码",
        "trade_date": "交易日期",
        "open": "开盘价",
        "high": "最高价",
        "low": "最低价",
        "close": "收盘价",
        "vol": "成交量",
        "amount": "成交额",
        "turnover_rate": "换手率",
        "pe": "市盈率PE",
        "pb": "市净率PB",
        "ps": "市销率PS",
        "dv_ratio": "股息率",
        "total_mv": "总市值",
        "signal": "信号",
        "position": "仓位",
        "ret": "收益率",
        "strategy_ret": "策略收益率",
        "benchmark_ret": "基准收益率",
        "strategy_nav": "策略净值",
        "benchmark_nav": "基准净值",
        "factor_score": "因子得分",
        "_source": "数据来源",
    }
    out = detail_df.rename(columns=detail_col_map)

    dynamic_factor_map = {}
    for col in out.columns:
        if col.startswith("factor_"):
            code = col.replace("factor_", "", 1)
            zh = FACTOR_ZH.get(code, code)
            dynamic_factor_map[col] = f"因子-{zh}"
    if dynamic_factor_map:
        out = out.rename(columns=dynamic_factor_map)

    if "交易日期" in out.columns:
        out = out.sort_values("交易日期").reset_index(drop=True)
    hidden_cols = ["市盈率PE", "市净率PB", "市销率PS", "股息率"]
    existing_hidden = [c for c in hidden_cols if c in out.columns]
    if existing_hidden:
        out = out.drop(columns=existing_hidden)
    return out


def run_factor_strategy(
    raw_df: pd.DataFrame,
    enabled_factors: list[str],
    factor_weights: dict[str, float],
    threshold: float,
) -> tuple[pd.DataFrame, dict]:
    strategy = FactorSelectionStrategy(
        threshold=float(threshold),
        enabled_factors=enabled_factors,
        factor_weights=factor_weights,
    )
    signal_df = strategy.generate_signals(raw_df.copy())
    result = BacktestEngine().run(signal_df)
    return result.equity_curve, result.stats


def _to_float(v, default: float = np.nan) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _clean_display_text(value: object, default: str = "") -> str:
    if value is None:
        return str(default)
    text = str(value).strip()
    if text in {"", "nan", "None", "NaT"}:
        return str(default)
    return text


def build_intraday_chart_url(ts_code: str) -> str:
    code = str(ts_code or "").strip().upper()
    if "." in code:
        symbol, market = code.split(".", 1)
        market = market.upper()
        if market == "SH":
            return f"https://quote.eastmoney.com/sh{symbol}.html"
        if market == "SZ":
            return f"https://quote.eastmoney.com/sz{symbol}.html"
        if market == "BJ":
            return f"https://quote.eastmoney.com/bj{symbol}.html"

    if code.startswith(("6", "9", "5")):
        return f"https://quote.eastmoney.com/sh{code[:6]}.html"
    if code.startswith(("0", "3")):
        return f"https://quote.eastmoney.com/sz{code[:6]}.html"
    return f"https://quote.eastmoney.com/{code}.html"


def build_local_backtest_detail_url(
    ts_code: str,
    name: str,
    lookback_days: int,
    threshold: float,
    enabled_factors: list[str],
    factor_weights: dict[str, float],
) -> str:
    params = {
        "backtest_code": str(ts_code or "").strip(),
        "backtest_name": str(name or "").strip(),
        "backtest_days": int(max(20, int(lookback_days))),
        "backtest_threshold": float(threshold),
        "backtest_factors": ",".join([str(item).strip() for item in enabled_factors if str(item).strip()]),
        "backtest_weights": json.dumps(factor_weights or {}, ensure_ascii=False),
    }
    return f"?{urlencode(params)}"


def render_intraday_links(df: pd.DataFrame, code_col: str, name_col: str, title: str = "点击股票名称查看当日分钟走势图") -> None:
    if df is None or df.empty:
        return

    parts = []
    for _, row in df.iterrows():
        code = str(row.get(code_col, "")).strip()
        name = str(row.get(name_col, "")).strip() or code
        if not code:
            continue
        url = build_intraday_chart_url(code)
        parts.append(f'<a href="{url}" target="_blank">{name}</a>')

    if not parts:
        return

    st.markdown(f"**{title}**")
    st.markdown(" | ".join(parts), unsafe_allow_html=True)


def render_recent_backtest_links(
    df: pd.DataFrame,
    code_col: str,
    name_col: str,
    threshold: float,
    enabled_factors: list[str],
    factor_weights: dict[str, float],
    lookback_days: int = 90,
    title: str = "点击后在新页面查看过去3个月回测明细",
) -> None:
    if df is None or df.empty:
        return

    parts = []
    for _, row in df.iterrows():
        code = str(row.get(code_col, "")).strip()
        name = str(row.get(name_col, "")).strip() or code
        if not code:
            continue
        url = build_local_backtest_detail_url(
            ts_code=code,
            name=name,
            lookback_days=lookback_days,
            threshold=threshold,
            enabled_factors=enabled_factors,
            factor_weights=factor_weights,
        )
        parts.append(f'<a href="{url}" target="_blank">查看 {code} {name} 近3个月回测</a>')

    if not parts:
        return

    st.markdown(f"**{title}**")
    st.markdown(" | ".join(parts), unsafe_allow_html=True)


def render_backtest_detail_links(
    df: pd.DataFrame,
    code_col: str,
    name_col: str,
    threshold: float,
    enabled_factors: list[str],
    factor_weights: dict[str, float],
    lookback_days: int = 60,
    title: str = "点击股票名称（新标签页）查看60日回测",
) -> None:
    if df is None or df.empty:
        return

    parts = []
    for _, row in df.iterrows():
        code = str(row.get(code_col, "")).strip()
        name = str(row.get(name_col, "")).strip() or code
        if not code:
            continue
        url = build_local_backtest_detail_url(
            ts_code=code,
            name=name,
            lookback_days=lookback_days,
            threshold=threshold,
            enabled_factors=enabled_factors,
            factor_weights=factor_weights,
        )
        parts.append(f'<a href="{url}" target="_blank">{name}</a>')

    if not parts:
        return

    st.markdown(f"**{title}**")
    st.markdown(" | ".join(parts), unsafe_allow_html=True)


def _load_three_bull_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
    except Exception:
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    return df


def _normalize_three_bull_candidate_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    work = filter_non_risk_warning(filter_non_chinext(df.copy()))
    if work.empty:
        return pd.DataFrame()

    if "trade_date" in work.columns:
        work["trade_date"] = pd.to_datetime(work["trade_date"], errors="coerce")

    numeric_cols = [
        "策略评分",
        "当日排名",
        "pct_chg",
        "量能比",
        "市场环境分",
        "板块强度分",
        "题材催化分",
        "执行确认分",
        "三阳累计涨幅%",
        "回踩天数",
        "回踩幅度%",
        "拥挤惩罚分",
        "次日收益%",
        "3日最高收益%",
        "3日收盘收益%",
    ]
    for col in numeric_cols:
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")

    if "候选优先级" not in work.columns:
        source = work["候选来源"] if "候选来源" in work.columns else pd.Series("", index=work.index)
        work["候选优先级"] = source.astype(str).eq("右侧确认候选").astype(int)

    sort_cols = [col for col in ["trade_date", "候选优先级", "策略评分", "执行确认分"] if col in work.columns]
    if sort_cols:
        ascending = [False if col != "执行确认分" else False for col in sort_cols]
        work = work.sort_values(sort_cols, ascending=ascending)
    return work.reset_index(drop=True)


def load_three_bull_latest_candidates() -> pd.DataFrame:
    return _normalize_three_bull_candidate_df(_load_three_bull_csv(THREE_BULL_LATEST_TOP20_FILE))


def load_three_bull_backtest_tables() -> tuple[pd.DataFrame, pd.DataFrame]:
    summary_df = _load_three_bull_csv(THREE_BULL_SUMMARY_FILE)
    score_bins_df = _load_three_bull_csv(THREE_BULL_SCORE_BINS_FILE)
    return summary_df, score_bins_df


def _three_bull_candidate_display_df(source_df: pd.DataFrame) -> pd.DataFrame:
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
    show_cols = [col for col in show_cols if col in source_df.columns]
    show_df = source_df[show_cols].copy()
    if "trade_date" in show_df.columns:
        show_df["trade_date"] = pd.to_datetime(show_df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return show_df.rename(
        columns={
            "trade_date": "信号日期",
            "ts_code": "股票代码",
            "name": "股票名称",
            "industry": "行业",
            "pct_chg": "当日涨幅%",
        }
    )


def render_three_bull_candidate_table(source_df: pd.DataFrame, *, height: int | None = None) -> None:
    if source_df is None or source_df.empty:
        st.info("暂无策略候选。请先运行 `python scripts/three_bull_pullback_report.py --min-score 60` 生成最新筛选结果。")
        return

    show_df = _three_bull_candidate_display_df(source_df)
    format_map = {
        "策略评分": "{:.2f}",
        "当日排名": "{:.0f}",
        "当日涨幅%": "{:.2f}%",
        "量能比": "{:.2f}x",
        "市场环境分": "{:.2f}",
        "板块强度分": "{:.2f}",
        "题材催化分": "{:.2f}",
        "执行确认分": "{:.2f}",
        "回踩天数": "{:.0f}",
        "回踩幅度%": "{:.2f}%",
    }
    active_format = {key: value for key, value in format_map.items() if key in show_df.columns}
    kwargs: dict[str, Any] = {"width": "stretch"}
    if height is not None:
        kwargs["height"] = int(height)
    st.dataframe(show_df.style.format(active_format), **kwargs)


def load_three_bull_high_score_samples(min_score: float = 80.0) -> pd.DataFrame:
    if not THREE_BULL_BACKTEST_TRADES_FILE.exists():
        return pd.DataFrame()

    try:
        df = pd.read_csv(THREE_BULL_BACKTEST_TRADES_FILE)
    except Exception:
        return pd.DataFrame()
    if df is None or df.empty or "策略评分" not in df.columns:
        return pd.DataFrame()

    df = filter_non_chinext(df)
    df = filter_non_risk_warning(df)
    if df.empty:
        return pd.DataFrame()

    df["策略评分"] = pd.to_numeric(df["策略评分"], errors="coerce")
    high = df[df["策略评分"] >= float(min_score)].copy()
    if high.empty:
        return pd.DataFrame()

    high["trade_date"] = pd.to_datetime(high.get("trade_date"), errors="coerce")
    for col in [
        "当日排名",
        "三阳累计涨幅%",
        "回踩天数",
        "回踩幅度%",
        "题材催化分",
        "板块强度分",
        "市场环境分",
        "拥挤惩罚分",
        "量能比",
        "执行确认分",
        "次日收益%",
        "3日最高收益%",
        "3日收盘收益%",
    ]:
        if col in high.columns:
            high[col] = pd.to_numeric(high[col], errors="coerce")

    if "候选优先级" not in high.columns:
        source = high["候选来源"] if "候选来源" in high.columns else pd.Series("", index=high.index)
        high["候选优先级"] = source.astype(str).eq("右侧确认候选").astype(int)
    return high.sort_values(["trade_date", "候选优先级", "策略评分"], ascending=[False, False, False]).reset_index(drop=True)


def render_three_bull_pullback_strategy_guide() -> None:
    st.subheader("三阳回踩 + 催化强化策略说明")
    st.caption("以下内容为策略规则说明与使用指南，用于帮助理解系统筛选逻辑，不构成投资建议。")

    latest_df = load_three_bull_latest_candidates()
    summary_df, score_bins_df = load_three_bull_backtest_tables()

    st.markdown("**页面位置：当前模块顶部的「最新筛选」页签，就是这套策略选出的股票。**")
    if latest_df.empty:
        st.info("暂无最新筛选结果。请先运行回测脚本生成 `cache/backtests/three_bull_pullback/latest_top20.csv`。")
    else:
        latest_date = pd.to_datetime(latest_df.get("trade_date"), errors="coerce").max()
        latest_date_text = latest_date.strftime("%Y-%m-%d") if pd.notna(latest_date) else "-"
        score = pd.to_numeric(latest_df.get("策略评分"), errors="coerce")
        volume_ratio = pd.to_numeric(latest_df.get("量能比"), errors="coerce")
        market_score = pd.to_numeric(latest_df.get("市场环境分"), errors="coerce")
        action_counts = latest_df.get("交易动作", pd.Series("", index=latest_df.index)).astype(str).value_counts()
        main_action = str(action_counts.index[0]) if not action_counts.empty else "-"

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("信号日期", latest_date_text)
        c2.metric("候选数量", int(len(latest_df)))
        c3.metric("最高评分", f"{float(score.max()):.2f}" if score.notna().any() else "-")
        c4.metric("平均量能比", f"{float(volume_ratio.mean()):.2f}x" if volume_ratio.notna().any() else "-")
        c5.metric("交易状态", main_action)

        if market_score.notna().any() and float(market_score.mean()) < 60.0:
            st.warning(f"当前市场环境分约 {float(market_score.mean()):.1f}，策略结果按弱市处理：优先观察或轻仓试错，不按重仓买入信号理解。")

    tab_latest, tab_backtest, tab_positioning, tab_pooling, tab_pullback, tab_catalyst, tab_execution, tab_scoring, tab_high_score = st.tabs(
        ["最新筛选", "回测表现", "策略定位", "入池规则", "回踩确认", "催化加分", "执行与风控", "评分与验证", "80分以上样本"]
    )

    with tab_latest:
        st.markdown("**当前策略选出的股票**")
        three_bull_success = _task_latest_success_time("three_bull_pullback")
        st.caption(
            "这里读取 `cache/backtests/three_bull_pullback/latest_top20.csv`。"
            f"后台最近成功：{three_bull_success or '-'}。表中「弱市轻仓」表示形态满足，但市场环境偏弱。"
        )
        render_three_bull_candidate_table(latest_df, height=520)
        if not latest_df.empty:
            render_recent_backtest_links(
                latest_df.head(10),
                code_col="ts_code",
                name_col="name",
                threshold=float(st.session_state.get("best_threshold", 0.1)),
                enabled_factors=["momentum_20"],
                factor_weights={"momentum_20": 1.0},
                title="最新策略 Top10：点击股票名称查看近3个月回测明细",
            )
            with st.expander("如何解读这张表"):
                st.markdown(
                    """
- `交易动作`：确认买点、弱市轻仓或观察等待；弱市轻仓不是重仓买入信号。
- `策略评分`：综合市场、板块、题材、回踩结构、执行确认后的排序分。
- `量能比`：最新交易日成交额相对 20 日均值，确认买点要求不低于 `1.2x`。
- `买点检查`：展示放量、突破、涨幅和市场环境是否达标。
"""
                )

    with tab_backtest:
        st.markdown("**历史回测表现**")
        st.caption("这里统计的是历史上满足同一套规则的样本，不代表今天这 20 支未来一定达到相同胜率。")
        if summary_df.empty:
            st.info("暂无回测汇总。请运行 `python scripts/three_bull_pullback_report.py --min-score 60`。")
        else:
            pct_cols = [col for col in summary_df.columns if col.endswith(("胜率", "收益", "达标率", "可控率"))]
            format_map = {col: "{:.2%}" for col in pct_cols}
            if "日均候选数" in summary_df.columns:
                format_map["日均候选数"] = "{:.2f}"
            st.dataframe(summary_df.style.format(format_map), width="stretch")
        if not score_bins_df.empty:
            st.markdown("**评分分组表现**")
            pct_cols = [col for col in score_bins_df.columns if col.endswith(("胜率", "收益", "达标率", "可控率"))]
            format_map = {col: "{:.2%}" for col in pct_cols}
            if "平均评分" in score_bins_df.columns:
                format_map["平均评分"] = "{:.2f}"
            st.dataframe(score_bins_df.style.format(format_map), width="stretch")
        st.caption("明细报告路径：`cache/backtests/three_bull_pullback/report.md`；项目总报告路径：`report.md`。")

    with tab_positioning:
        st.markdown(
            """
**策略定位**

- 这是一个偏右侧的强势股回踩策略，不是抄底策略。
- 核心思路是：先找被资金做强的票，再等回踩确认，最后用美盘映射或消息催化做优先级排序。
- 更适合做候选池和排序，不适合一眼打分后机械满仓执行。
"""
        )
        col_fit, col_avoid = st.columns(2)
        with col_fit:
            st.markdown(
                """
**适合环境**

- 市场偏强或存在结构性强势方向
- 成交额高于近 5 日均值
- 热点板块具备持续性，不是一日游
- 目标板块指数站在 10 日线或 20 日线之上
"""
            )
        with col_avoid:
            st.markdown(
                """
**不适合环境**

- 普跌日、退潮日、高位情绪崩塌日
- 热点轮动过快、缺少主线
- 市场持续缩量，板块承接弱
- 个股虽然有形态，但明显弱于板块
"""
            )
        st.markdown(
            """
**股票池范围**

- 剔除：`ST/*ST`、上市不足 120 日、日均成交额过低、流动性失真、明显庄股、重大解禁/减持/公告风险票。
- 优先：主线板块、板块内辨识度高、相对指数更强、成交额靠前、近期强于行业均值的个股。
"""
        )

    with tab_pooling:
        st.markdown(
            """
**三阳入池规则**

- 最近 5 个交易日内出现连续 3 根阳线。
- 三日累计涨幅建议落在 `+6% ~ +18%` 区间，避免过弱也避免末端加速。
- 至少 2 根是实体阳线，不能全是缩量十字或虚假上影。
- 三阳期间至少 1 天成交量高于近 20 日均量的 `1.2x`，视作资金确认。
- 三阳结束时，收盘价最好站上 `10 日线` 与 `20 日线`。
- 个股近 20 日强度应高于所属行业或板块的多数成分股。
"""
        )
        st.info("三阳只是进入候选池，不是直接买入信号。真正的关键在回踩质量、催化质量和次日承接。")

    with tab_pullback:
        st.markdown(
            """
**回踩确认规则**

- 回踩通常发生在三阳后的 `1 ~ 5` 个交易日内。
- 从三阳高点回撤建议控制在 `2% ~ 6%`，过深通常说明结构已经转弱。
- 收盘不应有效跌破 `10 日线`，最低价也不宜明显破坏三阳启动段中枢。
- 回踩量能应缩，但不能“缩到失真”；建议保持在三阳均量的 `55% ~ 85%`。
- 不接受放量中阴砸盘，也不接受连续两天破位下跌。
- 有下影、缩量、小实体整理，比直接长阴下杀更健康。
"""
        )
        st.warning("如果回踩阶段出现放量下跌、跌破关键均线、板块同步转弱，这类票更像强转弱，不应继续按候选股处理。")

    with tab_catalyst:
        col_us, col_cn = st.columns(2)
        with col_us:
            st.markdown(
                """
**美盘映射催化**

- 对应美股板块 ETF 或龙头隔夜明显上涨
- 中概同产业链走强
- 大宗商品相关链条出现海外涨价映射
- 海外政策、财报、产业事件对 A 股映射路径清晰
"""
            )
        with col_cn:
            st.markdown(
                """
**国内消息催化**

- 官方产业政策、监管表述、行业会议
- 订单、招标、业绩、产品发布等明确事件
- 板块级新闻能够清晰映射到具体受益链条
- 龙头公告超预期，能带动板块关注度
"""
            )
        st.markdown(
            """
**使用原则**

- 技术结构决定“能不能进候选池”，催化只决定“候选池里谁优先”。
- 催化是加分项，不是独立买入理由。
- 泛泛的利好、媒体情绪化标题、已经在竞价阶段充分透支的利好，不应给高权重。
"""
        )

    with tab_execution:
        col_buy, col_veto, col_risk = st.columns(3)
        with col_buy:
            st.markdown(
                """
**买入触发**

- 不追高开过猛，`0% ~ +3%` 更理想
- 开盘后看分时承接与量价配合
- 回踩低点不破，重新站上分时均价更优
- 可先打首笔仓位，再等确认后加仓
"""
            )
        with col_veto:
            st.markdown(
                """
**否决条件**

- 三阳出现在高位主升末端
- 回踩放量下跌
- 板块明显转弱
- 催化已在竞价阶段过度兑现
- 个股存在减持、监管、业绩雷等风险
"""
            )
        with col_risk:
            st.markdown(
                """
**风控与退出**

- 跌破回踩低点或放量跌破 10 日线，应止损
- 浮盈 `+6% ~ +8%` 可考虑减仓
- 买入后 2 到 3 天无上攻、催化落空、龙头掉队，应退出或降仓
- 没有退出规则，策略就不完整
"""
            )

    with tab_scoring:
        st.markdown(
            """
**建议评分框架（100 分）**

- `35 分`：结构强度
  三阳质量、趋势位置、相对行业/指数强弱
- `30 分`：回踩质量
  回撤幅度、量能流失情况、均线支撑是否有效
- `20 分`：催化强度
  美盘映射是否清晰、消息级别是否足够高、受益路径是否集中
- `15 分`：执行条件
  高开幅度、分时承接、板块同步性
"""
        )
        st.markdown(
            """
**使用建议**

- `80 分以上`：重点观察
- `70 ~ 79 分`：候选备选
- `70 分以下`：不建议纳入重点交易计划

**实盘前验证重点**

- 分市场环境看胜率与回撤
- 对比“有催化 / 无催化”的收益差异
- 看高开幅度对收益的影响
- 观察连续亏损次数，决定仓位上限
"""
        )

    with tab_high_score:
        high_score_df = load_three_bull_high_score_samples(min_score=80.0)
        if high_score_df.empty:
            st.info("暂无 80 分以上样本。可先运行 `python scripts/three_bull_pullback_report.py --min-score 60` 生成回测明细。")
        else:
            source = high_score_df["候选来源"] if "候选来源" in high_score_df.columns else pd.Series("", index=high_score_df.index)
            confirmed_df = high_score_df[source.astype(str).eq("右侧确认候选")].copy()
            watch_df = high_score_df[~high_score_df.index.isin(confirmed_df.index)].copy()
            metric_df = confirmed_df if not confirmed_df.empty else high_score_df
            next_ret = pd.to_numeric(metric_df.get("次日收益%"), errors="coerce")
            high_3d = pd.to_numeric(metric_df.get("3日最高收益%"), errors="coerce")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("确认买点样本", int(len(confirmed_df)))
            c2.metric("确认平均评分", f"{float(metric_df['策略评分'].mean()):.2f}")
            c3.metric("次日胜率", f"{float(next_ret.gt(0).mean()):.2%}")
            c4.metric("3日冲高达标", f"{float(high_3d.ge(3.0).mean()):.2%}")

            show_cols = [
                "trade_date",
                "ts_code",
                "name",
                "industry",
                "交易动作",
                "策略评分",
                "当日排名",
                "候选来源",
                "三阳累计涨幅%",
                "回踩天数",
                "回踩幅度%",
                "量能比",
                "执行确认分",
                "题材催化分",
                "板块强度分",
                "市场环境分",
                "拥挤惩罚分",
                "次日收益%",
                "3日最高收益%",
                "3日收盘收益%",
                "买点检查",
                "入选说明",
            ]
            format_map = {
                "策略评分": "{:.2f}",
                "当日排名": "{:.0f}",
                "三阳累计涨幅%": "{:.2f}%",
                "回踩天数": "{:.0f}",
                "回踩幅度%": "{:.2f}%",
                "量能比": "{:.2f}x",
                "执行确认分": "{:.2f}",
                "题材催化分": "{:.2f}",
                "板块强度分": "{:.2f}",
                "市场环境分": "{:.2f}",
                "拥挤惩罚分": "{:.2f}",
                "次日收益%": "{:.2f}%",
                "3日最高收益%": "{:.2f}%",
                "3日收盘收益%": "{:.2f}%",
            }

            def _display_high_score_table(source_df: pd.DataFrame) -> None:
                show_cols_local = [col for col in show_cols if col in source_df.columns]
                show_df = source_df[show_cols_local].copy()
                if "trade_date" in show_df.columns:
                    show_df["trade_date"] = pd.to_datetime(show_df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
                show_df = show_df.rename(
                    columns={
                        "trade_date": "信号日期",
                        "ts_code": "股票代码",
                        "name": "股票名称",
                        "industry": "行业",
                    }
                )
                active_format = {key: value for key, value in format_map.items() if key in show_df.columns}
                st.dataframe(show_df.style.format(active_format), use_container_width=True)

            st.markdown("**80 分以上确认买点**")
            if confirmed_df.empty:
                st.info("当前回测明细中暂无 80 分以上的右侧确认买点。观察票不会作为机械买入信号。")
            else:
                _display_high_score_table(confirmed_df)

            if not watch_df.empty:
                with st.expander(f"80 分以上待确认观察票（{len(watch_df)}）"):
                    _display_high_score_table(watch_df)
            render_recent_backtest_links(
                confirmed_df if not confirmed_df.empty else high_score_df,
                code_col="ts_code",
                name_col="name",
                threshold=float(st.session_state.get("best_threshold", 0.1)),
                enabled_factors=["momentum_20"],
                factor_weights={"momentum_20": 1.0},
                title="80分以上样本：点击后查看近3个月回测明细",
            )


def render_bottom_rebound_strategy_guide() -> None:
    st.subheader("副策略 + 抄底反弹策略说明")
    st.caption("以下内容为策略规则说明与使用指南，用于帮助理解系统筛选逻辑，不构成投资建议。")

    tab_positioning, tab_pooling, tab_rebound, tab_catalyst, tab_execution, tab_scoring = st.tabs(
        ["策略定位", "入池规则", "反弹确认", "催化加分", "执行与风控", "评分与验证"]
    )

    with tab_positioning:
        st.markdown(
            """
**策略定位**

- 这是一个偏左侧修复、但仍要求分时确认的副策略，不是无脑接飞刀。
- 核心思路是：先找阶段性超跌、但未彻底走坏的票，再等止跌和回流确认，最后结合板块修复与事件催化做排序。
- 更适合做主策略之外的补充仓位，不适合在情绪退潮日重仓逆势硬抄。
"""
        )
        col_fit, col_avoid = st.columns(2)
        with col_fit:
            st.markdown(
                """
**适合环境**

- 指数或板块处于急跌后的首轮修复期
- 目标板块未完全失去承接，板块轮动评分不极弱
- 个股出现缩量止跌或放量反抽
- 当日市场不是单边系统性崩盘
"""
            )
        with col_avoid:
            st.markdown(
                """
**不适合环境**

- 普跌加速日、破位杀跌日
- 高位主线集体补跌、情绪连续冰点
- 个股连续跌停、公告雷、减持雷、业绩雷
- 板块本身仍处在无承接下行通道
"""
            )
        st.markdown(
            """
**股票池范围**

- 剔除：`ST/*ST`、上市不足 120 日、流动性过差、明显庄股、重大公告风险票。
- 优先：近期有辨识度、超跌幅度足够、低位出现承接、所属板块不是最弱一档的个股。
"""
        )

    with tab_pooling:
        st.markdown(
            """
**超跌入池规则**

- 近 `5` 日跌幅一般达到 `-4%` 及以下，或近 `10` 日跌幅达到 `-8%` 及以下。
- 相比近 `20` 日高点，通常已有 `-12%` 左右的回撤，具备修复空间。
- RSI、连续下跌天数、短期回撤幅度中至少两项体现“短线超跌”。
- 不能是完全失控的断崖走势，低点附近要看到承接，而不是全天最低收盘。
- 个股仍需保留一定流动性，避免成交失真或被单一资金控盘。
"""
        )
        st.info("超跌只意味着有反弹土壤，不意味着立刻能买。真正的关键在止跌动作、量能回补和板块同步修复。")

    with tab_rebound:
        st.markdown(
            """
**反弹确认规则**

- 最新交易日应出现止跌动作，最好是阳线或下影明显的小实体。
- 收盘价需要重新站上短期承接位，例如前一日收盘、近几日整理高点或 `5 日线` 附近。
- 量能不能继续萎缩到失真，理想状态是较近 `10` 日均量回补到 `0.9x ~ 1.2x`。
- 从最近低点已有一定抬升，说明不是仍躺在最低位。
- 如果只是“止跌观察”，可以列入观察池；若出现放量、过前高、板块同步走强，则升级为确认候选。
"""
        )
        st.warning("抄底反弹的核心不是跌得多，而是跌够以后开始被接住。没有接住，就不是策略内买点。")

    with tab_catalyst:
        col_sector, col_event = st.columns(2)
        with col_sector:
            st.markdown(
                """
**板块修复催化**

- 板块指数重新站回 `10 日线` 或至少止跌企稳
- 板块资金流由大幅流出收敛为中性或回流
- 板块龙头先于跟风股翻红，说明修复不是假动作
- 海外映射或商品价格回暖，对链条修复有直接指向
"""
            )
        with col_event:
            st.markdown(
                """
**事件修复催化**

- 政策预期改善、行业会议、订单/招标、涨价消息
- 业绩预告或经营数据好于最悲观预期
- 龙头公司释放稳定预期，带动板块风险偏好修复
- 美股/港股对应映射链条隔夜止跌反弹
"""
            )
        st.markdown(
            """
**使用原则**

- 催化只负责提升排序，不负责替代止跌确认。
- 同样是超跌，带催化的优先级更高；但没有结构确认，仍不应直接重仓。
- 已在竞价阶段被充分透支的利好，不应再给过高加分。
"""
        )

    with tab_execution:
        col_buy, col_veto, col_risk = st.columns(3)
        with col_buy:
            st.markdown(
                """
**买入触发**

- 优先看低开后承接，而不是高开直接冲高
- 分时回到均价线之上、低点抬高更理想
- 可先试错首笔仓位，确认走强再加
- 更适合做 `T+1` 修复，而不是长期左侧埋伏
"""
            )
        with col_veto:
            st.markdown(
                """
**否决条件**

- 跌停或接近跌停后的弱反抽
- 反弹无量、冲高回落、收盘再创新低
- 板块继续走弱，龙头无修复
- 个股处在公告风险或业绩雷释放期
- 当天已经从低点拉太多，再追性价比很差
"""
            )
        with col_risk:
            st.markdown(
                """
**风控与退出**

- 跌回最近止跌低点下方，应快速止损
- 次日若无延续、冲高无量、板块掉队，应降仓或退出
- 浮盈 `+4% ~ +6%` 可考虑先兑现一部分
- 这类策略仓位应低于主策略，不能替代主策略
"""
            )

    with tab_scoring:
        st.markdown(
            """
**建议评分框架（100 分）**

- `30 分`：超跌质量
  回撤幅度、连续下跌天数、RSI 是否进入超跌区
- `25 分`：止跌结构
  是否收阳、是否抬高低点、是否重新站上短期承接位
- `20 分`：量能与承接
  是否有量、是否不是纯缩量死猫跳
- `15 分`：板块修复
  板块轮动评分、资金回流、板块龙头状态
- `10 分`：催化辅助
  事件是否明确、映射是否清晰
"""
        )
        st.markdown(
            """
**使用建议**

- `80 分以上`：重点观察，若分时配合可试仓
- `70 ~ 79 分`：小仓位备选
- `70 分以下`：只观察，不作为主交易计划

**实盘前验证重点**

- 区分“市场修复日”和“市场继续杀跌日”的收益差异
- 对比“仅超跌”与“超跌+放量确认”的胜率差异
- 观察高开过猛是否显著降低次日盈亏比
- 统计连续亏损次数，决定副策略仓位上限
"""
        )


def parse_minute_csv_input(
    minute_csv_text: str,
    minute_csv_file,
    default_trade_date: date,
    default_ts_code: str,
) -> pd.DataFrame:
    src_df = pd.DataFrame()
    if minute_csv_file is not None:
        src_df = pd.read_csv(minute_csv_file)
    elif (minute_csv_text or "").strip():
        src_df = pd.read_csv(StringIO(minute_csv_text.strip()))

    if src_df is None or src_df.empty:
        return pd.DataFrame()

    out = src_df.copy()
    if "trade_time" not in out.columns:
        if "datetime" in out.columns:
            out["trade_time"] = out["datetime"]
        elif "timestamp" in out.columns:
            out["trade_time"] = out["timestamp"]
        elif "time" in out.columns:
            day_prefix = pd.Timestamp(default_trade_date).strftime("%Y-%m-%d")
            out["trade_time"] = day_prefix + " " + out["time"].astype(str)
        elif "trade_date" in out.columns:
            out["trade_time"] = out["trade_date"]
        else:
            raise ValueError("分钟CSV需包含 trade_time/datetime/time/timestamp/trade_date 之一")

    out["trade_time"] = pd.to_datetime(out["trade_time"], errors="coerce")
    out = out.dropna(subset=["trade_time"]).sort_values("trade_time").reset_index(drop=True)

    if "close" not in out.columns:
        if "price" in out.columns:
            out["close"] = out["price"]
        elif "last" in out.columns:
            out["close"] = out["last"]
        else:
            raise ValueError("分钟CSV缺少 close 列（可用 price/last 替代）")

    for col in ["close", "open", "high", "low", "vol", "amount", "turnover_rate"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    out["close"] = out["close"].ffill().bfill()
    if "open" not in out.columns:
        out["open"] = out["close"]
    if "high" not in out.columns:
        out["high"] = out["close"]
    if "low" not in out.columns:
        out["low"] = out["close"]
    if "vol" not in out.columns:
        out["vol"] = 0.0
    if "amount" not in out.columns:
        out["amount"] = 0.0
    if "turnover_rate" not in out.columns:
        out["turnover_rate"] = np.nan
    if "ts_code" not in out.columns:
        out["ts_code"] = default_ts_code

    out = out[["trade_time", "ts_code", "open", "high", "low", "close", "vol", "amount", "turnover_rate"]]
    out = out.dropna(subset=["close"]).reset_index(drop=True)
    return out


def build_today_entry_plan(
    data_manager: DataManager,
    base_candidates: pd.DataFrame,
    end_dt: date,
    selected_factor_codes: list[str],
    factor_weights: dict[str, float],
    threshold: float,
    risk_profile: str,
) -> tuple[pd.DataFrame, dict]:
    if base_candidates is None or base_candidates.empty:
        return pd.DataFrame(), {}

    active_factors = selected_factor_codes if selected_factor_codes else ["momentum_20"]
    scan_start = (pd.Timestamp(end_dt) - pd.Timedelta(days=140)).strftime("%Y%m%d")
    scan_end = pd.Timestamp(end_dt).strftime("%Y%m%d")

    risk_init_ratio = {
        "稳健": 0.35,
        "均衡": 0.45,
        "激进": 0.60,
    }
    init_ratio = float(risk_init_ratio.get(risk_profile, 0.45))

    candidates = base_candidates.copy().reset_index(drop=True)
    n = len(candidates)
    rows = []
    for idx, row in candidates.iterrows():
        code = str(row.get("ts_code", "")).strip()
        if not code:
            continue

        try:
            raw_df = data_manager.get_daily_data(code, scan_start, scan_end, use_cache=True)
        except Exception:
            continue

        if raw_df is None or raw_df.empty or len(raw_df) < 25:
            continue

        signal_df = FactorSelectionStrategy(
            threshold=float(threshold),
            enabled_factors=active_factors,
            factor_weights=factor_weights,
        ).generate_signals(raw_df.copy())

        if signal_df is None or signal_df.empty:
            continue

        signal_df = signal_df.sort_values("trade_date").reset_index(drop=True)
        latest = signal_df.iloc[-1]

        close_now = _to_float(latest.get("close"), np.nan)
        close_5 = _to_float(signal_df["close"].iloc[-6], np.nan) if len(signal_df) > 5 else np.nan
        close_20 = _to_float(signal_df["close"].iloc[-21], np.nan) if len(signal_df) > 20 else np.nan
        ret_5 = (close_now / close_5 - 1.0) if np.isfinite(close_now) and np.isfinite(close_5) and close_5 > 0 else 0.0
        ret_20 = (close_now / close_20 - 1.0) if np.isfinite(close_now) and np.isfinite(close_20) and close_20 > 0 else 0.0

        factor_score = _to_float(row.get("最终推荐评分"), _to_float(latest.get("factor_score"), 0.0))
        factor_norm = float(0.5 + 0.5 * np.tanh(factor_score))

        turnover = _to_float(latest.get("turnover_rate"), np.nan)
        pe = _to_float(latest.get("pe"), np.nan)
        pb = _to_float(latest.get("pb"), np.nan)
        total_mv = _to_float(latest.get("total_mv"), np.nan)
        sector_rotation_score = _to_float(row.get("板块轮动评分"), 0.0)
        sector_rotation_norm = float(0.5 + 0.5 * np.tanh(sector_rotation_score))
        kondratiev_phase = str(row.get("康波阶段", "")).strip()
        rotation_state = str(row.get("轮动状态", "")).strip()
        wave_stage = str(row.get("板块波浪阶段", "")).strip()
        entry_hint = str(row.get("建仓提示", "")).strip()
        buy_point_score = float(np.clip(_to_float(row.get("买点评分"), 50.0), 0.0, 100.0))
        overheat_penalty = float(np.clip(_to_float(row.get("过热惩罚分"), 0.0), 0.0, 100.0))
        freshness_score = float(np.clip(_to_float(row.get("信号新鲜度"), 50.0), 0.0, 100.0))
        streak_up_days = float(np.clip(_to_float(row.get("连续上涨天数"), 0.0), 0.0, 20.0))
        high_chase = float(np.clip(_to_float(row.get("高位追涨标记"), 0.0), 0.0, 1.0))
        candidate_source = str(row.get("候选来源", "科学选股") or "科学选股").strip()
        right_side_raw = _to_float(row.get("右侧确认分"), 0.0)
        right_side_score = float(np.clip(right_side_raw if np.isfinite(right_side_raw) else 0.0, 0.0, 100.0))
        right_side_label = str(row.get("右侧形态", "") or "").strip()
        pullback_days_raw = _to_float(row.get("回踩天数"), 0.0)
        pullback_days = float(np.clip(pullback_days_raw if np.isfinite(pullback_days_raw) else 0.0, 0.0, 5.0))
        pullback_depth_raw = _to_float(row.get("回踩幅度%"), 0.0)
        pullback_depth_pct = float(pullback_depth_raw if np.isfinite(pullback_depth_raw) else 0.0)
        has_right_side = right_side_score > 0.0

        setup_norm = float(np.clip(buy_point_score / 100.0, 0.0, 1.0))
        freshness_norm = float(np.clip(freshness_score / 100.0, 0.0, 1.0))
        overheat_norm = float(np.clip(overheat_penalty / 100.0, 0.0, 1.0))

        finance_norm = 0.5
        if np.isfinite(pe) and pe > 0:
            if 5.0 <= pe <= 45.0:
                finance_norm += 0.15
            elif pe >= 90.0:
                finance_norm -= 0.15
        if np.isfinite(pb) and pb > 0:
            if pb <= 4.0:
                finance_norm += 0.10
            elif pb >= 8.0:
                finance_norm -= 0.10
        if np.isfinite(turnover) and turnover > 0:
            if 1.0 <= turnover <= 12.0:
                finance_norm += 0.10
            elif turnover >= 25.0:
                finance_norm -= 0.10
        if np.isfinite(total_mv) and total_mv > 0 and total_mv <= 1_500_000:
            finance_norm += 0.05
        finance_norm = float(np.clip(finance_norm, 0.0, 1.0))

        smart_rank_norm = 1.0 if n <= 1 else float(1.0 - idx / (n - 1))
        trend_guard_norm = 0.50
        if ret_20 > 0:
            trend_guard_norm += 0.10
        if -0.04 <= ret_5 <= 0.05:
            trend_guard_norm += 0.12
        elif ret_5 > 0.08:
            trend_guard_norm -= 0.18
        elif ret_5 < -0.06:
            trend_guard_norm -= 0.10
        if streak_up_days >= 4:
            trend_guard_norm -= min(0.18, 0.04 * (streak_up_days - 3.0))
        if high_chase > 0.5:
            trend_guard_norm -= 0.20
        trend_guard_norm = float(np.clip(trend_guard_norm, 0.0, 1.0))

        right_side_norm = float(np.clip(right_side_score / 100.0, 0.0, 1.0))
        pullback_quality = 0.0
        if has_right_side:
            pullback_quality = float(np.clip(1.0 - abs(abs(pullback_depth_pct) - 3.0) / 4.0, 0.0, 1.0))
        total_score = float(
            0.24 * factor_norm
            + 0.14 * finance_norm
            + 0.10 * smart_rank_norm
            + 0.10 * sector_rotation_norm
            + 0.15 * setup_norm
            + 0.10 * freshness_norm
            + 0.09 * trend_guard_norm
            + 0.12 * right_side_norm
            + 0.06 * pullback_quality
            - 0.15 * overheat_norm
        )

        signal_now = int(_to_float(latest.get("signal"), 0.0))
        base_position = max(0.0, _to_float(row.get("建议仓位%"), 8.0 if has_right_side else 0.0))
        entry_ready = bool(signal_now == 1 or (has_right_side and right_side_score >= 70.0))
        disallow_entry = (
            entry_hint == "不宜追高"
            or wave_stage in {"5浪冲顶", "C浪探底", "下行调整"}
            or high_chase > 0.5
            or overheat_penalty >= 60.0
        )

        if disallow_entry:
            action = "暂不建仓"
            pos_ratio = 0.0
        elif entry_ready and total_score >= 0.72 and buy_point_score >= 70:
            action = "今日优先建仓"
            pos_ratio = init_ratio if not has_right_side else max(0.28, init_ratio - 0.05)
        elif entry_ready and total_score >= 0.62 and buy_point_score >= 62:
            action = "分批试仓"
            pos_ratio = max(0.24, init_ratio - 0.12)
        elif entry_ready and total_score >= 0.54:
            action = "轻仓试错"
            pos_ratio = 0.18 if has_right_side else 0.20
        elif total_score >= 0.60 and buy_point_score >= 65 and freshness_score >= 60:
            action = "观察候选"
            pos_ratio = 0.10
        else:
            action = "暂不建仓"
            pos_ratio = 0.0

        today_position = float(base_position * pos_ratio)
        first_trade_position = float(today_position * 0.5)

        reasons = []
        if factor_norm >= 0.55:
            reasons.append("强度基础仍在")
        if has_right_side and right_side_label:
            reasons.append(right_side_label)
        if ret_20 > 0:
            reasons.append("中期趋势向上")
        if has_right_side and pullback_days > 0:
            reasons.append(f"回踩{int(pullback_days)}天后再转强")
        if buy_point_score >= 75:
            reasons.append("当前买点较优")
        elif buy_point_score < 60:
            reasons.append("买点仍需等待")
        if freshness_score >= 80:
            reasons.append("信号较新")
        elif freshness_score <= 20:
            reasons.append("信号偏旧")
        if finance_norm >= 0.60:
            reasons.append("估值与活跃度匹配")
        if rotation_state == "走强":
            reasons.append("所属板块轮动走强")
        elif kondratiev_phase in {"复苏", "繁荣"}:
            reasons.append(f"所属板块处于康波{kondratiev_phase}")
        if wave_stage in {"2浪回踩", "4浪整理", "1浪启动"}:
            reasons.append(f"板块处于{wave_stage}")
        if high_chase > 0.5 or streak_up_days >= 4:
            reasons.append("短线偏热避免追高")
        if entry_hint == "适合建仓":
            reasons.append("板块结构支持建仓")
        elif entry_hint == "只适合观察":
            reasons.append("先等回踩确认")
        if not reasons:
            reasons.append("等待更优买点确认")

        rows.append(
            {
                "ts_code": code,
                "name": str(row.get("name", "")).strip(),
                "行业": str(row.get("industry", "")).strip(),
                "候选来源": candidate_source,
                "右侧形态": right_side_label,
                "右侧确认分": right_side_score,
                "回踩天数": pullback_days,
                "回踩幅度%": pullback_depth_pct,
                "康波阶段": kondratiev_phase,
                "轮动状态": rotation_state,
                "板块波浪阶段": wave_stage,
                "建仓提示": entry_hint,
                "当日动作": action,
                "建议建仓%": today_position,
                "首笔建议%": first_trade_position,
                "因子得分": factor_score,
                "买点评分": buy_point_score,
                "过热惩罚分": overheat_penalty,
                "信号新鲜度": freshness_score,
                "连续上涨天数": streak_up_days,
                "高位追涨标记": high_chase,
                "综合得分": total_score,
                "近5日涨跌%": ret_5 * 100.0,
                "近20日涨跌%": ret_20 * 100.0,
                "换手率": turnover,
                "PE": pe,
                "PB": pb,
                "财经与表现解读": "；".join(reasons),
            }
        )

    if not rows:
        return pd.DataFrame(), {}

    out_df = pd.DataFrame(rows).sort_values(["综合得分", "买点评分", "建议建仓%"], ascending=False).head(20).reset_index(drop=True)
    entry_mask = out_df["当日动作"].isin(["今日优先建仓", "分批试仓", "轻仓试错"])
    summary = {
        "股票数": int(len(out_df)),
        "可建仓只数": int(entry_mask.sum()),
        "建议建仓合计%": float(out_df.loc[entry_mask, "建议建仓%"].sum()),
        "近5日均值%": float(out_df["近5日涨跌%"].mean()),
        "综合得分均值": float(out_df["综合得分"].mean()),
        "平均买点评分": float(out_df["买点评分"].mean()),
        "平均过热惩罚分": float(out_df["过热惩罚分"].mean()),
        "右侧确认只数": int(out_df["候选来源"].astype(str).str.contains("右侧确认", na=False).sum()) if "候选来源" in out_df.columns else 0,
    }
    return out_df, summary


def build_bottom_rebound_entry_plan(
    data_manager: DataManager,
    rebound_candidates: pd.DataFrame,
    end_dt: date,
    selected_factor_codes: list[str],
    factor_weights: dict[str, float],
    threshold: float,
    risk_profile: str,
) -> tuple[pd.DataFrame, dict]:
    if rebound_candidates is None or rebound_candidates.empty:
        return pd.DataFrame(), {}

    active_factors = selected_factor_codes if selected_factor_codes else ["momentum_20"]
    scan_start = (pd.Timestamp(end_dt) - pd.Timedelta(days=140)).strftime("%Y%m%d")
    scan_end = pd.Timestamp(end_dt).strftime("%Y%m%d")

    risk_init_ratio = {
        "稳健": 0.24,
        "均衡": 0.32,
        "激进": 0.40,
    }
    init_ratio = float(risk_init_ratio.get(risk_profile, 0.32))

    candidates = rebound_candidates.copy().reset_index(drop=True)
    candidates = candidates.sort_values(
        ["反弹确认分", "信号新鲜度", "板块轮动评分"],
        ascending=False,
    ).head(20).reset_index(drop=True)

    n = len(candidates)
    rows = []
    for idx, row in candidates.iterrows():
        code = str(row.get("ts_code", "")).strip()
        if not code:
            continue

        try:
            raw_df = data_manager.get_daily_data(code, scan_start, scan_end, use_cache=True)
        except Exception:
            continue

        if raw_df is None or raw_df.empty or len(raw_df) < 25:
            continue

        signal_df = FactorSelectionStrategy(
            threshold=float(threshold),
            enabled_factors=active_factors,
            factor_weights=factor_weights,
        ).generate_signals(raw_df.copy())

        if signal_df is None or signal_df.empty:
            continue

        signal_df = signal_df.sort_values("trade_date").reset_index(drop=True)
        latest = signal_df.iloc[-1]

        close_now = _to_float(latest.get("close"), np.nan)
        close_5 = _to_float(signal_df["close"].iloc[-6], np.nan) if len(signal_df) > 5 else np.nan
        close_20 = _to_float(signal_df["close"].iloc[-21], np.nan) if len(signal_df) > 20 else np.nan
        ret_5 = (close_now / close_5 - 1.0) if np.isfinite(close_now) and np.isfinite(close_5) and close_5 > 0 else 0.0
        ret_20 = (close_now / close_20 - 1.0) if np.isfinite(close_now) and np.isfinite(close_20) and close_20 > 0 else 0.0

        factor_score = _to_float(latest.get("factor_score"), 0.0)
        factor_norm = float(0.5 + 0.5 * np.tanh(factor_score))

        turnover = _to_float(latest.get("turnover_rate"), np.nan)
        pe = _to_float(latest.get("pe"), np.nan)
        pb = _to_float(latest.get("pb"), np.nan)
        total_mv = _to_float(latest.get("total_mv"), np.nan)
        sector_rotation_score = _to_float(row.get("板块轮动评分"), 0.0)
        sector_rotation_norm = float(np.clip(0.5 + 0.5 * np.tanh(sector_rotation_score), 0.0, 1.0))
        kondratiev_phase = str(row.get("康波阶段", "")).strip()
        rotation_state = str(row.get("轮动状态", "")).strip()
        wave_stage = str(row.get("板块波浪阶段", "")).strip()
        entry_hint = str(row.get("建仓提示", "")).strip()
        candidate_source = str(row.get("候选来源", "副策略") or "副策略").strip()
        rebound_label = str(row.get("反弹形态", "") or "").strip()
        rebound_score = float(np.clip(_to_float(row.get("反弹确认分"), 0.0), 0.0, 100.0))
        oversold_depth_pct = float(_to_float(row.get("超跌幅度%"), 0.0))
        rebound_lift_pct = float(_to_float(row.get("低位回升幅度%"), 0.0))
        buy_point_score = float(np.clip(_to_float(row.get("买点评分"), rebound_score), 0.0, 100.0))
        overheat_penalty = float(np.clip(_to_float(row.get("过热惩罚分"), 0.0), 0.0, 100.0))
        freshness_score = float(np.clip(_to_float(row.get("信号新鲜度"), 50.0), 0.0, 100.0))
        down_streak = float(np.clip(_to_float(row.get("连续下跌天数"), 0.0), 0.0, 20.0))
        high_chase = float(np.clip(_to_float(row.get("高位追涨标记"), 0.0), 0.0, 1.0))
        has_confirmation = candidate_source == "抄底反弹候选"

        oversold_norm = float(np.clip(abs(min(oversold_depth_pct, -1.0)) / 18.0, 0.0, 1.0))
        lift_norm = float(np.clip(max(rebound_lift_pct, 0.0) / 8.0, 0.0, 1.0))
        freshness_norm = float(np.clip(freshness_score / 100.0, 0.0, 1.0))
        heat_norm = float(np.clip(overheat_penalty / 100.0, 0.0, 1.0))
        rebound_norm = float(np.clip(rebound_score / 100.0, 0.0, 1.0))

        finance_norm = 0.5
        if np.isfinite(pe) and pe > 0:
            if 5.0 <= pe <= 55.0:
                finance_norm += 0.08
            elif pe >= 120.0:
                finance_norm -= 0.10
        if np.isfinite(pb) and pb > 0:
            if pb <= 4.5:
                finance_norm += 0.08
            elif pb >= 10.0:
                finance_norm -= 0.10
        if np.isfinite(turnover) and turnover > 0:
            if 1.0 <= turnover <= 15.0:
                finance_norm += 0.10
            elif turnover >= 28.0:
                finance_norm -= 0.10
        if np.isfinite(total_mv) and total_mv > 0 and total_mv <= 1_800_000:
            finance_norm += 0.04
        finance_norm = float(np.clip(finance_norm, 0.0, 1.0))

        smart_rank_norm = 1.0 if n <= 1 else float(1.0 - idx / (n - 1))
        trend_guard_norm = 0.48
        if -0.12 <= ret_5 <= 0.04:
            trend_guard_norm += 0.14
        elif ret_5 > 0.08:
            trend_guard_norm -= 0.15
        elif ret_5 < -0.10:
            trend_guard_norm -= 0.08
        if ret_20 > 0.12:
            trend_guard_norm -= 0.08
        elif ret_20 > -0.02:
            trend_guard_norm += 0.05
        if down_streak >= 4:
            trend_guard_norm += min(0.10, 0.02 * (down_streak - 3.0))
        if high_chase > 0.5:
            trend_guard_norm -= 0.20
        trend_guard_norm = float(np.clip(trend_guard_norm, 0.0, 1.0))

        total_score = float(
            0.16 * factor_norm
            + 0.20 * rebound_norm
            + 0.12 * oversold_norm
            + 0.10 * lift_norm
            + 0.10 * sector_rotation_norm
            + 0.10 * freshness_norm
            + 0.08 * finance_norm
            + 0.08 * smart_rank_norm
            + 0.10 * trend_guard_norm
            + (0.06 if has_confirmation else 0.0)
            - 0.12 * heat_norm
        )

        signal_now = int(_to_float(latest.get("signal"), 0.0))
        base_position = max(0.0, _to_float(row.get("建议仓位%"), 6.0 if has_confirmation else 0.0))
        entry_ready = bool(has_confirmation or signal_now == 1)
        disallow_entry = (
            high_chase > 0.5
            or overheat_penalty >= 60.0
            or (wave_stage in {"5浪冲顶", "下行调整"} and not has_confirmation)
        )

        if disallow_entry:
            action = "暂不建仓"
            pos_ratio = 0.0
        elif entry_ready and total_score >= 0.70 and rebound_score >= 75.0:
            action = "今日优先建仓"
            pos_ratio = max(0.18, init_ratio)
        elif entry_ready and total_score >= 0.60 and rebound_score >= 68.0:
            action = "分批试仓"
            pos_ratio = max(0.16, init_ratio - 0.08)
        elif total_score >= 0.52 and freshness_score >= 58.0:
            action = "轻仓试错"
            pos_ratio = 0.14 if has_confirmation else 0.12
        elif total_score >= 0.50:
            action = "观察候选"
            pos_ratio = 0.0
        else:
            action = "暂不建仓"
            pos_ratio = 0.0

        today_position = float(base_position * pos_ratio)
        first_trade_position = float(today_position * 0.45)

        reasons = []
        if oversold_norm >= 0.55:
            reasons.append("超跌幅度已到位")
        if rebound_label:
            reasons.append(rebound_label)
        if has_confirmation:
            reasons.append("已有反弹确认")
        else:
            reasons.append("先看次日延续")
        if freshness_score >= 80:
            reasons.append("信号较新")
        elif freshness_score <= 20:
            reasons.append("信号偏旧")
        if rebound_lift_pct >= 3.0:
            reasons.append(f"较低点回升{rebound_lift_pct:.1f}%")
        if down_streak >= 3:
            reasons.append(f"前期连续回落{int(down_streak)}天")
        if rotation_state == "走强":
            reasons.append("板块进入修复")
        elif sector_rotation_norm >= 0.55:
            reasons.append("板块强弱不差")
        if entry_hint == "适合建仓":
            reasons.append("板块结构支持试仓")
        elif entry_hint == "只适合观察":
            reasons.append("仍以观察为主")
        if factor_norm >= 0.55:
            reasons.append("基础因子未失真")
        if high_chase > 0.5 or overheat_penalty >= 15:
            reasons.append("反抽过急避免追高")
        if not reasons:
            reasons.append("等待更扎实的止跌确认")

        rows.append(
            {
                "ts_code": code,
                "name": _clean_display_text(row.get("name", ""), default=""),
                "行业": _clean_display_text(row.get("industry", ""), default="未分类") or "未分类",
                "候选来源": candidate_source,
                "反弹形态": rebound_label,
                "反弹确认分": rebound_score,
                "超跌幅度%": oversold_depth_pct,
                "低位回升幅度%": rebound_lift_pct,
                "康波阶段": kondratiev_phase,
                "轮动状态": rotation_state,
                "板块波浪阶段": wave_stage,
                "建仓提示": entry_hint,
                "当日动作": action,
                "建议建仓%": today_position,
                "首笔建议%": first_trade_position,
                "因子得分": factor_score,
                "买点评分": buy_point_score,
                "过热惩罚分": overheat_penalty,
                "信号新鲜度": freshness_score,
                "连续下跌天数": down_streak,
                "高位追涨标记": high_chase,
                "综合得分": total_score,
                "近5日涨跌%": ret_5 * 100.0,
                "近20日涨跌%": ret_20 * 100.0,
                "换手率": turnover,
                "PE": pe,
                "PB": pb,
                "财经与表现解读": "；".join(reasons),
            }
        )

    if not rows:
        return pd.DataFrame(), {}

    out_df = pd.DataFrame(rows).sort_values(["综合得分", "反弹确认分", "建议建仓%"], ascending=False).head(20).reset_index(drop=True)
    entry_mask = out_df["当日动作"].isin(["今日优先建仓", "分批试仓", "轻仓试错"])
    summary = {
        "股票数": int(len(out_df)),
        "可建仓只数": int(entry_mask.sum()),
        "建议建仓合计%": float(out_df.loc[entry_mask, "建议建仓%"].sum()),
        "近5日均值%": float(out_df["近5日涨跌%"].mean()),
        "综合得分均值": float(out_df["综合得分"].mean()),
        "平均买点评分": float(out_df["买点评分"].mean()),
        "平均过热惩罚分": float(out_df["过热惩罚分"].mean()),
        "反弹确认只数": int(out_df["候选来源"].astype(str).str.contains("抄底反弹", na=False).sum()),
    }
    return out_df, summary


def rolling_stage_compare(raw_df: pd.DataFrame, threshold: float, window_size: int, step: int) -> pd.DataFrame:
    stages = {
        "阶段1-技术因子": TECHNICAL_FACTORS,
        "阶段2-技术+量价": TECHNICAL_FACTORS + VOLUME_PRICE_FACTORS,
        "阶段3-技术+量价+基本面": TECHNICAL_FACTORS + VOLUME_PRICE_FACTORS + FUNDAMENTAL_FACTORS,
    }

    rows = []
    n = len(raw_df)
    if n < window_size:
        return pd.DataFrame()

    for start in range(0, n - window_size + 1, step):
        end = start + window_size
        win_df = raw_df.iloc[start:end].copy()
        win_start = pd.to_datetime(win_df["trade_date"].iloc[0]).strftime("%Y-%m-%d")
        win_end = pd.to_datetime(win_df["trade_date"].iloc[-1]).strftime("%Y-%m-%d")

        for stage_name, factors in stages.items():
            weights = {name: 1.0 for name in factors}
            _, stats = run_factor_strategy(win_df, factors, weights, threshold)
            rows.append(
                {
                    "阶段": stage_name,
                    "窗口开始": win_start,
                    "窗口结束": win_end,
                    "年化收益": stats["ann_return"],
                    "夏普": stats["sharpe"],
                    "最大回撤": stats["max_drawdown"],
                    "胜率": stats["win_rate"],
                }
            )

    return pd.DataFrame(rows)


def grid_search_factor_params(raw_df: pd.DataFrame, selected_factors: list[str]) -> tuple[dict, pd.DataFrame, str]:
    if not selected_factors:
        selected_factors = ["momentum_20"]

    note = ""
    search_factors = list(selected_factors)
    if len(search_factors) > 4:
        search_factors = search_factors[:4]
        note = "因子较多，网格搜索仅对前4个因子执行（其余因子权重保留1.0）。"

    threshold_grid = [-0.2, -0.1, 0.0, 0.1, 0.2]
    weight_grid = [0.0, 0.5, 1.0, 1.5]
    combos = len(threshold_grid) * (len(weight_grid) ** len(search_factors))
    if combos > 2000:
        weight_grid = [0.0, 1.0]
        combos = len(threshold_grid) * (len(weight_grid) ** len(search_factors))
        note = (note + " " if note else "") + f"已自动降采样搜索网格（组合数={combos}）。"

    rows = []
    best = None
    best_score = -1e18

    for th in threshold_grid:
        for ws in itertools.product(weight_grid, repeat=len(search_factors)):
            weights = {name: 1.0 for name in selected_factors}
            for idx, name in enumerate(search_factors):
                weights[name] = float(ws[idx])

            _, stats = run_factor_strategy(raw_df, selected_factors, weights, th)
            score = float(stats["sharpe"]) + 0.2 * float(stats["ann_return"]) - 0.5 * abs(float(stats["max_drawdown"]))

            row = {
                "threshold": th,
                "score": score,
                "ann_return": stats["ann_return"],
                "sharpe": stats["sharpe"],
                "max_drawdown": stats["max_drawdown"],
            }
            for name in selected_factors:
                row[f"w_{name}"] = weights[name]
            rows.append(row)

            if score > best_score:
                best_score = score
                best = {
                    "threshold": th,
                    "weights": weights,
                    "score": score,
                    "ann_return": stats["ann_return"],
                    "sharpe": stats["sharpe"],
                    "max_drawdown": stats["max_drawdown"],
                }

    res_df = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    return best or {}, res_df.head(20), note


def _fast_optimize_params(train_df: pd.DataFrame, selected_factors: list[str]) -> dict:
    factors = list(selected_factors or ["momentum_20"])
    if len(factors) > 4:
        factors = factors[:4]

    threshold_grid = [-0.1, 0.0, 0.1]
    weight_grid = [0.5, 1.0, 1.5]

    best = None
    best_score = -1e18
    for th in threshold_grid:
        for ws in itertools.product(weight_grid, repeat=len(factors)):
            weights = {name: 1.0 for name in selected_factors}
            for idx, name in enumerate(factors):
                weights[name] = float(ws[idx])

            _, stats = run_factor_strategy(train_df, selected_factors, weights, th)
            score = float(stats["sharpe"]) + 0.2 * float(stats["ann_return"]) - 0.5 * abs(float(stats["max_drawdown"]))
            if score > best_score:
                best_score = score
                best = {"threshold": th, "weights": weights, "score": score, "train_stats": stats}

    return best or {"threshold": 0.1, "weights": {name: 1.0 for name in selected_factors}, "score": -1e18, "train_stats": {}}


def run_walk_forward(
    raw_df: pd.DataFrame,
    selected_factors: list[str],
    train_window: int,
    test_window: int,
    step: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    n = len(raw_df)
    if n < train_window + test_window:
        return pd.DataFrame(), pd.DataFrame()

    fold_rows = []
    oos_returns = []

    fold_id = 0
    for start in range(0, n - train_window - test_window + 1, step):
        train_end = start + train_window
        test_end = train_end + test_window

        train_df = raw_df.iloc[start:train_end].copy()
        test_df = raw_df.iloc[train_end:test_end].copy()
        if len(test_df) == 0:
            continue

        best = _fast_optimize_params(train_df, selected_factors)
        threshold = float(best["threshold"])
        weights = best["weights"]

        signal_test = FactorSelectionStrategy(
            threshold=threshold,
            enabled_factors=selected_factors,
            factor_weights=weights,
        ).generate_signals(test_df)
        result_test = BacktestEngine().run(signal_test)

        fold_curve = result_test.equity_curve[["trade_date", "strategy_ret", "benchmark_ret"]].copy()
        oos_returns.append(fold_curve)

        train_start_date = pd.to_datetime(train_df["trade_date"].iloc[0]).strftime("%Y-%m-%d")
        train_end_date = pd.to_datetime(train_df["trade_date"].iloc[-1]).strftime("%Y-%m-%d")
        test_start_date = pd.to_datetime(test_df["trade_date"].iloc[0]).strftime("%Y-%m-%d")
        test_end_date = pd.to_datetime(test_df["trade_date"].iloc[-1]).strftime("%Y-%m-%d")

        active_weights = {k: v for k, v in weights.items() if abs(v) > 1e-9}
        weight_text = ", ".join([f"{factor_display_name(k)}:{v:.1f}" for k, v in active_weights.items()])

        fold_rows.append(
            {
                "折": fold_id,
                "训练区间": f"{train_start_date} ~ {train_end_date}",
                "测试区间": f"{test_start_date} ~ {test_end_date}",
                "最优阈值": threshold,
                "最优权重": weight_text,
                "训练夏普": float(best.get("train_stats", {}).get("sharpe", 0.0)),
                "测试年化": float(result_test.stats["ann_return"]),
                "测试夏普": float(result_test.stats["sharpe"]),
                "测试最大回撤": float(result_test.stats["max_drawdown"]),
                "测试胜率": float(result_test.stats["win_rate"]),
            }
        )
        fold_id += 1

    if not fold_rows or not oos_returns:
        return pd.DataFrame(), pd.DataFrame()

    oos_df = pd.concat(oos_returns, ignore_index=True)
    oos_df = oos_df.sort_values("trade_date").drop_duplicates(subset=["trade_date"], keep="last")
    oos_df["strategy_nav_oos（策略净值-样本外）"] = (1.0 + oos_df["strategy_ret"].fillna(0.0)).cumprod()
    oos_df["benchmark_nav_oos（基准净值-样本外）"] = (1.0 + oos_df["benchmark_ret"].fillna(0.0)).cumprod()

    return pd.DataFrame(fold_rows), oos_df


def main() -> None:
    st.set_page_config(page_title="A股分析框架", layout="wide")

    query_params = st.query_params
    detail_code_q = str(query_params.get("backtest_code", "") or "").strip()
    if detail_code_q:
        detail_name_q = str(query_params.get("backtest_name", "") or "").strip()
        lookback_days_q = int(_to_float(query_params.get("backtest_days", 60), 60))
        threshold_q = float(_to_float(query_params.get("backtest_threshold", 0.1), 0.1))
        factor_text_q = str(query_params.get("backtest_factors", "") or "").strip()
        factors_q = [item.strip() for item in factor_text_q.split(",") if item.strip()] or ["momentum_20"]
        weight_text_q = str(query_params.get("backtest_weights", "") or "").strip()
        try:
            weights_q = json.loads(weight_text_q) if weight_text_q else {}
        except Exception:
            weights_q = {}
        factor_weights_q = {str(k): float(v) for k, v in (weights_q or {}).items()} if isinstance(weights_q, dict) else {}

        st.title(f"{detail_code_q} {detail_name_q or ''} 近{lookback_days_q}日回测")
        data_manager = DataManager()
        detail_end_dt = date.today()
        detail_start_dt = detail_end_dt - timedelta(days=max(30, int(lookback_days_q) * 2))
        detail_start = detail_start_dt.strftime("%Y%m%d")
        detail_end = detail_end_dt.strftime("%Y%m%d")
        try:
            raw_recent = data_manager.get_daily_data(detail_code_q, detail_start, detail_end, use_cache=True)
            signal_recent = FactorSelectionStrategy(
                threshold=threshold_q,
                enabled_factors=factors_q,
                factor_weights=factor_weights_q,
            ).generate_signals(raw_recent.copy())
            result_recent = BacktestEngine().run(signal_recent)
            recent_display = add_action_hints(
                result_recent.equity_curve,
                stop_profit_pct=0.10,
                stop_loss_pct=0.05,
                enable_risk_hints=True,
            )
            recent_display = format_detail_table(recent_display)
            st.caption(f"区间：{detail_start} ~ {detail_end}，明细行数：{len(recent_display)}")
            stats = result_recent.stats
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("年化收益", f"{float(stats.get('ann_return', 0.0)):.2%}")
            c2.metric("夏普", f"{float(stats.get('sharpe', 0.0)):.2f}")
            c3.metric("最大回撤", f"{float(stats.get('max_drawdown', 0.0)):.2%}")
            c4.metric("胜率", f"{float(stats.get('win_rate', 0.0)):.2%}")
            st.dataframe(recent_display, use_container_width=True)
        except Exception as exc:
            st.error(f"加载 {detail_code_q} 近{lookback_days_q}日回测失败：{exc}")
        return

    st.title("A股分析框架（MVP）")
    if not settings.tushare_token:
        st.warning("未检测到 TUSHARE_TOKEN，当前将使用模拟数据（仅用于演示）")

    st.sidebar.header("参数仪表盘")
    if "watchlist_groups" not in st.session_state:
        st.session_state.watchlist_groups = load_watchlist_groups()
    if "holdings_df" not in st.session_state:
        st.session_state["holdings_df"] = load_holdings()
    if "holding_users" not in st.session_state:
        st.session_state["holding_users"] = load_holding_users(st.session_state["holdings_df"])
    if "active_holding_user" not in st.session_state:
        users = st.session_state.get("holding_users", ["默认用户"])
        st.session_state["active_holding_user"] = users[0] if users else "默认用户"
    if "holding_advice_df" not in st.session_state:
        st.session_state["holding_advice_df"] = pd.DataFrame()
    if "smart_pick_df" not in st.session_state:
        st.session_state["smart_pick_df"] = pd.DataFrame()
    if "smart_sector_rotation_df" not in st.session_state:
        st.session_state["smart_sector_rotation_df"] = pd.DataFrame()
    if "smart_sector_rotation_summary" not in st.session_state:
        st.session_state["smart_sector_rotation_summary"] = {}
    if "sector_fund_flow_df_map" not in st.session_state:
        st.session_state["sector_fund_flow_df_map"] = {}
    if "sector_fund_flow_summary_map" not in st.session_state:
        st.session_state["sector_fund_flow_summary_map"] = {}
    if "sector_fund_flow_msg" not in st.session_state:
        st.session_state["sector_fund_flow_msg"] = ""
    if "smart_macro_regime" not in st.session_state:
        st.session_state["smart_macro_regime"] = {}
    if "smart_macro_detail_df" not in st.session_state:
        st.session_state["smart_macro_detail_df"] = pd.DataFrame()
    if "smart_recent_entry_signal_df" not in st.session_state:
        st.session_state["smart_recent_entry_signal_df"] = pd.DataFrame()
    if "ma5_pullback_df" not in st.session_state:
        st.session_state["ma5_pullback_df"] = pd.DataFrame()
    if "dragon_radar_df" not in st.session_state:
        st.session_state["dragon_radar_df"] = pd.DataFrame()
    if "today_entry_df" not in st.session_state:
        st.session_state["today_entry_df"] = pd.DataFrame()
    if "today_entry_summary" not in st.session_state:
        st.session_state["today_entry_summary"] = {}
    if "right_side_watch_df" not in st.session_state:
        st.session_state["right_side_watch_df"] = pd.DataFrame()
    if "rebound_entry_df" not in st.session_state:
        st.session_state["rebound_entry_df"] = pd.DataFrame()
    if "rebound_entry_summary" not in st.session_state:
        st.session_state["rebound_entry_summary"] = {}
    if "rebound_watch_df" not in st.session_state:
        st.session_state["rebound_watch_df"] = pd.DataFrame()
    if "smart_pick_last_refresh_tag" not in st.session_state:
        st.session_state["smart_pick_last_refresh_tag"] = ""
    if "sector_fund_flow_last_refresh_tag" not in st.session_state:
        st.session_state["sector_fund_flow_last_refresh_tag"] = ""
    if "dragon_radar_last_refresh_tag" not in st.session_state:
        st.session_state["dragon_radar_last_refresh_tag"] = ""
    if "ma5_pullback_last_refresh_tag" not in st.session_state:
        st.session_state["ma5_pullback_last_refresh_tag"] = ""

    data_manager = DataManager()

    @st.fragment(run_every="15s")
    def render_background_status_fragment() -> None:
        worker_summary, worker_task_df = _background_status_panel()
        worker_progress_log_df = _background_progress_log_panel(limit=80)
        with st.expander("后台状态面板", expanded=False):
            s1, s2, s3, s4, s5, s6 = st.columns(6)
            s1.metric("Worker状态", str(worker_summary.get("状态", "") or "-"))
            s2.metric("健康度", str(worker_summary.get("健康", "") or "-"))
            s3.metric("当前任务", str(worker_summary.get("当前任务", "") or "-"))
            s4.metric("运行并发", int(worker_summary.get("并发数", 0)))
            s5.metric("最大并发", int(worker_summary.get("最大并发", 0)))
            s6.metric("心跳", str(worker_summary.get("心跳时间", "") or "-"))
            status_caption = "；".join(
                [
                    f"全市场60日数据库: {_task_latest_success_time('market_db_sync') or '-'}",
                    f"板块资金流: {_task_latest_success_time('sector_fund_flow') or '-'}",
                    f"科学选股: {_task_latest_success_time('smart_pick') or '-'}",
                    f"龙头雷达: {_task_latest_success_time('dragon_radar') or '-'}",
                    f"5日线回踩: {_task_latest_success_time('ma5_pullback') or '-'}",
                    f"当日建仓: {_task_latest_success_time('today_entry') or '-'}",
                    f"副策略建仓: {_task_latest_success_time('rebound_entry') or '-'}",
                    f"持仓建议: {_task_latest_success_time('holding_advice') or '-'}",
                ]
            )
            st.caption(f"各任务最近成功时间：{status_caption}")
            if worker_task_df is not None and not worker_task_df.empty:
                st.dataframe(worker_task_df, use_container_width=True)
            st.caption("任务进度日志（滚动保留最近600条）")
            if worker_progress_log_df is not None and not worker_progress_log_df.empty:
                st.dataframe(worker_progress_log_df, use_container_width=True, height=280)
            else:
                st.caption("暂无进度日志。")

    render_background_status_fragment()

    with st.sidebar.expander("基础参数（Basic）", expanded=True):
        all_codes = flatten_watchlist(st.session_state.watchlist_groups)
        if not all_codes:
            st.session_state.watchlist_groups = {k: list(v) for k, v in DEFAULT_GROUPS.items()}
            all_codes = flatten_watchlist(st.session_state.watchlist_groups)

        stock_basic_df = data_manager.get_stock_basic(use_cache=True)
        name_map = {}
        if stock_basic_df is not None and not stock_basic_df.empty:
            tmp = stock_basic_df.copy()
            if "ts_code" in tmp.columns and "name" in tmp.columns:
                for _, row in tmp.iterrows():
                    code = str(row.get("ts_code", "")).strip()
                    name = str(row.get("name", "")).strip()
                    if code:
                        name_map[code] = name

        def display_code(code: str) -> str:
            stock_name = name_map.get(code, "")
            return f"{code}（{stock_name}）" if stock_name else code

        group_names = sorted(st.session_state.watchlist_groups.keys())
        selected_group = st.selectbox("自选分组", options=group_names, index=0)
        group_codes = st.session_state.watchlist_groups.get(selected_group, [])
        if not group_codes:
            group_codes = all_codes

        filtered_codes = list(group_codes)

        display_options = [display_code(code) for code in filtered_codes]
        label_to_code = {display_code(code): code for code in filtered_codes}

        ts_label = st.selectbox(
            "股票代码（可搜索）",
            options=display_options,
            index=0,
            help="支持按代码/名称检索，并支持分组自选",
        )
        ts_code = label_to_code[ts_label]

        st.caption("添加自选：输入股票代码")
        new_code_raw = st.text_input("手动输入代码", value="", help="支持格式：600986.SH、600986 SH、sh600986、600986")

        c_add, c_del = st.columns(2)
        if c_add.button("加入自选"):
            try:
                new_code = normalize_ts_code(new_code_raw)

                group_items = st.session_state.watchlist_groups.get(selected_group, [])
                if new_code not in group_items:
                    group_items.append(new_code)
                    st.session_state.watchlist_groups[selected_group] = sorted(set(group_items))
                    save_watchlist_groups(st.session_state.watchlist_groups)
                    st.success(f"已加入：{new_code}")
                else:
                    st.info(f"已在分组【{selected_group}】中：{new_code}")
            except ValueError as exc:
                st.error(str(exc))

        if c_del.button("移除当前"):
            group_items = st.session_state.watchlist_groups.get(selected_group, [])
            if ts_code in group_items and len(group_items) > 0:
                group_items.remove(ts_code)
                st.session_state.watchlist_groups[selected_group] = sorted(set(group_items))
                if not flatten_watchlist(st.session_state.watchlist_groups):
                    st.session_state.watchlist_groups = {k: list(v) for k, v in DEFAULT_GROUPS.items()}
                save_watchlist_groups(st.session_state.watchlist_groups)
                st.success(f"已移除：{ts_code}")
                st.rerun()
            else:
                st.warning("当前分组中没有可移除代码")

        start_dt = st.date_input("开始日期", value=date(2022, 1, 1), format="YYYY/MM/DD")
        end_dt = st.date_input("结束日期", value=date.today(), format="YYYY/MM/DD")
        if start_dt > end_dt:
            st.error("开始日期不能晚于结束日期")
            st.stop()

    start_date = start_dt.strftime("%Y%m%d")
    end_date = end_dt.strftime("%Y%m%d")

    with st.sidebar.expander("策略与因子（Strategy & Factor）", expanded=False):
        strategies = list(build_strategies().keys()) + list(PROFESSIONAL_PLAYBOOKS.keys())
        selected_strategies = st.multiselect("战法对比（可多选）", strategies, default=["因子选股"])
        with st.popover("查看职业操作说明"):
            for name, cfg in PROFESSIONAL_PLAYBOOKS.items():
                st.markdown(f"- **{name}**：{cfg['summary']}")

        available_factor_names = list(ALL_FACTORS)
        factor_display_options = [factor_display_name(name) for name in available_factor_names]
        display_to_factor = {factor_display_name(name): name for name in available_factor_names}

        selected_factors = st.multiselect(
            "因子选股-启用因子（Factor）",
            options=factor_display_options,
            default=[factor_display_name(name) for name in TECHNICAL_FACTORS],
        )
        selected_factor_codes = [display_to_factor[item] for item in selected_factors]
        factor_threshold = st.slider("因子选股-开仓阈值", min_value=-1.0, max_value=1.0, value=0.1, step=0.01)

        st.caption("因子权重（可调）")
        factor_weights: dict[str, float] = {}
        default_weight_map = st.session_state.get("best_weight_map", {})
        for factor_name in (selected_factor_codes or ["momentum_20"]):
            default_v = float(default_weight_map.get(factor_name, 1.0))
            factor_weights[factor_name] = st.slider(
                f"{factor_display_name(factor_name)} 权重",
                min_value=-2.0,
                max_value=2.0,
                value=max(-2.0, min(2.0, default_v)),
                step=0.1,
                key=f"weight_slider_{factor_name}",
            )

    with st.sidebar.expander("回测增强（Advanced）", expanded=False):
        rolling_window_size = st.slider("滚动窗口长度（交易日）", min_value=60, max_value=260, value=120, step=10)
        rolling_step = st.slider("滚动步长（交易日）", min_value=10, max_value=120, value=20, step=5)
        enable_rolling_compare = st.checkbox("启用三阶段滚动窗口对比", value=True)

        enable_walk_forward = st.checkbox("启用 Walk-Forward（训练调参+测试评估）", value=False)
        wf_train_window = st.slider("WFO 训练窗口（交易日）", min_value=80, max_value=320, value=160, step=10)
        wf_test_window = st.slider("WFO 测试窗口（交易日）", min_value=20, max_value=120, value=40, step=5)
        wf_step = st.slider("WFO 滚动步长（交易日）", min_value=10, max_value=120, value=40, step=5)

        st.caption("交易提示参数")
        enable_risk_hints = st.checkbox("启用止盈止损提示", value=True)
        stop_profit_pct = st.slider("止盈阈值", min_value=0.01, max_value=0.50, value=0.10, step=0.01)
        stop_loss_pct = st.slider("止损阈值", min_value=0.01, max_value=0.50, value=0.05, step=0.01)

        run_grid = st.button("一键网格搜索最优参数")

    with st.sidebar.expander("智能选股（Smart Picker）", expanded=False):
        enable_smart_picker = st.checkbox("开启科学选股（趋势+资金+事件）", value=True)
        smart_macro_enabled = st.checkbox("宏观康波增强", value=True)
        st.caption("点击“生成科学选股推荐”将实时拉取并重算（忽略本地缓存）。")
        st.caption(f"自动刷新规则：每天固定 {AUTO_REFRESH_SCHEDULE_LABEL} 触发后台重算。")
        risk_profile = st.selectbox("风险偏好", options=["稳健", "均衡", "激进"], index=1)
        profile_defaults = {
            "稳健": {"total": 40, "single": 6},
            "均衡": {"total": 60, "single": 10},
            "激进": {"total": 80, "single": 15},
        }
        smart_lookback_days = st.slider("回看天数", min_value=20, max_value=240, value=90, step=10)
        smart_universe_size = st.slider("候选池规模", min_value=50, max_value=500, value=200, step=10)
        smart_top_n = st.slider("推荐股票数量", min_value=3, max_value=20, value=8, step=1)
        smart_recent_signal_days = st.slider("建仓信号回看天数", min_value=2, max_value=10, value=5, step=1)
        smart_sector_flow_indicator = st.selectbox("板块资金流周期", options=["当日", "3日", "10日"], index=0)
        st.caption("板块资金流来自 AkShare/Eastmoney；若未安装 akshare 或网络受限，将自动回退到缓存/仅价格轮动。")
        run_sector_fund_flow_refresh = st.button("强制刷新板块资金流")
        smart_total_position = st.slider(
            "建议总仓位%",
            min_value=10,
            max_value=100,
            value=int(profile_defaults[risk_profile]["total"]),
            step=5,
        )
        smart_single_cap = st.slider(
            "单票仓位上限%",
            min_value=3,
            max_value=30,
            value=int(profile_defaults[risk_profile]["single"]),
            step=1,
        )
        run_smart_pick = st.button("生成科学选股推荐")
        enable_ma5_pullback = st.checkbox("开启5日线回踩推荐", value=True)
        ma5_pullback_top_n = st.slider("回踩推荐数量", min_value=3, max_value=20, value=8, step=1)
        ma5_pullback_universe_size = st.slider("回踩候选池规模", min_value=80, max_value=500, value=220, step=10)
        ma5_pullback_tolerance = st.slider("回踩5日线容差", min_value=0.005, max_value=0.030, value=0.015, step=0.001)
        ma5_pullback_min_factor = st.slider("最小因子表现阈值", min_value=-0.5, max_value=1.0, value=0.05, step=0.01)
        run_ma5_pullback = st.button("生成5日线回踩推荐")
        run_today_entry = st.button("生成当日建议建仓")
        run_rebound_entry = st.button("生成副策略建仓")

    with st.sidebar.expander("龙头雷达（消息面+MACD）", expanded=False):
        enable_dragon_radar = st.checkbox("开启龙头雷达", value=False)
        st.caption(f"自动刷新规则：每天固定 {AUTO_REFRESH_SCHEDULE_LABEL} 触发后台重算。")
        dragon_lookback_days = st.slider("龙头雷达-回看天数", min_value=40, max_value=260, value=120, step=10)
        dragon_universe_size = st.slider("龙头雷达-候选池规模", min_value=50, max_value=500, value=220, step=10)
        dragon_top_n = st.slider("龙头雷达-推荐数量", min_value=3, max_value=20, value=8, step=1)
        dragon_gap_threshold = st.slider("放量缺口阈值", min_value=0.003, max_value=0.03, value=0.01, step=0.001)
        run_dragon_radar = st.button("生成龙头黄金买卖点")

    with st.sidebar.expander("盘中增量信号（Intraday）", expanded=False):
        enable_intraday_incremental = st.checkbox("启用盘中增量因子信号", value=True)
        intraday_trade_date = st.date_input("盘中交易日", value=end_dt, format="YYYY/MM/DD")
        intraday_lookback_days = st.slider("盘中计算-日线回看天数", min_value=60, max_value=400, value=220, step=20)
        intraday_source = st.selectbox(
            "分钟数据来源",
            options=["自动拉取(AkShare免费)", "自动拉取(Tushare)", "手动上传CSV"],
            index=0,
        )
        intraday_freq = st.selectbox("分钟频率", options=["1min", "5min", "15min", "30min", "60min"], index=0)
        intraday_force_refresh = False
        minute_csv_file = None
        minute_csv_text = ""
        if intraday_source == "手动上传CSV":
            minute_csv_file = st.file_uploader("上传分钟CSV", type=["csv"], key="intraday_minute_csv")
            minute_csv_text = st.text_area(
                "或粘贴分钟CSV文本",
                value="",
                height=100,
                help="至少包含时间列(trade_time/datetime/time)与close；建议含open/high/low/vol/amount",
            )
        else:
            if intraday_source == "自动拉取(AkShare免费)":
                st.caption("自动拉取将使用 AkShare 免费分钟源。")
            else:
                st.caption("自动拉取将使用 Tushare 分钟接口，注意该接口存在每分钟频控。")
            intraday_force_refresh = st.checkbox("强制刷新（忽略缓存）", value=False)
        run_intraday_incremental = st.button("运行盘中增量信号")

    st.subheader("首页持仓与当日交易动作建议")
    st.caption(
        f"录入当前持仓后，可一键生成当日动作建议；收益率会按 AkShare 当前价自动更新。"
        f"为避免频控，系统按 {HOLDING_FORCE_REFRESH_INTERVAL_SECONDS} 秒节流拉新，间隔内使用缓存并展示行情时间。"
    )

    with st.expander("持仓录入", expanded=True):
        holdings_all = st.session_state.get("holdings_df", pd.DataFrame())
        users_from_data = st.session_state.get("holding_users", ["默认用户"])
        if holdings_all is not None and not holdings_all.empty and "user_name" in holdings_all.columns:
            users_from_data = sorted(
                set(users_from_data)
                | set(holdings_all["user_name"].astype(str).str.strip().replace("", "默认用户").tolist())
            )
        st.session_state["holding_users"] = users_from_data

        uh1, uh2, uh3 = st.columns([1.2, 1.2, 1.2])
        if not users_from_data:
            users_from_data = ["默认用户"]
        current_user = str(st.session_state.get("active_holding_user", "默认用户") or "默认用户").strip() or "默认用户"
        if current_user not in users_from_data:
            users_from_data = sorted(set(users_from_data + [current_user]))
        selected_user_index = users_from_data.index(current_user) if current_user in users_from_data else 0

        active_user = uh1.selectbox(
            "持仓用户",
            options=users_from_data,
            index=selected_user_index,
        )
        st.session_state["active_holding_user"] = active_user

        new_user_name = uh2.text_input("新建用户", value="", placeholder="例如：张三")
        if uh2.button("创建并切换用户"):
            norm_user = str(new_user_name or "").strip()
            if not norm_user:
                st.warning("请输入用户名称。")
            elif norm_user in users_from_data:
                st.session_state["active_holding_user"] = norm_user
                st.info(f"已切换到用户：{norm_user}")
                st.rerun()
            else:
                updated_users = sorted(set(users_from_data + [norm_user]))
                st.session_state["holding_users"] = updated_users
                save_holding_users(updated_users)
                st.session_state["active_holding_user"] = norm_user
                st.success(f"已创建并切换用户：{norm_user}")
                st.rerun()

        delete_user_name = uh3.selectbox(
            "删除用户",
            options=users_from_data,
            index=selected_user_index if selected_user_index < len(users_from_data) else 0,
            key="delete_user_select",
        )
        if uh3.button("删除用户"):
            norm_delete_user = str(delete_user_name or "").strip()
            if not norm_delete_user:
                st.warning("请选择要删除的用户。")
            elif norm_delete_user == "默认用户":
                st.warning("默认用户不允许删除。")
            else:
                cur_holdings = st.session_state.get("holdings_df", pd.DataFrame())
                user_has_holdings = False
                if cur_holdings is not None and not cur_holdings.empty:
                    chk = cur_holdings.copy()
                    if "user_name" not in chk.columns:
                        chk["user_name"] = "默认用户"
                    chk["user_name"] = chk["user_name"].astype(str).str.strip().replace("", "默认用户")
                    user_has_holdings = bool((chk["user_name"] == norm_delete_user).any())

                if user_has_holdings:
                    st.warning(f"用户【{norm_delete_user}】仍有持仓，无法删除。请先清空持仓。")
                else:
                    cur_users = st.session_state.get("holding_users", ["默认用户"])
                    updated_users = sorted(set([u for u in cur_users if str(u).strip() and str(u).strip() != norm_delete_user]))
                    if "默认用户" not in updated_users:
                        updated_users = ["默认用户"] + updated_users

                    st.session_state["holding_users"] = updated_users
                    if st.session_state.get("active_holding_user", "默认用户") == norm_delete_user:
                        st.session_state["active_holding_user"] = updated_users[0] if updated_users else "默认用户"

                    save_holding_users(updated_users)
                    st.success(f"已删除用户：{norm_delete_user}")
                    st.rerun()

        h1, h2, h3, h4 = st.columns([1.8, 1.4, 1.0, 1.0])
        hold_code_raw = h1.text_input("股票代码", value="", placeholder="如 600986.SH")
        hold_name = h2.text_input("股票名称", value="", placeholder="可选")
        hold_shares = h3.number_input("持股数", min_value=0.0, value=0.0, step=100.0)
        hold_buy_price = h4.number_input("买入价", min_value=0.0, value=0.0, step=0.01, format="%.3f")

        c_add_h, _ = st.columns(2)
        if c_add_h.button("新增/更新持仓"):
            try:
                norm_code = normalize_ts_code(hold_code_raw)
                if hold_shares <= 0 or hold_buy_price <= 0:
                    st.warning("请填写有效的持股数和买入价。")
                else:
                    live_price = np.nan
                    try:
                        qdf = data_manager.get_realtime_prices_akshare(
                            ts_codes=[norm_code],
                            refresh_seconds=HOLDING_QUOTE_REFRESH_SECONDS,
                            force_refresh=True,
                        )
                        if qdf is not None and not qdf.empty:
                            live_price = float(qdf.iloc[0].get("realtime_price", 0) or 0)
                    except Exception:
                        live_price = np.nan

                    if not np.isfinite(live_price) or live_price <= 0:
                        try:
                            today_str = pd.Timestamp.now().strftime("%Y%m%d")
                            hist_start = (pd.Timestamp.now() - pd.Timedelta(days=20)).strftime("%Y%m%d")
                            daily_df = data_manager.get_daily_data(
                                norm_code,
                                hist_start,
                                today_str,
                                use_cache=False,
                            )
                            if daily_df is not None and not daily_df.empty:
                                live_price = float(daily_df.sort_values("trade_date").iloc[-1].get("close", 0) or 0)
                        except Exception:
                            live_price = np.nan

                    if np.isfinite(live_price) and live_price > 0:
                        auto_return_pct = (float(live_price) / float(hold_buy_price) - 1.0) * 100.0
                    else:
                        auto_return_pct = 0.0

                    hdf = st.session_state["holdings_df"].copy()
                    row = {
                        "user_name": st.session_state.get("active_holding_user", "默认用户"),
                        "ts_code": norm_code,
                        "name": str(hold_name).strip(),
                        "shares": float(hold_shares),
                        "buy_price": float(hold_buy_price),
                        "manual_return_pct": float(auto_return_pct),
                    }
                    has_target = (
                        (hdf["ts_code"] == norm_code)
                        & (hdf["user_name"].astype(str) == row["user_name"])
                    ) if not hdf.empty else pd.Series(dtype=bool)
                    if not hdf.empty and has_target.any():
                        hdf.loc[has_target, ["name", "shares", "buy_price", "manual_return_pct"]] = [
                            row["name"],
                            row["shares"],
                            row["buy_price"],
                            row["manual_return_pct"],
                        ]
                    else:
                        hdf = pd.concat([hdf, pd.DataFrame([row])], ignore_index=True)
                    st.session_state["holdings_df"] = hdf
                    save_holdings(hdf)
                    cur_users = st.session_state.get("holding_users", ["默认用户"])
                    if row["user_name"] not in cur_users:
                        cur_users = sorted(set(cur_users + [row["user_name"]]))
                        st.session_state["holding_users"] = cur_users
                        save_holding_users(cur_users)
                    if np.isfinite(live_price) and live_price > 0:
                        st.success(
                            f"已保存持仓：{row['user_name']} / {norm_code}，"
                            f"当前价 {live_price:.3f}，收益 {auto_return_pct:.2f}%"
                        )
                    else:
                        st.warning(
                            f"已保存持仓：{row['user_name']} / {norm_code}，"
                            "当前价暂不可用，收益先按 0.00% 保存。"
                        )
            except ValueError as exc:
                st.error(str(exc))

    holdings_df = st.session_state.get("holdings_df", pd.DataFrame())
    active_user = st.session_state.get("active_holding_user", "默认用户")
    user_holdings_df = pd.DataFrame()
    if holdings_df is not None and not holdings_df.empty:
        base_hdf = holdings_df.copy()
        if "user_name" not in base_hdf.columns:
            base_hdf["user_name"] = "默认用户"
        base_hdf["user_name"] = base_hdf["user_name"].astype(str).str.strip().replace("", "默认用户")
        user_holdings_df = base_hdf[base_hdf["user_name"] == active_user].reset_index(drop=False)

    if user_holdings_df is not None and not user_holdings_df.empty:
        show_hold = user_holdings_df.rename(
            columns={
                "user_name": "用户",
                "ts_code": "股票代码",
                "name": "股票名称",
                "shares": "持股数",
                "buy_price": "买入价",
                "manual_return_pct": "自动收益%",
            }
        )
        st.dataframe(
            show_hold[["用户", "股票代码", "股票名称", "持股数", "买入价", "自动收益%"]].style.format(
                {"持股数": "{:.0f}", "买入价": "{:.3f}", "自动收益%": "{:.2f}%"}
            ),
            use_container_width=True,
        )

        st.markdown("#### 行内卖出（按用户持仓）")
        st.caption("输入卖出数量和卖出金额后点击“卖出”。系统会按卖出金额回写剩余持仓成本，并更新该条收益率。")

        for _, row in user_holdings_df.iterrows():
            global_idx = int(row.get("index"))
            code = str(row.get("ts_code", "")).strip()
            name = str(row.get("name", "")).strip()
            shares = float(row.get("shares", 0) or 0)
            buy_price = float(row.get("buy_price", 0) or 0)

            if shares <= 0 or buy_price <= 0 or not code:
                continue

            s1, s2, s3, s4, s5 = st.columns([2.2, 1.2, 1.2, 1.2, 0.9])
            s1.write(f"{code} {name or ''} | 持股 {shares:.0f} | 成本 {buy_price:.3f}")
            sell_qty = s2.number_input(
                "卖出数量",
                min_value=0.0,
                max_value=float(shares),
                value=0.0,
                step=100.0,
                key=f"sell_qty_{active_user}_{global_idx}",
            )
            sell_amount = s3.number_input(
                "卖出金额",
                min_value=0.0,
                value=0.0,
                step=100.0,
                key=f"sell_amt_{active_user}_{global_idx}",
            )
            implied_sell_price = float(sell_amount / sell_qty) if sell_qty > 0 else 0.0
            s4.write(f"卖出价 {implied_sell_price:.3f}" if implied_sell_price > 0 else "卖出价 -")

            if s5.button("卖出", key=f"sell_btn_{active_user}_{global_idx}"):
                if sell_qty <= 0 or sell_amount <= 0:
                    st.warning("请填写有效的卖出数量和卖出金额。")
                elif sell_qty > shares:
                    st.warning("卖出数量不能超过当前持股数。")
                else:
                    hdf = st.session_state.get("holdings_df", pd.DataFrame()).copy()
                    if hdf.empty or global_idx not in hdf.index:
                        st.warning("持仓索引已变化，请重试。")
                        st.rerun()

                    cur_shares = float(hdf.loc[global_idx, "shares"])
                    cur_buy = float(hdf.loc[global_idx, "buy_price"])
                    remain_shares = cur_shares - float(sell_qty)
                    realized_amount = float(sell_amount - sell_qty * cur_buy)

                    if remain_shares <= 0:
                        hdf = hdf.drop(index=global_idx).reset_index(drop=True)
                        st.success(f"{active_user} / {code} 已全部卖出，已从持仓中移除。")
                    else:
                        total_cost_before = cur_shares * cur_buy
                        remain_cost = total_cost_before - float(sell_amount)
                        new_buy_price = max(0.0, remain_cost / remain_shares)
                        new_return_pct = ((float(sell_amount) / (float(sell_qty) * cur_buy)) - 1.0) * 100.0

                        hdf.loc[global_idx, "shares"] = remain_shares
                        hdf.loc[global_idx, "buy_price"] = new_buy_price
                        hdf.loc[global_idx, "manual_return_pct"] = new_return_pct
                        st.success(
                            f"{active_user} / {code} 卖出成功：实现盈亏 {realized_amount:.2f}，"
                            f"剩余持股 {remain_shares:.0f}，新成本 {new_buy_price:.3f}"
                        )

                    st.session_state["holdings_df"] = hdf
                    save_holdings(hdf)
                    st.rerun()
    else:
        st.info(f"用户【{active_user}】暂无持仓，请先在上方录入。")

    run_holding_advice = st.button("生成持仓当日交易动作建议")
    if run_holding_advice:
        all_hdf = st.session_state.get("holdings_df", pd.DataFrame())
        if all_hdf is None or all_hdf.empty:
            st.warning("请先录入持仓。")
        else:
            st.session_state["holding_advice_msg"] = _enqueue_manual_refresh_once(
                task_name="holding_advice",
                label="持仓建议",
                payload={
                    "end_date": end_date,
                    "threshold": float(st.session_state.get("best_threshold", factor_threshold)),
                    "enabled_factors": selected_factor_codes if selected_factor_codes else ["momentum_20"],
                    "factor_weights": factor_weights,
                    "stop_profit_pct": float(stop_profit_pct),
                    "stop_loss_pct": float(stop_loss_pct),
                },
                queued_message="已提交后台持仓建议刷新任务，请稍后自动加载结果。",
            )

    holding_snapshot = read_snapshot("holding_advice")
    if isinstance(holding_snapshot, dict):
        status = str(holding_snapshot.get("status", "") or "")
        generated_at = str(holding_snapshot.get("generated_at", "") or "")
        if status == "ok":
            advice_df = _snapshot_payload_df(holding_snapshot, "holding_advice_df")
            if not advice_df.empty and "user_name" in advice_df.columns:
                advice_df = advice_df[advice_df["user_name"].astype(str) == str(active_user)].reset_index(drop=True)
            st.session_state["holding_advice_df"] = advice_df

            updated_holdings_df = _snapshot_payload_df(holding_snapshot, "updated_holdings_df")
            if not updated_holdings_df.empty:
                st.session_state["holdings_df"] = updated_holdings_df

            advice_attrs = _snapshot_payload_dict(holding_snapshot, "advice_attrs")
            source_stats = advice_attrs.get("quote_source_stats", {}) if isinstance(advice_attrs, dict) else {}
            source_text = "，".join([f"{k}:{v}" for k, v in source_stats.items()]) if source_stats else "无"
            st.session_state["holding_advice_msg"] = (
                f"持仓建议已更新（后台预计算完成：{generated_at or '未知时间'}，行情来源分布：{source_text}）"
            )
        elif status == "error":
            err = str(holding_snapshot.get("error", "") or "")
            st.session_state["holding_advice_msg"] = f"持仓建议后台任务失败：{err}"

    holding_runtime_msg = _background_status_message(
        "holding_advice",
        "持仓建议",
        holding_snapshot if isinstance(holding_snapshot, dict) else {},
        SCHEDULED_REFRESH_STALE_SECONDS,
    )
    if holding_runtime_msg:
        st.session_state["holding_advice_msg"] = holding_runtime_msg

    if st.session_state.get("holding_advice_msg"):
        st.info(st.session_state.get("holding_advice_msg"))

    advice_show = st.session_state.get("holding_advice_df", pd.DataFrame())
    if advice_show is not None and not advice_show.empty:
        st.dataframe(
            advice_show.style.format(
                {
                    "持股数": "{:.0f}",
                    "买入价": "{:.3f}",
                    "更新后收益%": "{:.2f}%",
                    "最新收盘": "{:.3f}",
                    "当前价": "{:.3f}",
                    "浮动收益%": "{:.2f}%",
                    "浮盈亏": "{:.2f}",
                    "因子得分": "{:.3f}",
                }
            ),
            use_container_width=True,
        )

    if run_grid:
        try:
            raw_for_search = data_manager.get_daily_data(ts_code, start_date, end_date, use_cache=True)
            best, top_df, note = grid_search_factor_params(raw_for_search, selected_factor_codes or ["momentum_20"])
            rename_cols = {}
            for col in top_df.columns:
                if col.startswith("w_"):
                    code = col[2:]
                    rename_cols[col] = f"w_{factor_display_name(code)}"
                elif col == "threshold":
                    rename_cols[col] = "threshold（阈值）"
                elif col == "score":
                    rename_cols[col] = "score（评分）"
                elif col == "ann_return":
                    rename_cols[col] = "ann_return（年化）"
                elif col == "sharpe":
                    rename_cols[col] = "sharpe（夏普）"
                elif col == "max_drawdown":
                    rename_cols[col] = "max_drawdown（最大回撤）"
            top_df = top_df.rename(columns=rename_cols)
            st.session_state["best_threshold"] = float(best.get("threshold", factor_threshold))
            st.session_state["best_weight_map"] = best.get("weights", {})
            st.session_state["grid_top_df"] = top_df
            st.session_state["grid_note"] = note
            st.sidebar.success("网格搜索完成，已将最优参数写入当前会话。")
        except Exception as exc:
            st.sidebar.error(f"网格搜索失败：{exc}")

    if "best_threshold" in st.session_state:
        st.info(
            f"当前最优参数（会话内）：threshold={st.session_state['best_threshold']:.2f}，"
            f"可在侧栏滑条继续微调。"
        )
    if "grid_note" in st.session_state and st.session_state["grid_note"]:
        st.caption(st.session_state["grid_note"])
    if "grid_top_df" in st.session_state:
        st.subheader("网格搜索 Top20（参数组合）")
        st.dataframe(
            st.session_state["grid_top_df"].style.format(
                {
                    "score（评分）": "{:.3f}",
                    "ann_return（年化）": "{:.2%}",
                    "max_drawdown（最大回撤）": "{:.2%}",
                    "sharpe（夏普）": "{:.2f}",
                }
            ),
            use_container_width=True,
        )

    now_for_realtime = pd.Timestamp.now()
    sector_flow_refresh_tag = _build_auto_refresh_tag(now_for_realtime, SECTOR_FUND_FLOW_AUTO_REFRESH_SECONDS)
    smart_refresh_tag = _build_auto_refresh_tag(now_for_realtime, SMART_PICK_AUTO_REFRESH_SECONDS)
    dragon_refresh_tag = _build_auto_refresh_tag(now_for_realtime, DRAGON_RADAR_AUTO_REFRESH_SECONDS)
    ma5_pullback_refresh_tag = _build_auto_refresh_tag(now_for_realtime, MA5_PULLBACK_AUTO_REFRESH_SECONDS)

    sector_flow_snapshot_for_auto = read_snapshot("sector_fund_flow") if enable_smart_picker else None
    smart_snapshot_for_auto = read_snapshot("smart_pick") if enable_smart_picker else None
    dragon_snapshot_for_auto = read_snapshot("dragon_radar") if enable_dragon_radar else None
    ma5_snapshot_for_auto = read_snapshot("ma5_pullback") if (enable_smart_picker and bool(enable_ma5_pullback)) else None

    sector_flow_auto_refresh = False
    if enable_smart_picker and (not run_sector_fund_flow_refresh):
        last_tag = str(st.session_state.get("sector_fund_flow_last_refresh_tag", "") or "")
        sector_flow_auto_refresh = (last_tag != sector_flow_refresh_tag) and (not _snapshot_covers_refresh_slot(sector_flow_snapshot_for_auto, now_for_realtime))

    smart_auto_refresh = False
    if enable_smart_picker and (not run_smart_pick):
        last_tag = str(st.session_state.get("smart_pick_last_refresh_tag", "") or "")
        smart_auto_refresh = (last_tag != smart_refresh_tag) and (not _snapshot_covers_refresh_slot(smart_snapshot_for_auto, now_for_realtime))

    dragon_auto_refresh = False
    if enable_dragon_radar and (not run_dragon_radar):
        last_tag = str(st.session_state.get("dragon_radar_last_refresh_tag", "") or "")
        dragon_auto_refresh = (last_tag != dragon_refresh_tag) and (not _snapshot_covers_refresh_slot(dragon_snapshot_for_auto, now_for_realtime))

    ma5_pullback_auto_refresh = False
    if enable_smart_picker and bool(enable_ma5_pullback) and (not run_ma5_pullback):
        last_tag = str(st.session_state.get("ma5_pullback_last_refresh_tag", "") or "")
        ma5_pullback_auto_refresh = (last_tag != ma5_pullback_refresh_tag) and (not _snapshot_covers_refresh_slot(ma5_snapshot_for_auto, now_for_realtime))

    smart_request_payload = {
        "end_date": _resolve_realtime_end_date(end_dt, now_for_realtime),
        "lookback_days": int(smart_lookback_days),
        "top_n": int(smart_top_n),
        "total_position_pct": float(smart_total_position),
        "universe_size": int(smart_universe_size),
        "max_single_position_pct": float(smart_single_cap),
        "sector_flow_indicator": str(smart_sector_flow_indicator),
        "macro_enabled": bool(smart_macro_enabled),
        "recent_signal_days": int(smart_recent_signal_days),
        "threshold": float(st.session_state.get("best_threshold", factor_threshold)),
        "enabled_factors": selected_factor_codes if selected_factor_codes else ["momentum_20"],
        "factor_weights": factor_weights,
    }
    sector_flow_request_payload = {
        "sector_type": "行业资金流",
        "indicators": ["当日", "3日", "10日"],
    }

    if enable_smart_picker and run_sector_fund_flow_refresh:
        st.session_state["sector_fund_flow_msg"] = _enqueue_manual_refresh_once(
            task_name="sector_fund_flow",
            label="板块资金流",
            payload=sector_flow_request_payload,
            queued_message="已提交板块资金流后台强制刷新任务，请稍后自动加载结果。",
        )
    elif enable_smart_picker and sector_flow_auto_refresh and _can_auto_enqueue("sector_fund_flow", SECTOR_FUND_FLOW_AUTO_REFRESH_SECONDS * 3):
        request_refresh("sector_fund_flow", payload=sector_flow_request_payload, force=False)
        st.session_state["sector_fund_flow_last_refresh_tag"] = sector_flow_refresh_tag

    if enable_smart_picker and run_smart_pick:
        st.session_state["smart_pick_msg"] = _enqueue_manual_refresh_once(
            task_name="smart_pick",
            label="科学选股",
            payload=smart_request_payload,
            queued_message="已提交科学选股后台刷新任务，请稍后自动加载结果。",
        )
    elif enable_smart_picker and smart_auto_refresh and _can_auto_enqueue("smart_pick", SMART_PICK_AUTO_REFRESH_SECONDS * 3):
        request_refresh("smart_pick", payload=smart_request_payload, force=False)
        st.session_state["smart_pick_last_refresh_tag"] = smart_refresh_tag

    if enable_dragon_radar and run_dragon_radar:
        st.session_state["dragon_radar_msg"] = _enqueue_manual_refresh_once(
            task_name="dragon_radar",
            label="龙头雷达",
            payload={
                "end_date": _resolve_realtime_end_date(end_dt, now_for_realtime),
                "lookback_days": int(dragon_lookback_days),
                "top_n": int(dragon_top_n),
                "universe_size": int(dragon_universe_size),
                "gap_threshold": float(dragon_gap_threshold),
            },
            queued_message="已提交龙头雷达后台刷新任务，请稍后自动加载结果。",
        )
    elif enable_dragon_radar and dragon_auto_refresh and _can_auto_enqueue("dragon_radar", DRAGON_RADAR_AUTO_REFRESH_SECONDS * 3):
        request_refresh(
            "dragon_radar",
            payload={
                "end_date": _resolve_realtime_end_date(end_dt, now_for_realtime),
                "lookback_days": int(dragon_lookback_days),
                "top_n": int(dragon_top_n),
                "universe_size": int(dragon_universe_size),
                "gap_threshold": float(dragon_gap_threshold),
            },
            force=False,
        )
        st.session_state["dragon_radar_last_refresh_tag"] = dragon_refresh_tag

    if enable_smart_picker and bool(enable_ma5_pullback) and run_ma5_pullback:
        st.session_state["ma5_pullback_msg"] = _enqueue_manual_refresh_once(
            task_name="ma5_pullback",
            label="5日线回踩",
            payload={
                "end_date": _resolve_realtime_end_date(end_dt, now_for_realtime),
                "lookback_days": int(max(90, smart_lookback_days)),
                "top_n": int(ma5_pullback_top_n),
                "universe_size": int(ma5_pullback_universe_size),
                "pullback_tolerance": float(ma5_pullback_tolerance),
                "min_factor_score": float(ma5_pullback_min_factor),
            },
            queued_message="已提交5日线回踩后台刷新任务，请稍后自动加载结果。",
        )
    elif enable_smart_picker and bool(enable_ma5_pullback) and ma5_pullback_auto_refresh and _can_auto_enqueue("ma5_pullback", MA5_PULLBACK_AUTO_REFRESH_SECONDS * 3):
        request_refresh(
            "ma5_pullback",
            payload={
                "end_date": _resolve_realtime_end_date(end_dt, now_for_realtime),
                "lookback_days": int(max(90, smart_lookback_days)),
                "top_n": int(ma5_pullback_top_n),
                "universe_size": int(ma5_pullback_universe_size),
                "pullback_tolerance": float(ma5_pullback_tolerance),
                "min_factor_score": float(ma5_pullback_min_factor),
            },
            force=False,
        )
        st.session_state["ma5_pullback_last_refresh_tag"] = ma5_pullback_refresh_tag

    if run_today_entry and enable_smart_picker:
        st.session_state["today_entry_msg"] = _enqueue_manual_refresh_once(
            task_name="today_entry",
            label="当日建仓",
            payload={
                "end_date": _resolve_realtime_end_date(end_dt, now_for_realtime),
                "risk_profile": risk_profile,
                "threshold": float(st.session_state.get("best_threshold", factor_threshold)),
                "enabled_factors": selected_factor_codes if selected_factor_codes else ["momentum_20"],
                "factor_weights": factor_weights,
                "lookback_days": int(smart_lookback_days),
                "top_n": 20,
                "total_position_pct": float(smart_total_position),
                "universe_size": int(smart_universe_size),
                "max_single_position_pct": float(smart_single_cap),
                "sector_flow_indicator": str(smart_sector_flow_indicator),
                "macro_enabled": bool(smart_macro_enabled),
            },
            queued_message="已提交当日建仓后台刷新任务，请稍后自动加载结果。",
        )

    if run_rebound_entry and enable_smart_picker:
        st.session_state["rebound_entry_msg"] = _enqueue_manual_refresh_once(
            task_name="rebound_entry",
            label="副策略建仓",
            payload={
                "end_date": _resolve_realtime_end_date(end_dt, now_for_realtime),
                "risk_profile": risk_profile,
                "threshold": float(st.session_state.get("best_threshold", factor_threshold)),
                "enabled_factors": selected_factor_codes if selected_factor_codes else ["momentum_20"],
                "factor_weights": factor_weights,
                "lookback_days": int(smart_lookback_days),
                "top_n": 20,
            },
            queued_message="已提交副策略建仓后台刷新任务，请稍后自动加载结果。",
        )

    @st.fragment(run_every="15s")
    def render_background_results_fragment() -> None:
        _sync_background_snapshot_state(
            enable_smart_picker=bool(enable_smart_picker),
            enable_dragon_radar=bool(enable_dragon_radar),
            enable_ma5_pullback=bool(enable_ma5_pullback),
        )

        if enable_smart_picker:
            if "sector_fund_flow_msg" in st.session_state and st.session_state["sector_fund_flow_msg"]:
                st.info(st.session_state["sector_fund_flow_msg"])
            if "smart_pick_msg" in st.session_state and st.session_state["smart_pick_msg"]:
                st.info(st.session_state["smart_pick_msg"])

            sector_flow_snapshot_meta = read_snapshot("sector_fund_flow") if enable_smart_picker else {}
            sector_flow_snapshot_time = str((sector_flow_snapshot_meta or {}).get("generated_at", "") or "")
            sector_flow_task_success = _task_latest_success_time("sector_fund_flow")
            smart_snapshot_meta = read_snapshot("smart_pick") if enable_smart_picker else {}
            smart_snapshot_time = str((smart_snapshot_meta or {}).get("generated_at", "") or "")
            smart_task_success = _task_latest_success_time("smart_pick")

            if "smart_macro_regime" in st.session_state and isinstance(st.session_state["smart_macro_regime"], dict):
                macro_summary = st.session_state["smart_macro_regime"]
                if macro_summary:
                    st.subheader("中国宏观康波摘要")
                    st.caption(f"后台刷新时间：{smart_snapshot_time or '-'} | 任务最近成功：{smart_task_success or '-'}")
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("宏观阶段", str(macro_summary.get("宏观康波阶段", "") or "-"))
                    m2.metric("景气方向", str(macro_summary.get("景气方向", "") or "-"))
                    m3.metric("流动性状态", str(macro_summary.get("流动性状态", "") or "-"))
                    m4.metric("宏观环境", str(macro_summary.get("宏观顺风/逆风", "") or "-"))
                    st.caption(str(macro_summary.get("宏观解读", "") or ""))

            if "smart_macro_detail_df" in st.session_state and isinstance(st.session_state["smart_macro_detail_df"], pd.DataFrame):
                macro_detail_show = st.session_state["smart_macro_detail_df"].copy()
                if not macro_detail_show.empty:
                    st.dataframe(
                        macro_detail_show.style.format(
                            {
                                "最新值": "{:.2f}",
                                "前值": "{:.2f}",
                                "得分": "{:.2f}",
                            }
                        ),
                        use_container_width=True,
                    )

            if "smart_sector_rotation_summary" in st.session_state and isinstance(st.session_state["smart_sector_rotation_summary"], dict):
                sector_summary = st.session_state["smart_sector_rotation_summary"]
                if sector_summary:
                    st.subheader("康波周期板块轮动与资金流")
                    st.caption(f"来自 smart_pick 快照：{smart_snapshot_time or '-'} | smart_pick 最近成功：{smart_task_success or '-'}")
                    c1, c2, c3, c4, c5 = st.columns(5)
                    c1.metric("最热板块", str(sector_summary.get("最热板块", "") or "-"))
                    c2.metric("走强板块数", int(sector_summary.get("走强板块数", 0)))
                    c3.metric("走弱板块数", int(sector_summary.get("走弱板块数", 0)))
                    c4.metric("复苏/繁荣", int(sector_summary.get("复苏板块数", 0)) + int(sector_summary.get("繁荣板块数", 0)))
                    c5.metric("繁荣候选数", int(sector_summary.get("繁荣候选数", 0)))
                    hot_names = "、".join(sector_summary.get("热门板块", []) or [])
                    up_names = "、".join(sector_summary.get("走强板块", []) or [])
                    down_names = "、".join(sector_summary.get("走弱板块", []) or [])
                    flow_names = "、".join(sector_summary.get("资金净流入板块", []) or [])
                    next_names = "、".join(sector_summary.get("下一周期繁荣候选", []) or [])
                    flow_meta = str(sector_summary.get("资金流周期", "") or "").strip()
                    flow_source = str(sector_summary.get("资金流来源", "") or "").strip()
                    macro_phase = str(sector_summary.get("宏观康波阶段", "") or "").strip()
                    liquidity_state = str(sector_summary.get("流动性状态", "") or "").strip()
                    if hot_names:
                        st.caption(f"当前热门板块：{hot_names}")
                    if up_names:
                        st.caption(f"正在走强：{up_names}")
                    if down_names:
                        st.caption(f"正在走弱：{down_names}")
                    if flow_names:
                        st.caption(f"{flow_meta or '资金流'}净流入靠前：{flow_names}")
                    if next_names:
                        st.caption(f"下一周期繁荣候选：{next_names}")
                    if flow_source:
                        st.caption(f"资金流来源：{flow_source}")
                    if macro_phase or liquidity_state:
                        st.caption(f"宏观阶段：{macro_phase or '-'}，流动性：{liquidity_state or '-'}")

            if "sector_fund_flow_df_map" in st.session_state and isinstance(st.session_state["sector_fund_flow_df_map"], dict):
                fund_flow_map = st.session_state["sector_fund_flow_df_map"]
                summary_map = st.session_state.get("sector_fund_flow_summary_map", {})
                flow_periods = [period for period in ["当日", "3日", "10日"] if isinstance(fund_flow_map.get(period), pd.DataFrame) and not fund_flow_map.get(period).empty]
                if flow_periods:
                    st.subheader("板块资金流明细")
                    st.caption(
                        f"后台刷新时间：{sector_flow_snapshot_time or '-'} | sector_fund_flow 最近成功：{sector_flow_task_success or '-'}"
                    )
                    tabs = st.tabs(flow_periods)
                    for period, tab in zip(flow_periods, tabs):
                        with tab:
                            selected_summary = summary_map.get(period, {}) if isinstance(summary_map, dict) else {}
                            flow_mode = str(selected_summary.get("资金流模式", "") or "")
                            flow_source = str(selected_summary.get("资金流来源", "") or "")
                            flow_updated_at = str(selected_summary.get("更新时间", "") or "")
                            intraday_mode = str(selected_summary.get("当日资金流模式", "") or "")
                            intraday_source = str(selected_summary.get("当日资金流来源", "") or "")
                            intraday_updated_at = str(selected_summary.get("当日更新时间", "") or "")
                            fund_flow_show = fund_flow_map.get(period, pd.DataFrame()).copy().rename(columns={"industry": "板块"})
                            st.caption(f"当前周期：{period} | 当前模式：{flow_mode or '-'} | 原始来源：{flow_source or '-'} | 数据更新时间：{flow_updated_at or '-'}")
                            if period != "当日":
                                st.caption(f"当日口径：{intraday_mode or '-'} | 原始来源：{intraday_source or '-'} | 数据更新时间：{intraday_updated_at or '-'}")
                            st.dataframe(
                                fund_flow_show.style.format(
                                    {
                                        "涨跌幅": "{:.2f}%",
                                        "主力净流入净额": "{:.0f}",
                                        "主力净流入净占比": "{:.2f}%",
                                        "当日主力净流入净额": "{:.0f}",
                                        "当日主力净流入净占比": "{:.2f}%",
                                        "超大单净流入净额": "{:.0f}",
                                        "超大单净流入净占比": "{:.2f}%",
                                        "大单净流入净额": "{:.0f}",
                                        "大单净流入净占比": "{:.2f}%",
                                        "中单净流入净额": "{:.0f}",
                                        "中单净流入净占比": "{:.2f}%",
                                        "小单净流入净额": "{:.0f}",
                                        "小单净流入净占比": "{:.2f}%",
                                    }
                                ),
                                use_container_width=True,
                            )

            if "smart_sector_rotation_df" in st.session_state and isinstance(st.session_state["smart_sector_rotation_df"], pd.DataFrame):
                sector_show = st.session_state["smart_sector_rotation_df"].copy()
                if not sector_show.empty:
                    sector_show = sector_show.rename(columns={"industry": "板块"})
                    st.subheader("板块轮动与下周期繁荣预测")
                    st.caption(f"后台刷新时间：{smart_snapshot_time or '-'} | smart_pick 最近成功：{smart_task_success or '-'}")
                    st.dataframe(
                        sector_show.style.format(
                            {
                                "5日涨跌": "{:.2%}",
                                "20日涨跌": "{:.2%}",
                                "60日涨跌": "{:.2%}",
                                "资金热度": "{:.2%}",
                                "强势占比": "{:.2%}",
                                "突破占比": "{:.2%}",
                                "趋势加速度": "{:.2%}",
                                "平均成交额": "{:.0f}",
                                "板块资金流涨跌幅": "{:.2f}%",
                                "主力净流入净额(亿)": "{:.2f}",
                                "超大单净流入净额(亿)": "{:.2f}",
                                "大单净流入净额(亿)": "{:.2f}",
                                "主力净流入净占比": "{:.2f}%",
                                "板块轮动评分": "{:.2f}",
                                "板块热度": "{:.2f}",
                                "板块资金流评分": "{:.2f}",
                                "下一周期繁荣评分": "{:.2f}",
                                "下一周期繁荣概率": "{:.1f}%",
                                "宏观景气评分": "{:.2f}",
                                "宏观流动性评分": "{:.2f}",
                                "宏观总评分": "{:.2f}",
                                "浪2回撤比例": "{:.3f}",
                                "浪3延展比例": "{:.3f}",
                                "浪4回撤比例": "{:.3f}",
                                "浪5延展比例": "{:.3f}",
                                "浪5等长比": "{:.3f}",
                                "浪5目标扩展比例": "{:.3f}",
                                "A浪回撤比例": "{:.3f}",
                                "B浪反弹比例": "{:.3f}",
                                "C浪延展比例": "{:.3f}",
                            }
                        ),
                        use_container_width=True,
                    )

            if "smart_recent_entry_signal_df" in st.session_state and isinstance(st.session_state["smart_recent_entry_signal_df"], pd.DataFrame):
                entry_signal_show = st.session_state["smart_recent_entry_signal_df"].copy()
                if not entry_signal_show.empty:
                    st.subheader("全市场刚出现建仓信号的股票")
                    st.caption(
                        f"后台刷新时间：{smart_snapshot_time or '-'} | smart_pick 最近成功：{smart_task_success or '-'} | "
                        "已排除 300/301/688/689/北交所/ST/退市；按信号日期从近到远排序。"
                    )
                    show_df = entry_signal_show.rename(
                        columns={
                            "ts_code": "股票代码",
                            "name": "股票名称",
                            "industry": "行业",
                            "market": "市场",
                        }
                    )
                    st.dataframe(
                        show_df.style.format(
                            {
                                "最新收盘": "{:.3f}",
                                "最新因子得分": "{:.3f}",
                                "近5日涨跌": "{:.2%}",
                                "近20日涨跌": "{:.2%}",
                            }
                        ),
                        use_container_width=True,
                    )
                    render_backtest_detail_links(
                        show_df,
                        code_col="股票代码",
                        name_col="股票名称",
                        threshold=float(st.session_state.get("best_threshold", factor_threshold)),
                        enabled_factors=selected_factor_codes if selected_factor_codes else ["momentum_20"],
                        factor_weights=factor_weights,
                        lookback_days=60,
                        title="建仓信号候选：点击股票名称（新标签页）查看60日回测",
                    )

            if "smart_pick_df" in st.session_state and isinstance(st.session_state["smart_pick_df"], pd.DataFrame):
                smart_df_show = st.session_state["smart_pick_df"].copy()
                if not smart_df_show.empty:
                    col_map = {
                        "ts_code": "股票代码",
                        "name": "股票名称",
                        "industry": "行业",
                        "market": "市场",
                        "最近交易日": "最近交易日",
                        "康波阶段": "康波阶段",
                        "轮动状态": "轮动状态",
                        "板块波浪阶段": "板块波浪阶段",
                        "波浪标签": "波浪标签",
                        "建仓位置评分": "建仓位置评分",
                        "建仓提示": "建仓提示",
                        "板块轮动评分": "板块轮动评分",
                        "板块热度": "板块热度",
                        "主力净流入净额(亿)": "所属板块主力净流入(亿)",
                        "主力净流入净占比": "所属板块主力净流入占比",
                        "板块资金流评分": "所属板块资金流评分",
                        "下一周期繁荣评分": "下一周期繁荣评分",
                        "下一周期繁荣概率": "下一周期繁荣概率",
                        "下一周期判断": "下一周期判断",
                        "宏观康波阶段": "宏观康波阶段",
                        "宏观顺风/逆风": "宏观顺风/逆风",
                        "趋势评分": "趋势评分",
                        "资金评分": "资金评分",
                        "事件评分": "事件评分",
                        "因子得分": "因子得分",
                        "推荐评分": "推荐评分",
                        "建议仓位%": "建议仓位%",
                        "推荐理由": "推荐理由",
                        "交易计划": "交易计划",
                    }
                    smart_df_show = smart_df_show.rename(columns=col_map)
                    st.subheader("科学选股推荐（趋势+资金+事件）")
                    st.dataframe(
                        smart_df_show.style.format(
                            {
                                "建仓位置评分": "{:.0f}",
                                "板块轮动评分": "{:.2f}",
                                "板块热度": "{:.2f}",
                                "所属板块主力净流入(亿)": "{:.2f}",
                                "所属板块主力净流入占比": "{:.2f}%",
                                "所属板块资金流评分": "{:.2f}",
                                "下一周期繁荣评分": "{:.2f}",
                                "下一周期繁荣概率": "{:.1f}%",
                                "趋势评分": "{:.2f}",
                                "资金评分": "{:.2f}",
                                "事件评分": "{:.2f}",
                                "因子得分": "{:.2f}",
                                "推荐评分": "{:.2f}",
                                "建议仓位%": "{:.1f}%",
                            }
                        ),
                        use_container_width=True,
                    )

                    render_intraday_links(
                        smart_df_show,
                        code_col="股票代码",
                        name_col="股票名称",
                        title="点击股票名称（新标签页）查看当日分钟走势图",
                    )
                    render_recent_backtest_links(
                        smart_df_show,
                        code_col="股票代码",
                        name_col="股票名称",
                        threshold=float(st.session_state.get("best_threshold", factor_threshold)),
                        enabled_factors=selected_factor_codes if selected_factor_codes else ["momentum_20"],
                        factor_weights=factor_weights,
                        title="点击后在新页面查看过去3个月回测明细",
                    )

            if bool(enable_ma5_pullback) and st.session_state.get("ma5_pullback_msg"):
                st.info(st.session_state.get("ma5_pullback_msg"))

            if bool(enable_ma5_pullback) and isinstance(st.session_state.get("ma5_pullback_df"), pd.DataFrame):
                pullback_show = st.session_state["ma5_pullback_df"].copy()
                if not pullback_show.empty:
                    show_df = pullback_show.rename(
                        columns={
                            "ts_code": "股票代码",
                            "name": "股票名称",
                            "industry": "行业",
                            "market": "市场",
                        }
                    )
                    ma5_snapshot_meta = read_snapshot("ma5_pullback") or {}
                    ma5_snapshot_time = str(ma5_snapshot_meta.get("generated_at", "") or "")
                    ma5_task_success = _task_latest_success_time("ma5_pullback")
                    st.subheader("5日线回踩+MACD多头推荐")
                    st.caption(f"后台刷新时间：{ma5_snapshot_time or '-'} | 任务最近成功：{ma5_task_success or '-'}")
                    st.dataframe(
                        show_df.style.format(
                            {
                                "5日线偏离%": "{:.2%}",
                                "20日趋势涨幅": "{:.2%}",
                                "量能趋势": "{:.2%}",
                                "因子表现": "{:.2f}",
                                "推荐评分": "{:.2f}",
                                "MACD多头": "{:.0f}",
                            }
                        ),
                        use_container_width=True,
                    )

                    render_recent_backtest_links(
                        show_df,
                        code_col="股票代码",
                        name_col="股票名称",
                        threshold=float(st.session_state.get("best_threshold", factor_threshold)),
                        enabled_factors=selected_factor_codes if selected_factor_codes else ["momentum_20"],
                        factor_weights=factor_weights,
                        title="5日线回踩推荐：点击后在新页面查看近3个月回测明细",
                    )

            render_three_bull_pullback_strategy_guide()

            if "today_entry_msg" in st.session_state and st.session_state["today_entry_msg"]:
                st.info(st.session_state["today_entry_msg"])

            if "today_entry_summary" in st.session_state and isinstance(st.session_state["today_entry_summary"], dict):
                s = st.session_state["today_entry_summary"]
                if s:
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("候选股票", int(s.get("股票数", 0)))
                    c2.metric("可建仓只数", int(s.get("可建仓只数", 0)))
                    c3.metric("建议建仓合计", f"{float(s.get('建议建仓合计%', 0.0)):.1f}%")
                    c4.metric("右侧确认只数", int(s.get("右侧确认只数", 0)))

            if "today_entry_df" in st.session_state and isinstance(st.session_state["today_entry_df"], pd.DataFrame):
                plan_show = st.session_state["today_entry_df"].copy()
                if not plan_show.empty:
                    today_snapshot_meta = read_snapshot("today_entry") or {}
                    today_snapshot_time = str(today_snapshot_meta.get("generated_at", "") or "")
                    today_task_success = _task_latest_success_time("today_entry")
                    st.subheader("当日建议建仓（主策略 + 右侧确认候选）")
                    st.caption(f"后台刷新时间：{today_snapshot_time or '-'} | 任务最近成功：{today_task_success or '-'}")
                    st.dataframe(
                        plan_show.style.format(
                            {
                                "建议建仓%": "{:.1f}%",
                                "首笔建议%": "{:.1f}%",
                                "因子得分": "{:.2f}",
                                "综合得分": "{:.2f}",
                                "近5日涨跌%": "{:.2f}%",
                                "近20日涨跌%": "{:.2f}%",
                                "换手率": "{:.2f}",
                                "PE": "{:.2f}",
                                "PB": "{:.2f}",
                            }
                        ),
                        use_container_width=True,
                    )

                    render_intraday_links(
                        plan_show,
                        code_col="ts_code",
                        name_col="name",
                        title="当日建仓候选：点击股票名称（新标签页）查看当日分钟走势图",
                    )

                    render_recent_backtest_links(
                        plan_show,
                        code_col="ts_code",
                        name_col="name",
                        threshold=float(st.session_state.get("best_threshold", factor_threshold)),
                        enabled_factors=selected_factor_codes if selected_factor_codes else ["momentum_20"],
                        factor_weights=factor_weights,
                        title="当日建议建仓：点击后在新页面查看近3个月回测明细",
                    )

            if "right_side_watch_df" in st.session_state and isinstance(st.session_state["right_side_watch_df"], pd.DataFrame):
                watch_show = st.session_state["right_side_watch_df"].copy()
                if not watch_show.empty:
                    st.subheader("右侧观察候选（全量）")
                    st.caption("三连阳后回踩未破坏，但尚未完成重新放量/突破确认，供逐只人工确认。")
                    st.dataframe(
                        watch_show.style.format(
                            {
                                "右侧确认分": "{:.0f}",
                                "回踩天数": "{:.0f}",
                                "回踩幅度%": "{:.2f}%",
                                "信号新鲜度": "{:.0f}",
                                "连续上涨天数": "{:.0f}",
                                "高位追涨标记": "{:.0f}",
                                "近5日涨跌%": "{:.2f}%",
                                "近20日涨跌%": "{:.2f}%",
                                "换手率": "{:.2f}",
                                "PE": "{:.2f}",
                                "PB": "{:.2f}",
                            }
                        ),
                        use_container_width=True,
                    )
                    render_recent_backtest_links(
                        watch_show,
                        code_col="ts_code",
                        name_col="name",
                        threshold=float(st.session_state.get("best_threshold", factor_threshold)),
                        enabled_factors=selected_factor_codes if selected_factor_codes else ["momentum_20"],
                        factor_weights=factor_weights,
                        title="右侧观察候选：点击后在新页面查看近3个月回测明细",
                    )

            render_bottom_rebound_strategy_guide()

            if "rebound_entry_msg" in st.session_state and st.session_state["rebound_entry_msg"]:
                st.info(st.session_state["rebound_entry_msg"])

            if "rebound_entry_summary" in st.session_state and isinstance(st.session_state["rebound_entry_summary"], dict):
                s = st.session_state["rebound_entry_summary"]
                if s:
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("候选股票", int(s.get("股票数", 0)))
                    c2.metric("可建仓只数", int(s.get("可建仓只数", 0)))
                    c3.metric("建议建仓合计", f"{float(s.get('建议建仓合计%', 0.0)):.1f}%")
                    c4.metric("反弹确认只数", int(s.get("反弹确认只数", 0)))

            if "rebound_entry_df" in st.session_state and isinstance(st.session_state["rebound_entry_df"], pd.DataFrame):
                rebound_show = st.session_state["rebound_entry_df"].copy()
                if not rebound_show.empty:
                    rebound_snapshot_meta = read_snapshot("rebound_entry") or {}
                    rebound_snapshot_time = str(rebound_snapshot_meta.get("generated_at", "") or "")
                    rebound_task_success = _task_latest_success_time("rebound_entry")
                    st.subheader("当日建议建仓（副策略 + 抄底反弹策略）")
                    st.caption(f"后台刷新时间：{rebound_snapshot_time or '-'} | 任务最近成功：{rebound_task_success or '-'}")
                    st.dataframe(
                        rebound_show.style.format(
                            {
                                "建议建仓%": "{:.1f}%",
                                "首笔建议%": "{:.1f}%",
                                "反弹确认分": "{:.0f}",
                                "超跌幅度%": "{:.2f}%",
                                "低位回升幅度%": "{:.2f}%",
                                "因子得分": "{:.2f}",
                                "综合得分": "{:.2f}",
                                "近5日涨跌%": "{:.2f}%",
                                "近20日涨跌%": "{:.2f}%",
                                "换手率": "{:.2f}",
                                "PE": "{:.2f}",
                                "PB": "{:.2f}",
                            }
                        ),
                        use_container_width=True,
                    )

                    render_intraday_links(
                        rebound_show,
                        code_col="ts_code",
                        name_col="name",
                        title="副策略候选：点击股票名称（新标签页）查看当日分钟走势图",
                    )

                    render_recent_backtest_links(
                        rebound_show,
                        code_col="ts_code",
                        name_col="name",
                        threshold=float(st.session_state.get("best_threshold", factor_threshold)),
                        enabled_factors=selected_factor_codes if selected_factor_codes else ["momentum_20"],
                        factor_weights=factor_weights,
                        title="副策略建仓：点击后在新页面查看近3个月回测明细",
                    )

            if "rebound_watch_df" in st.session_state and isinstance(st.session_state["rebound_watch_df"], pd.DataFrame):
                rebound_watch_show = st.session_state["rebound_watch_df"].copy()
                if not rebound_watch_show.empty:
                    st.subheader("抄底观察候选（全量）")
                    st.caption("已进入超跌观察区，但尚未完成量能与结构确认，适合做次日承接跟踪。")
                    st.dataframe(
                        rebound_watch_show.style.format(
                            {
                                "反弹确认分": "{:.0f}",
                                "超跌幅度%": "{:.2f}%",
                                "低位回升幅度%": "{:.2f}%",
                                "信号新鲜度": "{:.0f}",
                                "连续下跌天数": "{:.0f}",
                                "近1日涨跌%": "{:.2f}%",
                                "近5日涨跌%": "{:.2f}%",
                                "近10日涨跌%": "{:.2f}%",
                                "近20日涨跌%": "{:.2f}%",
                                "换手率": "{:.2f}",
                                "PE": "{:.2f}",
                                "PB": "{:.2f}",
                            }
                        ),
                        use_container_width=True,
                    )
                    render_recent_backtest_links(
                        rebound_watch_show,
                        code_col="ts_code",
                        name_col="name",
                        threshold=float(st.session_state.get("best_threshold", factor_threshold)),
                        enabled_factors=selected_factor_codes if selected_factor_codes else ["momentum_20"],
                        factor_weights=factor_weights,
                        title="抄底观察候选：点击后在新页面查看近3个月回测明细",
                    )

        if enable_dragon_radar:
            if "dragon_radar_msg" in st.session_state and st.session_state["dragon_radar_msg"]:
                st.info(st.session_state["dragon_radar_msg"])

            if "dragon_radar_df" in st.session_state and isinstance(st.session_state["dragon_radar_df"], pd.DataFrame):
                dragon_show = st.session_state["dragon_radar_df"].copy()
                if not dragon_show.empty:
                    dragon_snapshot_meta = read_snapshot("dragon_radar") or {}
                    dragon_snapshot_time = str(dragon_snapshot_meta.get("generated_at", "") or "")
                    dragon_task_success = _task_latest_success_time("dragon_radar")
                    col_map = {
                        "ts_code": "股票代码",
                        "name": "股票名称",
                        "industry": "行业",
                        "market": "市场",
                        "最近交易日": "最近交易日",
                        "龙头标签": "龙头标签",
                        "参与类型": "参与类型",
                        "传播启动分": "传播启动分",
                        "涨价映射分": "涨价映射分",
                        "海外映射分": "海外映射分",
                        "题材催化分": "题材催化分",
                        "龙头地位分": "龙头地位分",
                        "板块内涨幅排名": "板块内涨幅排名",
                        "板块内成交额排名": "板块内成交额排名",
                        "板块内放量排名": "板块内放量排名",
                        "封板确认分": "封板确认分",
                        "拥挤惩罚分": "拥挤惩罚分",
                        "高位追涨标记": "高位追涨标记",
                        "涨价品种": "涨价品种",
                        "海外对标": "海外对标",
                        "近1日涨价幅度": "近1日涨价幅度",
                        "近5日涨价幅度": "近5日涨价幅度",
                        "海外隔夜涨幅": "海外隔夜涨幅",
                        "海外3日涨幅": "海外3日涨幅",
                        "映射说明": "映射说明",
                        "趋势评分": "趋势评分",
                        "消息面评分": "消息面评分",
                        "财报评分": "财报评分",
                        "MACD金叉": "MACD金叉",
                        "波段位置": "波段位置",
                        "缺口信号": "缺口信号",
                        "放量评分": "放量评分",
                        "因子得分": "因子得分",
                        "擒龙评分": "擒龙评分",
                        "盘中涨跌%": "盘中涨跌%",
                        "盘中振幅%": "盘中振幅%",
                        "盘中量比": "盘中量比",
                        "盘中确认分": "盘中确认分",
                        "综合得分": "综合得分",
                        "建议仓位%": "建议仓位%",
                        "黄金买点": "黄金买点",
                        "黄金卖点": "黄金卖点",
                        "推荐理由": "推荐理由",
                    }
                    dragon_show = dragon_show.rename(columns=col_map)
                    st.subheader("龙头雷达推荐（题材催化+龙头地位+封板确认）")
                    st.caption(f"后台刷新时间：{dragon_snapshot_time or '-'} | 任务最近成功：{dragon_task_success or '-'}")
                    st.dataframe(
                        dragon_show.style.format(
                            {
                                "传播启动分": "{:.1f}",
                                "涨价映射分": "{:.1f}",
                                "海外映射分": "{:.1f}",
                                "题材催化分": "{:.1f}",
                                "龙头地位分": "{:.1f}",
                                "板块内涨幅排名": "{:.0f}",
                                "板块内成交额排名": "{:.0f}",
                                "板块内放量排名": "{:.0f}",
                                "封板确认分": "{:.1f}",
                                "拥挤惩罚分": "{:.1f}",
                                "高位追涨标记": "{:.0f}",
                                "近1日涨价幅度": "{:.2f}%",
                                "近5日涨价幅度": "{:.2f}%",
                                "海外隔夜涨幅": "{:.2f}%",
                                "海外3日涨幅": "{:.2f}%",
                                "趋势评分": "{:.2f}",
                                "消息面评分": "{:.2f}",
                                "财报评分": "{:.2f}",
                                "MACD金叉": "{:.0f}",
                                "波段位置": "{:.2f}",
                                "缺口信号": "{:.0f}",
                                "放量评分": "{:.2f}",
                                "因子得分": "{:.2f}",
                                "擒龙评分": "{:.2f}",
                                "盘中涨跌%": "{:.2f}%",
                                "盘中振幅%": "{:.2f}%",
                                "盘中量比": "{:.2f}",
                                "盘中确认分": "{:.2f}",
                                "综合得分": "{:.2f}",
                                "建议仓位%": "{:.1f}%",
                            }
                        ),
                        use_container_width=True,
                    )

                    render_recent_backtest_links(
                        dragon_show,
                        code_col="股票代码",
                        name_col="股票名称",
                        threshold=float(st.session_state.get("best_threshold", factor_threshold)),
                        enabled_factors=selected_factor_codes if selected_factor_codes else ["momentum_20"],
                        factor_weights=factor_weights,
                        title="龙头雷达：点击后在新页面查看近3个月回测明细",
                    )

    render_background_results_fragment()

    if run_intraday_incremental and enable_intraday_incremental:
        try:
            if intraday_source in {"自动拉取(Tushare)", "自动拉取(AkShare免费)"}:
                source_key = "tushare" if intraday_source == "自动拉取(Tushare)" else "akshare"
                minute_df = data_manager.get_minute_data(
                    ts_code=ts_code,
                    trade_date=pd.Timestamp(intraday_trade_date).strftime("%Y%m%d"),
                    freq=intraday_freq,
                    source=source_key,
                    use_cache=True,
                    force_refresh=bool(intraday_force_refresh),
                )
            else:
                minute_df = parse_minute_csv_input(
                    minute_csv_text=minute_csv_text,
                    minute_csv_file=minute_csv_file,
                    default_trade_date=intraday_trade_date,
                    default_ts_code=ts_code,
                )
            if minute_df.empty:
                if intraday_source in {"自动拉取(Tushare)", "自动拉取(AkShare免费)"}:
                    st.session_state["intraday_msg"] = "自动拉取未返回分钟数据（可能为非交易时段、频控或当日无数据）。"
                else:
                    st.session_state["intraday_msg"] = "请先上传或粘贴分钟CSV数据。"
                st.session_state["intraday_signal_df"] = pd.DataFrame()
            else:
                hist_start = (pd.Timestamp(intraday_trade_date) - pd.Timedelta(days=int(intraday_lookback_days))).strftime("%Y%m%d")
                hist_end = pd.Timestamp(intraday_trade_date).strftime("%Y%m%d")
                daily_hist = data_manager.get_daily_data(ts_code, hist_start, hist_end, use_cache=True)
                if daily_hist is None or daily_hist.empty:
                    st.session_state["intraday_msg"] = "日线历史不足，无法执行盘中增量计算。"
                    st.session_state["intraday_signal_df"] = pd.DataFrame()
                else:
                    strategy = FactorSelectionStrategy(
                        threshold=float(st.session_state.get("best_threshold", factor_threshold)),
                        enabled_factors=selected_factor_codes if selected_factor_codes else ["momentum_20"],
                        factor_weights=factor_weights,
                    )
                    intraday_df = strategy.generate_intraday_signals(daily_df=daily_hist.copy(), minute_df=minute_df)
                    if intraday_df is None or intraday_df.empty:
                        st.session_state["intraday_msg"] = "分钟数据已读取，但未生成有效盘中信号。"
                        st.session_state["intraday_signal_df"] = pd.DataFrame()
                    else:
                        intraday_df = intraday_df.copy()
                        intraday_df["signal"] = pd.to_numeric(intraday_df["signal"], errors="coerce").fillna(0).astype(int)
                        prev_sig = intraday_df["signal"].shift(1).fillna(intraday_df["signal"].iloc[0]).astype(int)
                        intraday_df["盘中动作"] = np.select(
                            [
                                (prev_sig == 0) & (intraday_df["signal"] == 1),
                                (prev_sig == 1) & (intraday_df["signal"] == 0),
                                intraday_df["signal"] == 1,
                            ],
                            ["买入触发", "卖出触发", "持有"],
                            default="观望",
                        )
                        keep_cols = [c for c in ["minute_time", "trade_date", "close", "factor_score", "signal", "盘中动作"] if c in intraday_df.columns]
                        intraday_df = intraday_df[keep_cols].copy()
                        st.session_state["intraday_signal_df"] = intraday_df
                        st.session_state["intraday_msg"] = (
                            f"盘中增量计算完成：{ts_code}，共 {len(intraday_df)} 条分钟信号（来源：{intraday_source}）。"
                        )
        except Exception as exc:
            st.session_state["intraday_signal_df"] = pd.DataFrame()
            st.session_state["intraday_msg"] = (
                f"盘中增量计算失败：{exc}。"
                "若刚触发分钟接口配额，请取消“强制刷新”并重试（优先走缓存），"
                "或改用手动上传CSV。"
            )

    if enable_dragon_radar:
        if "dragon_radar_msg" in st.session_state and st.session_state["dragon_radar_msg"]:
            st.info(st.session_state["dragon_radar_msg"])

        if "dragon_radar_df" in st.session_state and isinstance(st.session_state["dragon_radar_df"], pd.DataFrame):
            dragon_show = st.session_state["dragon_radar_df"].copy()
            if not dragon_show.empty:
                col_map = {
                    "ts_code": "股票代码",
                    "name": "股票名称",
                    "industry": "行业",
                    "market": "市场",
                    "最近交易日": "最近交易日",
                    "龙头标签": "龙头标签",
                    "参与类型": "参与类型",
                    "传播启动分": "传播启动分",
                    "涨价映射分": "涨价映射分",
                    "海外映射分": "海外映射分",
                    "题材催化分": "题材催化分",
                    "龙头地位分": "龙头地位分",
                    "板块内涨幅排名": "板块内涨幅排名",
                    "板块内成交额排名": "板块内成交额排名",
                    "板块内放量排名": "板块内放量排名",
                    "封板确认分": "封板确认分",
                    "拥挤惩罚分": "拥挤惩罚分",
                    "高位追涨标记": "高位追涨标记",
                    "涨价品种": "涨价品种",
                    "海外对标": "海外对标",
                    "近1日涨价幅度": "近1日涨价幅度",
                    "近5日涨价幅度": "近5日涨价幅度",
                    "海外隔夜涨幅": "海外隔夜涨幅",
                    "海外3日涨幅": "海外3日涨幅",
                    "映射说明": "映射说明",
                    "趋势评分": "趋势评分",
                    "消息面评分": "消息面评分",
                    "财报评分": "财报评分",
                    "MACD金叉": "MACD金叉",
                    "波段位置": "波段位置",
                    "缺口信号": "缺口信号",
                    "放量评分": "放量评分",
                    "因子得分": "因子得分",
                    "擒龙评分": "擒龙评分",
                    "盘中涨跌%": "盘中涨跌%",
                    "盘中振幅%": "盘中振幅%",
                    "盘中量比": "盘中量比",
                    "盘中确认分": "盘中确认分",
                    "综合得分": "综合得分",
                    "建议仓位%": "建议仓位%",
                    "黄金买点": "黄金买点",
                    "黄金卖点": "黄金卖点",
                    "推荐理由": "推荐理由",
                }
                dragon_show = dragon_show.rename(columns=col_map)
                st.subheader("龙头雷达推荐（题材催化+龙头地位+封板确认）")
                st.caption(_market_db_caption(display_count=len(dragon_show)))
                st.dataframe(
                    dragon_show.style.format(
                        {
                            "传播启动分": "{:.1f}",
                            "涨价映射分": "{:.1f}",
                            "海外映射分": "{:.1f}",
                            "题材催化分": "{:.1f}",
                            "龙头地位分": "{:.1f}",
                            "板块内涨幅排名": "{:.0f}",
                            "板块内成交额排名": "{:.0f}",
                            "板块内放量排名": "{:.0f}",
                            "封板确认分": "{:.1f}",
                            "拥挤惩罚分": "{:.1f}",
                            "高位追涨标记": "{:.0f}",
                            "近1日涨价幅度": "{:.2f}%",
                            "近5日涨价幅度": "{:.2f}%",
                            "海外隔夜涨幅": "{:.2f}%",
                            "海外3日涨幅": "{:.2f}%",
                            "趋势评分": "{:.2f}",
                            "消息面评分": "{:.2f}",
                            "财报评分": "{:.2f}",
                            "MACD金叉": "{:.0f}",
                            "波段位置": "{:.2f}",
                            "缺口信号": "{:.0f}",
                            "放量评分": "{:.2f}",
                            "因子得分": "{:.2f}",
                            "擒龙评分": "{:.2f}",
                            "盘中涨跌%": "{:.2f}%",
                            "盘中振幅%": "{:.2f}%",
                            "盘中量比": "{:.2f}",
                            "盘中确认分": "{:.2f}",
                            "综合得分": "{:.2f}",
                            "建议仓位%": "{:.1f}%",
                        }
                    ),
                    use_container_width=True,
                )

                render_recent_backtest_links(
                    dragon_show,
                    code_col="股票代码",
                    name_col="股票名称",
                    threshold=float(st.session_state.get("best_threshold", factor_threshold)),
                    enabled_factors=selected_factor_codes if selected_factor_codes else ["momentum_20"],
                    factor_weights=factor_weights,
                    title="龙头雷达：点击后在新页面查看近3个月回测明细",
                )

    if "intraday_msg" in st.session_state and st.session_state["intraday_msg"]:
        st.info(st.session_state["intraday_msg"])

    if "intraday_signal_df" in st.session_state and isinstance(st.session_state["intraday_signal_df"], pd.DataFrame):
        intraday_show = st.session_state["intraday_signal_df"].copy()
        if not intraday_show.empty:
            st.subheader("盘中增量因子信号（分钟级）")
            latest_row = intraday_show.iloc[-1]
            c1, c2, c3 = st.columns(3)
            c1.metric("最新动作", str(latest_row.get("盘中动作", "-")))
            c2.metric("最新信号", int(latest_row.get("signal", 0)))
            c3.metric("最新因子分", f"{float(latest_row.get('factor_score', 0.0)):.2f}")

            st.dataframe(
                intraday_show.style.format(
                    {
                        "close": "{:.3f}",
                        "factor_score": "{:.3f}",
                    }
                ),
                use_container_width=True,
            )

            chart_df = intraday_show.copy()
            if "minute_time" in chart_df.columns:
                chart_df = chart_df.set_index("minute_time")
                plot_cols = [c for c in ["close", "factor_score", "signal"] if c in chart_df.columns]
                if plot_cols:
                    st.line_chart(chart_df[plot_cols], use_container_width=True)

    if st.sidebar.button("运行回测", type="primary"):
        if not selected_strategies:
            st.error("请至少选择一个战法")
            st.stop()

        normalized_code = ts_code
        threshold_run = float(st.session_state.get("best_threshold", factor_threshold))

        try:
            with st.spinner("运行中..."):
                raw_df = data_manager.get_daily_data(
                    ts_code=normalized_code,
                    start_date=start_date,
                    end_date=end_date,
                    use_cache=True,
                )

                strategy_pool = build_strategies()
                result_map = {}
                for strategy_name in selected_strategies:
                    if strategy_name == "因子选股":
                        signal_df = FactorSelectionStrategy(
                            threshold=threshold_run,
                            enabled_factors=selected_factor_codes if selected_factor_codes else ["momentum_20"],
                            factor_weights=factor_weights,
                        ).generate_signals(raw_df.copy())
                        result_map[strategy_name] = BacktestEngine().run(signal_df)
                    elif strategy_name in PROFESSIONAL_PLAYBOOKS:
                        profile = PROFESSIONAL_PLAYBOOKS[strategy_name]
                        signal_df = FactorSelectionStrategy(
                            threshold=float(profile["threshold"]),
                            enabled_factors=list(profile["factors"]),
                            factor_weights=dict(profile["weights"]),
                        ).generate_signals(raw_df.copy())
                        result_map[strategy_name] = BacktestEngine().run(signal_df)
                    else:
                        strategy = strategy_pool[strategy_name]
                        signal_df = strategy.generate_signals(raw_df.copy())
                        result_map[strategy_name] = BacktestEngine().run(signal_df)
        except Exception as exc:
            st.error(str(exc))
            st.info(
                "可检查：1) token 是否正确；2) 账号积分/权限是否覆盖 daily 接口；"
                "3) 若仅演示流程，可先清空 TUSHARE_TOKEN 使用模拟数据。"
            )
            st.stop()

        compare_rows = []
        nav_df = pd.DataFrame()
        benchmark_set = False

        for strategy_name, result in result_map.items():
            stats = result.stats
            curve = result.equity_curve.copy()
            compare_rows.append(
                {
                    "战法": strategy_name,
                    "年化收益": stats["ann_return"],
                    "夏普": stats["sharpe"],
                    "最大回撤": stats["max_drawdown"],
                    "胜率": stats["win_rate"],
                }
            )

            series = curve.set_index("trade_date")["strategy_nav"].rename(strategy_name)
            nav_df = pd.concat([nav_df, series], axis=1) if not nav_df.empty else series.to_frame()

            if not benchmark_set:
                nav_df = nav_df.join(
                    curve.set_index("trade_date")["benchmark_nav"].rename("benchmark_nav（基准净值）"),
                    how="left",
                )
                benchmark_set = True

        st.subheader("战法对比指标")
        compare_df = pd.DataFrame(compare_rows).sort_values("年化收益", ascending=False).reset_index(drop=True)
        st.dataframe(compare_df.style.format({"年化收益": "{:.2%}", "最大回撤": "{:.2%}", "胜率": "{:.2%}", "夏普": "{:.2f}"}), use_container_width=True)

        st.subheader("净值曲线对比")
        st.line_chart(nav_df, use_container_width=True)

        st.subheader("最新回测明细（按首个战法展示）")
        first_curve = result_map[selected_strategies[0]].equity_curve
        first_curve_display = add_action_hints(
            first_curve,
            stop_profit_pct=float(stop_profit_pct),
            stop_loss_pct=float(stop_loss_pct),
            enable_risk_hints=bool(enable_risk_hints),
        )
        first_curve_display = format_detail_table(first_curve_display)

        st.caption(f"明细行数：{len(first_curve_display)}（从起始日期开始）")
        st.dataframe(first_curve_display, use_container_width=True)

        if enable_walk_forward:
            st.subheader("Walk-Forward（滚动前瞻） OOS（样本外）评估")
            if "因子选股" not in selected_strategies:
                st.info("Walk-Forward 当前仅对【因子选股】执行，请在战法中勾选因子选股。")
            else:
                wf_factors = selected_factor_codes if selected_factor_codes else ["momentum_20"]
                wf_fold_df, wf_oos_df = run_walk_forward(
                    raw_df=raw_df,
                    selected_factors=wf_factors,
                    train_window=wf_train_window,
                    test_window=wf_test_window,
                    step=wf_step,
                )
                if wf_fold_df.empty or wf_oos_df.empty:
                    st.info("样本长度不足或无有效窗口，请扩大日期区间或缩小窗口参数。")
                else:
                    st.dataframe(
                        wf_fold_df.style.format(
                            {
                                "最优阈值": "{:.2f}",
                                "训练夏普": "{:.2f}",
                                "测试年化": "{:.2%}",
                                "测试夏普": "{:.2f}",
                                "测试最大回撤": "{:.2%}",
                                "测试胜率": "{:.2%}",
                            }
                        ),
                        use_container_width=True,
                    )

                    wf_summary = {
                        "折数": int(len(wf_fold_df)),
                        "OOS 年化": float((1.0 + wf_oos_df["strategy_ret"].mean()) ** 242 - 1.0),
                        "OOS 夏普": float((wf_oos_df["strategy_ret"].mean() / (wf_oos_df["strategy_ret"].std() + 1e-12)) * np.sqrt(242.0)),
                        "OOS 最大回撤": float((wf_oos_df["strategy_nav_oos（策略净值-样本外）"] / wf_oos_df["strategy_nav_oos（策略净值-样本外）"].cummax() - 1.0).min()),
                    }
                    st.write(
                        f"折数: {wf_summary['折数']} | OOS年化: {wf_summary['OOS 年化']:.2%} | "
                        f"OOS夏普: {wf_summary['OOS 夏普']:.2f} | OOS最大回撤: {wf_summary['OOS 最大回撤']:.2%}"
                    )

                    st.line_chart(
                        wf_oos_df.set_index("trade_date")[["strategy_nav_oos（策略净值-样本外）", "benchmark_nav_oos（基准净值-样本外）"]],
                        use_container_width=True,
                    )

        if enable_rolling_compare:
            stage_df = rolling_stage_compare(raw_df, threshold=threshold_run, window_size=rolling_window_size, step=rolling_step)
            st.subheader("三阶段滚动窗口对比")
            if stage_df.empty:
                st.info("样本长度不足，无法进行滚动窗口对比，请扩大日期范围。")
            else:
                summary = (
                    stage_df.groupby("阶段")[["年化收益", "夏普", "最大回撤", "胜率"]]
                    .mean()
                    .reset_index()
                    .sort_values("夏普", ascending=False)
                )
                st.dataframe(
                    summary.style.format({"年化收益": "{:.2%}", "最大回撤": "{:.2%}", "胜率": "{:.2%}", "夏普": "{:.2f}"}),
                    use_container_width=True,
                )
                st.dataframe(
                    stage_df.style.format({"年化收益": "{:.2%}", "最大回撤": "{:.2%}", "胜率": "{:.2%}", "夏普": "{:.2f}"}),
                    use_container_width=True,
                )


if __name__ == "__main__":
    main()
