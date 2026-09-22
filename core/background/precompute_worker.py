from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from core.background.cache_maintenance import cleanup_cache
from core.background.snapshot_store import (
    append_task_progress_log,
    has_pending_request,
    read_request,
    read_snapshot,
    read_task_status,
    request_refresh,
    take_refresh_request,
    write_snapshot,
    write_task_status,
    write_worker_status,
)
from core.background.task_rules import should_write_error_snapshot
from core.data.data_manager import DataManager


@dataclass
class WorkerIntervals:
    market_db_sync_seconds: int = 120
    sector_fund_flow_seconds: int = 180
    smart_pick_seconds: int = 180
    dragon_radar_seconds: int = 180
    ma5_pullback_seconds: int = 180
    three_bull_pullback_seconds: int = 900
    today_entry_seconds: int = 900
    rebound_entry_seconds: int = 900
    holding_advice_seconds: int = 120
    cache_cleanup_seconds: int = 6 * 3600


SCHEDULED_REFRESH_TASKS: tuple[str, ...] = (
    "market_db_sync",
    "sector_fund_flow",
    "smart_pick",
    "dragon_radar",
    "ma5_pullback",
    "three_bull_pullback",
    "today_entry",
    "rebound_entry",
    "holding_advice",
)
SCHEDULED_REFRESH_HOURS: tuple[int, ...] = (9, 16, 20)


@dataclass
class WorkerTimeouts:
    market_db_sync_seconds: int = 0
    sector_fund_flow_seconds: int = 0
    smart_pick_seconds: int = 0
    dragon_radar_seconds: int = 0
    ma5_pullback_seconds: int = 0
    three_bull_pullback_seconds: int = 0
    today_entry_seconds: int = 0
    rebound_entry_seconds: int = 0
    holding_advice_seconds: int = 0
    cache_cleanup_seconds: int = 0


@dataclass(frozen=True)
class TaskSpec:
    name: str
    interval_seconds: int
    timeout_seconds: int
    priority: int
    dependencies: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class ActiveTask:
    spec: TaskSpec
    process: mp.Process
    out_queue: mp.Queue
    started_at: pd.Timestamp
    params: dict[str, Any]
    force_refresh: bool


def _task_process_main(task_name: str, params: dict[str, Any], force_refresh: bool, out_queue: mp.Queue) -> None:
    worker = PrecomputeWorker()
    try:
        payload = worker.dispatch_task(task_name=task_name, params=params, force_refresh=force_refresh)
        write_snapshot(task_name, payload=payload, status="ok")
        out_queue.put(
            {
                "ok": True,
                "generated_at": pd.Timestamp.now().isoformat(),
            }
        )
    except Exception as exc:
        out_queue.put(
            {
                "ok": False,
                "error": str(exc),
                "traceback": traceback.format_exc()[-8000:],
            }
        )


class PrecomputeWorker:
    def __init__(
        self,
        intervals: WorkerIntervals | None = None,
        timeouts: WorkerTimeouts | None = None,
        max_concurrency: int | None = None,
    ) -> None:
        self.data_manager = DataManager()
        self.intervals = intervals or WorkerIntervals()
        self.timeouts = timeouts or WorkerTimeouts()
        cpu_count = os.cpu_count() or 4
        self.max_concurrency = int(max_concurrency or max(2, min(4, cpu_count)))
        self.last_run: dict[str, pd.Timestamp] = {}
        self.active_tasks: dict[str, ActiveTask] = {}
        self._task_progress_state: dict[str, dict[str, Any]] = {}
        self.task_specs = self._build_task_specs()

    def _build_task_specs(self) -> dict[str, TaskSpec]:
        return {
            "market_db_sync": TaskSpec(
                name="market_db_sync",
                interval_seconds=self.intervals.market_db_sync_seconds,
                timeout_seconds=self.timeouts.market_db_sync_seconds,
                priority=5,
            ),
            "sector_fund_flow": TaskSpec(
                name="sector_fund_flow",
                interval_seconds=self.intervals.sector_fund_flow_seconds,
                timeout_seconds=self.timeouts.sector_fund_flow_seconds,
                priority=8,
                dependencies=("market_db_sync",),
            ),
            "smart_pick": TaskSpec(
                name="smart_pick",
                interval_seconds=self.intervals.smart_pick_seconds,
                timeout_seconds=self.timeouts.smart_pick_seconds,
                priority=10,
            ),
            "dragon_radar": TaskSpec(
                name="dragon_radar",
                interval_seconds=self.intervals.dragon_radar_seconds,
                timeout_seconds=self.timeouts.dragon_radar_seconds,
                priority=20,
            ),
            "ma5_pullback": TaskSpec(
                name="ma5_pullback",
                interval_seconds=self.intervals.ma5_pullback_seconds,
                timeout_seconds=self.timeouts.ma5_pullback_seconds,
                priority=30,
            ),
            "three_bull_pullback": TaskSpec(
                name="three_bull_pullback",
                interval_seconds=self.intervals.three_bull_pullback_seconds,
                timeout_seconds=self.timeouts.three_bull_pullback_seconds,
                priority=35,
                dependencies=("market_db_sync",),
            ),
            "today_entry": TaskSpec(
                name="today_entry",
                interval_seconds=self.intervals.today_entry_seconds,
                timeout_seconds=self.timeouts.today_entry_seconds,
                priority=40,
                dependencies=("smart_pick", "market_db_sync"),
            ),
            "rebound_entry": TaskSpec(
                name="rebound_entry",
                interval_seconds=self.intervals.rebound_entry_seconds,
                timeout_seconds=self.timeouts.rebound_entry_seconds,
                priority=45,
                dependencies=("market_db_sync",),
            ),
            "holding_advice": TaskSpec(
                name="holding_advice",
                interval_seconds=self.intervals.holding_advice_seconds,
                timeout_seconds=self.timeouts.holding_advice_seconds,
                priority=15,
            ),
            "cache_cleanup": TaskSpec(
                name="cache_cleanup",
                interval_seconds=self.intervals.cache_cleanup_seconds,
                timeout_seconds=self.timeouts.cache_cleanup_seconds,
                priority=100,
            ),
        }

    @staticmethod
    def _clamp_progress(progress_pct: int | float, upper: int = 100) -> int:
        try:
            value = int(float(progress_pct))
        except Exception:
            value = 0
        return max(0, min(int(upper), value))

    @staticmethod
    def _has_timeout(timeout_seconds: int | float | None) -> bool:
        try:
            return int(timeout_seconds or 0) > 0
        except Exception:
            return False

    @staticmethod
    def _int_list_param(value: Any, default: list[int]) -> list[int]:
        if value is None:
            return list(default)
        if isinstance(value, str):
            parts = [item.strip() for item in value.split(",")]
        elif isinstance(value, (list, tuple, set)):
            parts = list(value)
        else:
            parts = [value]
        out: list[int] = []
        for item in parts:
            if str(item).strip() == "":
                continue
            out.append(int(item))
        return out or list(default)

    @staticmethod
    def _fallback_three_bull_candidates(panel: pd.DataFrame, top_n: int = 20) -> pd.DataFrame:
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
        if panel is None or panel.empty:
            return pd.DataFrame(columns=show_cols)

        from core.strategies.three_bull_pullback import prepare_three_bull_feature_panel

        features = prepare_three_bull_feature_panel(panel)
        if features.empty:
            return pd.DataFrame(columns=show_cols)

        features["trade_date"] = pd.to_datetime(features["trade_date"], errors="coerce")
        latest_date = features["trade_date"].max()
        if pd.isna(latest_date):
            return pd.DataFrame(columns=show_cols)

        latest = features[features["trade_date"].eq(latest_date)].copy()
        if latest.empty:
            return pd.DataFrame(columns=show_cols)

        for col in ["pct_chg", "ret_5", "ret_20", "amount", "amount_ma_20", "市场环境分", "板块强度分", "题材催化分", "距20日高点"]:
            if col not in latest.columns:
                latest[col] = 0.0
            latest[col] = pd.to_numeric(latest[col], errors="coerce").fillna(0.0)

        amount_ma = latest["amount_ma_20"].where(latest["amount_ma_20"] > 0.0, pd.NA)
        latest["量能比"] = (latest["amount"] / amount_ma).replace([pd.NA, pd.NaT], 1.0).fillna(1.0).clip(lower=0.0, upper=5.0)
        trend_score = ((latest["ret_20"].clip(lower=-0.08, upper=0.12) + 0.08) / 0.20 * 100.0).clip(lower=0.0, upper=100.0)
        pullback_score = ((latest["距20日高点"].clip(lower=-0.12, upper=0.0) + 0.12) / 0.12 * 100.0).clip(lower=0.0, upper=100.0)
        volume_score = ((latest["量能比"].clip(lower=0.5, upper=1.8) - 0.5) / 1.3 * 100.0).clip(lower=0.0, upper=100.0)
        latest["执行确认分"] = (0.60 * volume_score + 0.40 * (latest["pct_chg"].clip(lower=-2.0, upper=5.0) + 2.0) / 7.0 * 100.0).clip(0.0, 100.0)
        latest["策略评分"] = (
            0.22 * latest["市场环境分"].clip(0.0, 100.0)
            + 0.22 * latest["板块强度分"].clip(0.0, 100.0)
            + 0.20 * latest["题材催化分"].clip(0.0, 100.0)
            + 0.18 * trend_score
            + 0.10 * pullback_score
            + 0.08 * latest["执行确认分"]
        ).clip(0.0, 100.0)
        latest["回踩幅度%"] = (latest["距20日高点"].clip(lower=-0.30, upper=0.0) * 100.0).round(2)
        latest["回踩天数"] = 0.0
        latest["交易动作"] = "观察等待"
        latest["候选来源"] = "形态观察兜底"
        latest["买点检查"] = "严格三阳回踩信号为空，按最新市场强弱排序观察"
        latest["入选说明"] = "未触发严格三阳回踩买点，仅用于保持最新观察榜单可见"

        out = latest.sort_values(["策略评分", "执行确认分"], ascending=[False, False]).head(int(top_n or 20)).copy()
        out["当日排名"] = range(1, len(out) + 1)
        out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        for col in show_cols:
            if col not in out.columns:
                out[col] = ""
        return out[show_cols].reset_index(drop=True)

    @classmethod
    def _running_progress(cls, elapsed_seconds: float, timeout_seconds: int | float | None) -> int:
        if cls._has_timeout(timeout_seconds):
            safe_timeout = max(1, int(timeout_seconds or 0))
            return cls._clamp_progress((elapsed_seconds / safe_timeout) * 100.0, upper=95)
        # Without a timeout threshold, keep a low, slow-moving progress bar and
        # let elapsed time carry the actual runtime signal.
        return cls._clamp_progress(max(1, int(elapsed_seconds // 60) + 1), upper=95)

    @classmethod
    def _progress_bar(cls, progress_pct: int | float, width: int = 20) -> str:
        pct = cls._clamp_progress(progress_pct)
        width = max(10, int(width or 0))
        filled = int(round((pct / 100.0) * width))
        filled = max(0, min(width, filled))
        return f"[{'#' * filled}{'-' * (width - filled)}] {pct}%"

    def _append_progress_event(
        self,
        task_name: str,
        status: str,
        progress_pct: int | float,
        message: str,
        extra: dict[str, Any] | None = None,
        min_interval_seconds: int = 8,
        force: bool = False,
    ) -> None:
        pct = self._clamp_progress(progress_pct)
        now_sec = int(pd.Timestamp.now().timestamp())
        prev = self._task_progress_state.get(task_name) or {}
        if (not force) and str(status or "") == "running":
            prev_sec = int(prev.get("ts", 0) or 0)
            prev_pct = int(prev.get("pct", -1) or -1)
            if (now_sec - prev_sec) < max(1, int(min_interval_seconds)) and prev_pct == pct:
                return

        try:
            append_task_progress_log(
                task_name=task_name,
                status=str(status or ""),
                progress_pct=pct,
                message=str(message or ""),
                extra={
                    "progress_bar": self._progress_bar(pct),
                    **(extra or {}),
                },
                max_entries=600,
            )
        except Exception:
            # Progress logging should never block task scheduling.
            pass

        self._task_progress_state[task_name] = {
            "ts": now_sec,
            "pct": pct,
            "status": str(status or ""),
        }

    @staticmethod
    def _snapshot_generated_at(snapshot: dict[str, Any] | None) -> pd.Timestamp | None:
        if not isinstance(snapshot, dict):
            return None
        ts = pd.to_datetime(snapshot.get("generated_at"), errors="coerce")
        return None if pd.isna(ts) else ts

    @classmethod
    def _reconciled_task_status(
        cls,
        task_name: str,
        status: dict[str, Any] | None,
        snapshot: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(status, dict):
            return status
        if str(status.get("status", "") or "") != "running":
            return status
        if not isinstance(snapshot, dict) or str(snapshot.get("status", "") or "") != "ok":
            return status

        extra = status.get("extra") if isinstance(status.get("extra"), dict) else {}
        started_at = pd.to_datetime(extra.get("started_at"), errors="coerce")
        snapshot_at = cls._snapshot_generated_at(snapshot)
        if pd.isna(started_at) or snapshot_at is None or snapshot_at < started_at:
            return status

        reconciled = dict(status)
        reconciled["status"] = "ok"
        reconciled["updated_at"] = snapshot_at.isoformat()
        reconciled["error"] = ""
        reconciled["extra"] = {
            **extra,
            "finished_at": snapshot_at.isoformat(),
            "progress_pct": 100,
            "progress_bar": cls._progress_bar(100),
            "progress_message": "任务完成",
            "reconciled_from_snapshot": True,
        }
        return reconciled

    def _reconcile_stale_task_statuses(self) -> None:
        for task_name in self.task_specs:
            status = read_task_status(task_name)
            snapshot = read_snapshot(task_name)
            reconciled = self._reconciled_task_status(task_name, status, snapshot)
            if reconciled is status or not isinstance(reconciled, dict):
                continue
            extra = reconciled.get("extra") if isinstance(reconciled.get("extra"), dict) else {}
            write_task_status(
                task_name,
                status=str(reconciled.get("status", "") or ""),
                error=str(reconciled.get("error", "") or ""),
                extra=extra,
            )

    def _last_effective_run(self, task_name: str) -> pd.Timestamp | None:
        latest = self.last_run.get(task_name)
        snapshot = read_snapshot(task_name)
        snapshot_ts = self._snapshot_generated_at(snapshot)
        if latest is None:
            return snapshot_ts
        if snapshot_ts is None:
            return latest
        return max(latest, snapshot_ts)

    def _is_due(self, task_name: str, interval_seconds: int, now: pd.Timestamp) -> bool:
        last = self._last_effective_run(task_name)
        if task_name in SCHEDULED_REFRESH_TASKS:
            slot = self._latest_scheduled_slot(now)
            if last is None:
                return True
            return bool(last < slot)
        if last is None:
            return True
        return (now - last).total_seconds() >= max(1, int(interval_seconds))

    @staticmethod
    def _latest_scheduled_slot(now: pd.Timestamp) -> pd.Timestamp:
        day_start = now.normalize()
        valid_hours = sorted({int(h) for h in SCHEDULED_REFRESH_HOURS if 0 <= int(h) <= 23})
        if not valid_hours:
            return day_start

        for hour in reversed(valid_hours):
            slot = day_start + pd.Timedelta(hours=hour)
            if slot <= now:
                return slot

        prev_day = day_start - pd.Timedelta(days=1)
        return prev_day + pd.Timedelta(hours=valid_hours[-1])

    @staticmethod
    def _snapshot_params(task_name: str) -> dict[str, Any]:
        snapshot = read_snapshot(task_name)
        if not isinstance(snapshot, dict):
            return {}
        payload = snapshot.get("payload")
        if not isinstance(payload, dict):
            return {}
        params = payload.get("params")
        return params if isinstance(params, dict) else {}

    def _task_dependency_blocked(self, spec: TaskSpec) -> bool:
        for dep in spec.dependencies:
            if dep in self.active_tasks:
                return True
            if has_pending_request(dep):
                return True
            if read_snapshot(dep) is None:
                return True
        return False

    def _resolve_task_params(self, task_name: str, request_data: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
        if isinstance(request_data, dict):
            params = request_data.get("payload") or {}
            force_refresh = bool(request_data.get("force", False))
            return dict(params), force_refresh
        params = dict(self._snapshot_params(task_name))
        if task_name in {"market_db_sync", "sector_fund_flow", "smart_pick", "dragon_radar", "ma5_pullback", "three_bull_pullback", "today_entry", "rebound_entry", "holding_advice"}:
            params.pop("end_date", None)
        return params, False

    def _queue_refresh_if_idle(self, task_name: str, force: bool = False) -> None:
        if task_name in self.active_tasks:
            return
        if has_pending_request(task_name):
            return
        request_refresh(task_name, force=force)

    def _start_task(self, spec: TaskSpec) -> bool:
        if spec.name in self.active_tasks:
            return False

        request_data = take_refresh_request(spec.name) if has_pending_request(spec.name) else None
        params, force_refresh = self._resolve_task_params(spec.name, request_data)
        available_methods = mp.get_all_start_methods()
        if "spawn" in available_methods:
            start_method = "spawn"
        elif "fork" in available_methods:
            start_method = "fork"
        else:
            start_method = available_methods[0]
        ctx = mp.get_context(start_method)
        out_queue: mp.Queue = ctx.Queue(maxsize=1)
        proc = ctx.Process(
            target=_task_process_main,
            args=(spec.name, dict(params or {}), bool(force_refresh), out_queue),
            daemon=True,
        )
        proc.start()
        started_at = pd.Timestamp.now()
        self.active_tasks[spec.name] = ActiveTask(
            spec=spec,
            process=proc,
            out_queue=out_queue,
            started_at=started_at,
            params=dict(params or {}),
            force_refresh=force_refresh,
        )
        progress_pct = 1
        progress_message = "任务已启动"
        task_timeout_seconds = max(0, int(spec.timeout_seconds or 0))
        write_task_status(
            spec.name,
            status="running",
            extra={
                "pid": os.getpid(),
                "child_pid": proc.pid,
                "started_at": started_at.isoformat(),
                "force_refresh": force_refresh,
                "timeout_seconds": task_timeout_seconds,
                "progress_pct": progress_pct,
                "progress_bar": self._progress_bar(progress_pct),
                "progress_message": progress_message,
                "params": params,
            },
        )
        self._append_progress_event(
            task_name=spec.name,
            status="running",
            progress_pct=progress_pct,
            message=(
                f"{progress_message}（超时阈值 {task_timeout_seconds}s）"
                if self._has_timeout(task_timeout_seconds)
                else f"{progress_message}（无时间阈值）"
            ),
            extra={
                "child_pid": proc.pid,
                "timeout_seconds": task_timeout_seconds,
                "force_refresh": force_refresh,
            },
            force=True,
        )
        return True

    @staticmethod
    def _read_result(out_queue: mp.Queue) -> dict[str, Any] | None:
        try:
            if out_queue.empty():
                return None
            result = out_queue.get_nowait()
            return result if isinstance(result, dict) else None
        except Exception:
            return None

    def _finalize_task_error(self, task_name: str, active: ActiveTask, error_text: str) -> None:
        finished_at = pd.Timestamp.now()
        duration_seconds = int((finished_at - active.started_at).total_seconds())
        progress_pct = 100
        progress_message = "任务失败"

        snapshot = read_snapshot(task_name)
        generated_at = self._snapshot_generated_at(snapshot)
        if (generated_at is None or generated_at < active.started_at) and should_write_error_snapshot(snapshot):
            write_snapshot(
                task_name,
                payload={"params": active.params},
                status="error",
                error=error_text,
            )
        write_task_status(
            task_name,
            status="error",
            error=error_text,
            extra={
                "pid": os.getpid(),
                "child_pid": active.process.pid,
                "started_at": active.started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "duration_seconds": duration_seconds,
                "progress_pct": progress_pct,
                "progress_bar": self._progress_bar(progress_pct),
                "progress_message": progress_message,
                "params": active.params,
            },
        )
        brief_error = str(error_text or "").splitlines()[0][:200]
        self._append_progress_event(
            task_name=task_name,
            status="error",
            progress_pct=progress_pct,
            message=f"失败：{brief_error or '未知错误'}",
            extra={
                "duration_seconds": duration_seconds,
            },
            force=True,
        )
        self._task_progress_state.pop(task_name, None)
        self.last_run[task_name] = finished_at

    def _finalize_task_success(self, task_name: str, active: ActiveTask) -> None:
        finished_at = pd.Timestamp.now()
        duration_seconds = int((finished_at - active.started_at).total_seconds())
        progress_pct = 100
        progress_message = "任务完成"

        write_task_status(
            task_name,
            status="ok",
            extra={
                "pid": os.getpid(),
                "child_pid": active.process.pid,
                "started_at": active.started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "duration_seconds": duration_seconds,
                "progress_pct": progress_pct,
                "progress_bar": self._progress_bar(progress_pct),
                "progress_message": progress_message,
                "params": active.params,
            },
        )
        self._append_progress_event(
            task_name=task_name,
            status="ok",
            progress_pct=progress_pct,
            message=f"完成：耗时 {duration_seconds}s",
            extra={
                "duration_seconds": duration_seconds,
            },
            force=True,
        )
        self._task_progress_state.pop(task_name, None)
        self.last_run[task_name] = finished_at

    def _poll_active_tasks(self) -> None:
        now = pd.Timestamp.now()
        for task_name, active in list(self.active_tasks.items()):
            elapsed = (now - active.started_at).total_seconds()
            if active.process.is_alive():
                timeout_seconds = max(0, int(active.spec.timeout_seconds or 0))
                progress_pct = self._running_progress(elapsed, timeout_seconds)
                progress_message = (
                    f"运行中 {int(elapsed)}s/{timeout_seconds}s"
                    if self._has_timeout(timeout_seconds)
                    else f"运行中 {int(elapsed)}s"
                )
                write_task_status(
                    task_name,
                    status="running",
                    extra={
                        "pid": os.getpid(),
                        "child_pid": active.process.pid,
                        "started_at": active.started_at.isoformat(),
                        "elapsed_seconds": int(elapsed),
                        "timeout_seconds": timeout_seconds,
                        "progress_pct": progress_pct,
                        "progress_bar": self._progress_bar(progress_pct),
                        "progress_message": progress_message,
                        "force_refresh": active.force_refresh,
                        "params": active.params,
                    },
                )
                self._append_progress_event(
                    task_name=task_name,
                    status="running",
                    progress_pct=progress_pct,
                    message=progress_message,
                    extra={
                        "elapsed_seconds": int(elapsed),
                        "timeout_seconds": timeout_seconds,
                    },
                    min_interval_seconds=10,
                )
                if not self._has_timeout(timeout_seconds):
                    continue
                if elapsed < max(10, timeout_seconds):
                    continue
                active.process.terminate()
                active.process.join(timeout=5)
                self._finalize_task_error(
                    task_name,
                    active,
                    f"{task_name} 后台任务执行超时（>{timeout_seconds}s）",
                )
                self.active_tasks.pop(task_name, None)
                continue

            result = self._read_result(active.out_queue)
            if active.process.exitcode == 0 and bool((result or {}).get("ok", False)):
                self._finalize_task_success(task_name, active)
            else:
                err = str((result or {}).get("error", "") or f"{task_name} 后台任务异常退出，exit_code={active.process.exitcode}")
                trace = str((result or {}).get("traceback", "") or "")
                self._finalize_task_error(task_name, active, f"{err}\n{trace}".strip())
            self.active_tasks.pop(task_name, None)

    def _candidate_specs(self, now: pd.Timestamp) -> list[TaskSpec]:
        candidates: list[TaskSpec] = []
        for spec in sorted(self.task_specs.values(), key=lambda item: item.priority):
            if spec.name in self.active_tasks:
                continue
            if self._task_dependency_blocked(spec):
                continue
            request_data = read_request(spec.name)
            if request_data is not None:
                candidates.append(spec)
                continue
            if self._is_due(spec.name, spec.interval_seconds, now):
                candidates.append(spec)
        return candidates

    def _schedule_due_tasks(self) -> None:
        now = pd.Timestamp.now()
        available_slots = max(0, int(self.max_concurrency) - len(self.active_tasks))
        if available_slots <= 0:
            return

        # Re-evaluate candidates after each launch so dependency rules are applied
        # against the latest active task set.
        for _ in range(available_slots):
            candidates = self._candidate_specs(now)
            if not candidates:
                break
            started = False
            for spec in candidates:
                if self._start_task(spec):
                    started = True
                    break
            if not started:
                break

    def _write_global_status(self) -> None:
        if self.active_tasks:
            active_names = sorted(self.active_tasks.keys())
            write_worker_status(
                status="running",
                current_task=",".join(active_names),
                extra={
                    "pid": os.getpid(),
                    "running_count": len(active_names),
                    "active_tasks": active_names,
                    "max_concurrency": self.max_concurrency,
                    "scheduled_hours": list(SCHEDULED_REFRESH_HOURS),
                    "scheduled_tasks": list(SCHEDULED_REFRESH_TASKS),
                },
            )
        else:
            write_worker_status(
                status="idle",
                current_task="",
                extra={
                    "pid": os.getpid(),
                    "running_count": 0,
                    "max_concurrency": self.max_concurrency,
                    "scheduled_hours": list(SCHEDULED_REFRESH_HOURS),
                    "scheduled_tasks": list(SCHEDULED_REFRESH_TASKS),
                },
            )

    def dispatch_task(self, task_name: str, params: dict[str, Any], force_refresh: bool) -> dict[str, Any]:
        mapping = {
            "market_db_sync": self._compute_market_db_sync,
            "sector_fund_flow": self._compute_sector_fund_flow,
            "smart_pick": self._compute_smart_pick,
            "dragon_radar": self._compute_dragon_radar,
            "ma5_pullback": self._compute_ma5_pullback,
            "three_bull_pullback": self._compute_three_bull_pullback,
            "today_entry": self._compute_today_entry,
            "rebound_entry": self._compute_rebound_entry,
            "holding_advice": self._compute_holding_advice,
            "cache_cleanup": self._compute_cache_cleanup,
        }
        fn = mapping.get(task_name)
        if fn is None:
            raise ValueError(f"未知后台任务: {task_name}")
        return fn(params=params, force_refresh=force_refresh)

    def _compute_sector_fund_flow(self, params: dict[str, Any], force_refresh: bool) -> dict[str, Any]:
        indicators = list(params.get("indicators") or ["当日", "3日", "10日"])
        sector_type = str(params.get("sector_type", "行业资金流") or "行业资金流")
        return self.data_manager.build_sector_fund_flow_snapshot(
            indicators=indicators,
            sector_type=sector_type,
            force_refresh=bool(force_refresh),
        )

    def _compute_smart_pick(self, params: dict[str, Any], force_refresh: bool) -> dict[str, Any]:
        now = pd.Timestamp.now()
        end_date = str(params.get("end_date") or now.strftime("%Y%m%d"))
        lookback_days = int(params.get("lookback_days", 90))
        top_n = int(params.get("top_n", 8))
        total_position_pct = float(params.get("total_position_pct", 60.0))
        universe_size = int(params.get("universe_size", 200))
        max_single_position_pct = float(params.get("max_single_position_pct", 10.0))
        sector_flow_indicator = str(params.get("sector_flow_indicator", "10日"))
        macro_enabled = bool(params.get("macro_enabled", True))
        recent_signal_days = int(params.get("recent_signal_days", 5))
        threshold = float(params.get("threshold", 0.1))
        enabled_factors = list(params.get("enabled_factors") or ["momentum_20"])
        factor_weights = dict(params.get("factor_weights") or {k: 1.0 for k in enabled_factors})

        smart_df = self.data_manager.recommend_scientific_candidates(
            end_date=end_date,
            lookback_days=lookback_days,
            top_n=top_n,
            total_position_pct=total_position_pct,
            universe_size=universe_size,
            max_single_position_pct=max_single_position_pct,
            sector_flow_indicator=sector_flow_indicator,
            macro_enabled=macro_enabled,
            force_refresh=bool(force_refresh),
        )
        if smart_df is None:
            smart_df = pd.DataFrame()

        if not smart_df.empty and "推荐评分" in smart_df.columns:
            median_score = float(pd.to_numeric(smart_df["推荐评分"], errors="coerce").median())
            smart_df = smart_df.copy()
            smart_df["操作建议"] = [
                "可考虑建仓" if float(v) >= median_score else "观察"
                for v in pd.to_numeric(smart_df["推荐评分"], errors="coerce").fillna(0.0)
            ]

        recent_entry_signal_df = self.data_manager.list_recent_sector_entry_signals(
            end_date=end_date,
            lookback_days=lookback_days,
            universe_size=0,
            recent_signal_days=recent_signal_days,
            threshold=threshold,
            enabled_factors=enabled_factors,
            factor_weights=factor_weights,
            sector_flow_indicator=sector_flow_indicator,
            macro_enabled=macro_enabled,
            force_refresh=False,
        )

        return {
            "params": {
                "end_date": end_date,
                "lookback_days": lookback_days,
                "top_n": top_n,
                "total_position_pct": total_position_pct,
                "universe_size": universe_size,
                "max_single_position_pct": max_single_position_pct,
                "sector_flow_indicator": sector_flow_indicator,
                "macro_enabled": macro_enabled,
                "recent_signal_days": recent_signal_days,
                "threshold": threshold,
                "enabled_factors": enabled_factors,
                "factor_weights": factor_weights,
            },
            "smart_pick_df": smart_df,
            "sector_rotation_df": smart_df.attrs.get("sector_rotation_df", pd.DataFrame()) if isinstance(smart_df, pd.DataFrame) else pd.DataFrame(),
            "sector_rotation_summary": smart_df.attrs.get("sector_rotation_summary", {}) if isinstance(smart_df, pd.DataFrame) else {},
            "macro_regime": smart_df.attrs.get("macro_regime", {}) if isinstance(smart_df, pd.DataFrame) else {},
            "macro_detail_df": smart_df.attrs.get("macro_detail_df", pd.DataFrame()) if isinstance(smart_df, pd.DataFrame) else pd.DataFrame(),
            "recent_entry_signal_df": recent_entry_signal_df if isinstance(recent_entry_signal_df, pd.DataFrame) else pd.DataFrame(),
        }

    def _compute_market_db_sync(self, params: dict[str, Any], force_refresh: bool) -> dict[str, Any]:
        now = pd.Timestamp.now()
        end_date = str(params.get("end_date") or now.strftime("%Y%m%d"))
        lookback_trade_days = int(params.get("lookback_trade_days", 60))
        sync_result = self.data_manager.sync_market_data_window_db(
            end_date=end_date,
            lookback_trade_days=lookback_trade_days,
            force_refresh=bool(force_refresh),
        )
        if isinstance(sync_result, dict) and str(sync_result.get("status", "") or "") == "ok":
            sync_reason = str(sync_result.get("sync_reason", "") or "")
            if sync_reason in {"close", "catchup", "bootstrap", "force"}:
                for task_name in ["sector_fund_flow", "smart_pick", "dragon_radar", "ma5_pullback", "three_bull_pullback", "today_entry", "rebound_entry"]:
                    self._queue_refresh_if_idle(task_name, force=True)
        return {
            "params": {
                "end_date": end_date,
                "lookback_trade_days": lookback_trade_days,
            },
            "market_db_status": sync_result,
        }

    def _compute_dragon_radar(self, params: dict[str, Any], force_refresh: bool) -> dict[str, Any]:
        now = pd.Timestamp.now()
        end_date = str(params.get("end_date") or now.strftime("%Y%m%d"))
        lookback_days = int(params.get("lookback_days", 100))
        top_n = int(params.get("top_n", 10))
        universe_size = int(params.get("universe_size", 200))
        gap_threshold = float(params.get("gap_threshold", 0.01))

        dragon_df = self.data_manager.recommend_dragon_candidates(
            end_date=end_date,
            lookback_days=lookback_days,
            top_n=top_n,
            universe_size=universe_size,
            gap_threshold=gap_threshold,
            force_refresh=bool(force_refresh),
        )
        if dragon_df is None:
            dragon_df = pd.DataFrame()

        return {
            "params": {
                "end_date": end_date,
                "lookback_days": lookback_days,
                "top_n": top_n,
                "universe_size": universe_size,
                "gap_threshold": gap_threshold,
            },
            "dragon_radar_df": dragon_df,
        }

    def _compute_ma5_pullback(self, params: dict[str, Any], force_refresh: bool) -> dict[str, Any]:
        now = pd.Timestamp.now()
        end_date = str(params.get("end_date") or now.strftime("%Y%m%d"))
        lookback_days = int(params.get("lookback_days", 120))
        top_n = int(params.get("top_n", 10))
        universe_size = int(params.get("universe_size", 220))
        pullback_tolerance = float(params.get("pullback_tolerance", 0.015))
        min_factor_score = float(params.get("min_factor_score", 0.05))

        pullback_df = self.data_manager.recommend_ma5_pullback_candidates(
            end_date=end_date,
            lookback_days=lookback_days,
            top_n=top_n,
            universe_size=universe_size,
            pullback_tolerance=pullback_tolerance,
            min_factor_score=min_factor_score,
            force_refresh=bool(force_refresh),
        )
        if pullback_df is None:
            pullback_df = pd.DataFrame()

        return {
            "params": {
                "end_date": end_date,
                "lookback_days": lookback_days,
                "top_n": top_n,
                "universe_size": universe_size,
                "pullback_tolerance": pullback_tolerance,
                "min_factor_score": min_factor_score,
            },
            "ma5_pullback_df": pullback_df,
        }

    def _compute_three_bull_pullback(self, params: dict[str, Any], force_refresh: bool) -> dict[str, Any]:
        from config import settings
        from core.strategies.three_bull_pullback import (
            ThreeBullPullbackConfig,
            append_forward_returns,
            build_three_bull_pullback_signals,
            rolling_backtest_summary,
            summarize_score_bins,
            summarize_three_bull_backtest,
        )
        from scripts.three_bull_pullback_report import (
            build_markdown_report,
            latest_top_candidates,
            load_market_panel,
        )

        now = pd.Timestamp.now()
        requested_start = str(params.get("start_date", "") or "").strip()
        end_date = pd.to_datetime(str(params.get("end_date") or now.strftime("%Y%m%d")), errors="coerce")
        end_date = end_date.strftime("%Y%m%d") if pd.notna(end_date) else now.strftime("%Y%m%d")
        warmup_calendar_days = int(params.get("warmup_calendar_days", 180))
        min_score = float(params.get("min_score", 60.0))
        top_n_values = self._int_list_param(params.get("top_n_values"), [3, 5, 10, 20])
        horizons = self._int_list_param(params.get("horizons"), [1, 3, 5])
        horizon_days = int(params.get("horizon_days", 3))
        target_gain_pct = float(params.get("target_gain_pct", 3.0))
        max_drawdown_pct = float(params.get("max_drawdown_pct", 3.0))
        fee_rate = float(params.get("fee_rate", 0.0005))
        latest_top_n = int(params.get("latest_top_n", 20))
        min_confirm_volume = float(params.get("min_confirm_volume", 1.20))
        min_confirm_gain_pct = float(params.get("min_confirm_gain_pct", 1.5))
        max_confirm_gain_pct = float(params.get("max_confirm_gain_pct", 7.5))
        min_market_score = float(params.get("min_market_score", 0.0))
        min_sector_score = float(params.get("min_sector_score", 0.0))

        load_start = requested_start
        if requested_start:
            start_ts = pd.to_datetime(requested_start, errors="coerce")
            if pd.notna(start_ts):
                load_start = (start_ts - pd.Timedelta(days=max(0, warmup_calendar_days))).strftime("%Y%m%d")

        panel = load_market_panel(
            db_path=settings.market_db_path,
            start_date=load_start,
            end_date=end_date,
        )
        if panel.empty:
            raise RuntimeError(f"三阳回踩策略未找到可用市场数据：{settings.market_db_path}")

        config = ThreeBullPullbackConfig(
            min_score=min_score,
            include_watch=False,
            min_confirm_volume_ratio=min_confirm_volume,
            min_confirm_gain=min_confirm_gain_pct / 100.0,
            max_confirm_gain=max_confirm_gain_pct / 100.0,
            min_market_score=min_market_score,
            min_sector_score=min_sector_score,
        )
        latest_config = ThreeBullPullbackConfig(
            min_score=min_score,
            include_watch=True,
            min_confirm_volume_ratio=min_confirm_volume,
            min_confirm_gain=min_confirm_gain_pct / 100.0,
            max_confirm_gain=max_confirm_gain_pct / 100.0,
            min_market_score=min_market_score,
            min_sector_score=min_sector_score,
        )

        signals = build_three_bull_pullback_signals(panel, config=config)
        latest_signals = build_three_bull_pullback_signals(panel, config=latest_config)
        if requested_start:
            signal_start_ts = pd.to_datetime(requested_start, errors="coerce")
            if pd.notna(signal_start_ts):
                if not signals.empty:
                    signals = signals[signals["trade_date"] >= signal_start_ts].reset_index(drop=True)
                if not latest_signals.empty:
                    latest_signals = latest_signals[latest_signals["trade_date"] >= signal_start_ts].reset_index(drop=True)

        trades = append_forward_returns(signals, panel, horizons=horizons, fee_rate=fee_rate)
        latest_top_df = latest_top_candidates(latest_signals, top_n=latest_top_n)
        latest_fallback_used = False
        if latest_top_df.empty:
            latest_top_df = self._fallback_three_bull_candidates(panel, top_n=latest_top_n)
            latest_fallback_used = not latest_top_df.empty
        latest_buy_df = latest_top_candidates(signals, top_n=latest_top_n)
        summary_df = summarize_three_bull_backtest(
            trades,
            top_n_values=top_n_values,
            target_gain_pct=target_gain_pct,
            max_drawdown_pct=max_drawdown_pct,
            horizon_days=horizon_days,
        )
        score_bins_df = summarize_score_bins(
            trades,
            target_gain_pct=target_gain_pct,
            max_drawdown_pct=max_drawdown_pct,
            horizon_days=horizon_days,
        )
        rolling_df = rolling_backtest_summary(
            trades,
            top_n=max(top_n_values) if top_n_values else 5,
            window_days=int(params.get("rolling_window_days", 20)),
            step_days=int(params.get("rolling_step_days", 5)),
        )

        output_dir = settings.cache_dir / "backtests" / "three_bull_pullback"
        output_dir.mkdir(parents=True, exist_ok=True)
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
        (output_dir / "report.md").write_text(markdown + "\n", encoding="utf-8")

        return {
            "params": {
                "end_date": end_date,
                "start_date": requested_start,
                "min_score": min_score,
                "top_n_values": top_n_values,
                "horizons": horizons,
                "horizon_days": horizon_days,
                "latest_top_n": latest_top_n,
                "min_confirm_volume": min_confirm_volume,
                "min_confirm_gain_pct": min_confirm_gain_pct,
                "max_confirm_gain_pct": max_confirm_gain_pct,
                "min_market_score": min_market_score,
                "min_sector_score": min_sector_score,
                "latest_fallback_used": latest_fallback_used,
            },
            "three_bull_latest_df": latest_top_df,
            "three_bull_buy_df": latest_buy_df,
            "three_bull_summary_df": summary_df,
            "three_bull_score_bins_df": score_bins_df,
            "three_bull_rolling_df": rolling_df,
            "signal_count": int(len(signals)),
            "trade_count": int(len(trades)),
            "output_dir": str(output_dir),
        }

    def _compute_today_entry(self, params: dict[str, Any], force_refresh: bool) -> dict[str, Any]:
        from app import build_today_entry_plan

        now = pd.Timestamp.now()
        end_date = str(params.get("end_date") or now.strftime("%Y%m%d"))
        end_dt = pd.to_datetime(end_date, format="%Y%m%d", errors="coerce")
        if pd.isna(end_dt):
            end_dt = now

        risk_profile = str(params.get("risk_profile", "均衡"))
        threshold = float(params.get("threshold", 0.1))
        selected_factor_codes = list(params.get("enabled_factors") or ["momentum_20"])
        factor_weights = dict(params.get("factor_weights") or {k: 1.0 for k in selected_factor_codes})

        smart_snapshot = read_snapshot("smart_pick")
        base_candidates = pd.DataFrame()
        if smart_snapshot and isinstance(smart_snapshot.get("payload"), dict):
            maybe_df = smart_snapshot["payload"].get("smart_pick_df")
            if isinstance(maybe_df, pd.DataFrame):
                base_candidates = maybe_df.copy()

        target_top_n = max(1, min(20, int(params.get("top_n", 20) or 20)))
        smart_params = dict(params)
        smart_params["top_n"] = target_top_n
        if base_candidates.empty or len(base_candidates) < target_top_n:
            if force_refresh:
                smart_payload = self._compute_smart_pick(params=smart_params, force_refresh=True)
                maybe_df = smart_payload.get("smart_pick_df")
                if isinstance(maybe_df, pd.DataFrame) and not maybe_df.empty:
                    base_candidates = maybe_df.copy()
            else:
                # In scheduled mode, avoid nested heavy recompute to reduce timeout risk.
                # Keep current candidates as-is and wait for next scheduled smart_pick refresh.
                pass

        lookback_days = max(60, int(params.get("lookback_days", 80)))
        right_side_df = self.data_manager.recommend_right_side_confirmation_candidates(
            end_date=end_date,
            lookback_days=lookback_days,
            top_n=target_top_n,
            force_refresh=bool(force_refresh),
        )
        right_side_watch_df = self.data_manager.recommend_right_side_watch_candidates(
            end_date=end_date,
            lookback_days=lookback_days,
            force_refresh=bool(force_refresh),
        )
        if "候选来源" not in base_candidates.columns and not base_candidates.empty:
            base_candidates = base_candidates.copy()
            base_candidates["候选来源"] = "科学选股"
        if isinstance(right_side_df, pd.DataFrame) and not right_side_df.empty:
            if base_candidates.empty:
                base_candidates = right_side_df.copy()
            else:
                merged = base_candidates.copy()
                for _, right_row in right_side_df.iterrows():
                    ts_code = str(right_row.get("ts_code", "") or "").strip()
                    if not ts_code:
                        continue
                    hit_mask = merged["ts_code"].astype(str).eq(ts_code)
                    if bool(hit_mask.any()):
                        idx = merged.index[hit_mask][0]
                        merged.at[idx, "候选来源"] = "科学选股+右侧确认"
                        for col, value in right_row.items():
                            if col == "ts_code":
                                continue
                            if col not in merged.columns or pd.isna(merged.at[idx, col]) or str(merged.at[idx, col]).strip() in {"", "nan", "None"}:
                                merged.at[idx, col] = value
                            elif col in {"右侧确认分", "信号新鲜度", "买点评分"}:
                                merged.at[idx, col] = max(float(merged.at[idx, col] or 0.0), float(value or 0.0))
                    else:
                        merged = pd.concat([merged, pd.DataFrame([right_row])], ignore_index=True, sort=False)
                base_candidates = merged

        plan_df, summary = build_today_entry_plan(
            data_manager=self.data_manager,
            base_candidates=base_candidates,
            end_dt=end_dt.date() if isinstance(end_dt, pd.Timestamp) else date.today(),
            selected_factor_codes=selected_factor_codes,
            factor_weights=factor_weights,
            threshold=threshold,
            risk_profile=risk_profile,
        )

        return {
            "params": {
                "end_date": end_date,
                "risk_profile": risk_profile,
                "threshold": threshold,
                "enabled_factors": selected_factor_codes,
                "factor_weights": factor_weights,
            },
            "today_entry_df": plan_df if isinstance(plan_df, pd.DataFrame) else pd.DataFrame(),
            "today_entry_summary": summary if isinstance(summary, dict) else {},
            "right_side_watch_df": right_side_watch_df if isinstance(right_side_watch_df, pd.DataFrame) else pd.DataFrame(),
        }

    def _compute_rebound_entry(self, params: dict[str, Any], force_refresh: bool) -> dict[str, Any]:
        from app import build_bottom_rebound_entry_plan

        now = pd.Timestamp.now()
        end_date = str(params.get("end_date") or now.strftime("%Y%m%d"))
        end_dt = pd.to_datetime(end_date, format="%Y%m%d", errors="coerce")
        if pd.isna(end_dt):
            end_dt = now

        risk_profile = str(params.get("risk_profile", "均衡"))
        threshold = float(params.get("threshold", 0.1))
        selected_factor_codes = list(params.get("enabled_factors") or ["momentum_20"])
        factor_weights = dict(params.get("factor_weights") or {k: 1.0 for k in selected_factor_codes})
        lookback_days = max(60, int(params.get("lookback_days", 90)))
        target_top_n = max(1, min(20, int(params.get("top_n", 20) or 20)))

        rebound_df = self.data_manager.recommend_bottom_rebound_candidates(
            end_date=end_date,
            lookback_days=lookback_days,
            top_n=target_top_n,
            force_refresh=bool(force_refresh),
        )
        rebound_watch_df = self.data_manager.recommend_bottom_rebound_watch_candidates(
            end_date=end_date,
            lookback_days=lookback_days,
            force_refresh=bool(force_refresh),
        )

        candidate_pool = pd.DataFrame()
        parts = []
        if isinstance(rebound_df, pd.DataFrame) and not rebound_df.empty:
            parts.append(rebound_df.copy())
        if isinstance(rebound_watch_df, pd.DataFrame) and not rebound_watch_df.empty:
            parts.append(rebound_watch_df.head(target_top_n).copy())
        if parts:
            candidate_pool = pd.concat(parts, ignore_index=True, sort=False)
            if "ts_code" in candidate_pool.columns:
                candidate_pool = candidate_pool.drop_duplicates(subset=["ts_code"], keep="first").reset_index(drop=True)

        plan_df, summary = build_bottom_rebound_entry_plan(
            data_manager=self.data_manager,
            rebound_candidates=candidate_pool,
            end_dt=end_dt.date() if isinstance(end_dt, pd.Timestamp) else date.today(),
            selected_factor_codes=selected_factor_codes,
            factor_weights=factor_weights,
            threshold=threshold,
            risk_profile=risk_profile,
        )

        return {
            "params": {
                "end_date": end_date,
                "risk_profile": risk_profile,
                "threshold": threshold,
                "enabled_factors": selected_factor_codes,
                "factor_weights": factor_weights,
                "lookback_days": lookback_days,
                "top_n": target_top_n,
            },
            "rebound_entry_df": plan_df if isinstance(plan_df, pd.DataFrame) else pd.DataFrame(),
            "rebound_entry_summary": summary if isinstance(summary, dict) else {},
            "rebound_watch_df": rebound_watch_df if isinstance(rebound_watch_df, pd.DataFrame) else pd.DataFrame(),
        }

    def _compute_holding_advice(self, params: dict[str, Any], force_refresh: bool) -> dict[str, Any]:
        from app import build_holding_action_advice, load_holdings, save_holdings

        holdings_df = load_holdings()
        if holdings_df is None or holdings_df.empty:
            return {
                "params": params,
                "holding_advice_df": pd.DataFrame(),
                "updated_holdings_df": pd.DataFrame(),
            }

        threshold = float(params.get("threshold", 0.1))
        selected_factor_codes = list(params.get("enabled_factors") or ["momentum_20"])
        factor_weights = dict(params.get("factor_weights") or {k: 1.0 for k in selected_factor_codes})
        stop_profit_pct = float(params.get("stop_profit_pct", 0.10))
        stop_loss_pct = float(params.get("stop_loss_pct", 0.05))
        end_date = str(params.get("end_date") or pd.Timestamp.now().strftime("%Y%m%d"))

        advice_df, updated_holdings = build_holding_action_advice(
            data_manager=self.data_manager,
            holdings_df=holdings_df,
            end_date=end_date,
            selected_factor_codes=selected_factor_codes,
            factor_weights=factor_weights,
            threshold=threshold,
            stop_profit_pct=stop_profit_pct,
            stop_loss_pct=stop_loss_pct,
            quote_force_refresh=bool(force_refresh),
        )
        if isinstance(updated_holdings, pd.DataFrame) and (not updated_holdings.empty):
            save_holdings(updated_holdings)

        return {
            "params": {
                "end_date": end_date,
                "threshold": threshold,
                "enabled_factors": selected_factor_codes,
                "factor_weights": factor_weights,
                "stop_profit_pct": stop_profit_pct,
                "stop_loss_pct": stop_loss_pct,
            },
            "holding_advice_df": advice_df if isinstance(advice_df, pd.DataFrame) else pd.DataFrame(),
            "updated_holdings_df": updated_holdings if isinstance(updated_holdings, pd.DataFrame) else pd.DataFrame(),
            "advice_attrs": getattr(advice_df, "attrs", {}),
        }

    def _compute_cache_cleanup(self, params: dict[str, Any], force_refresh: bool) -> dict[str, Any]:
        stats = cleanup_cache()
        return {"stats": stats, "params": params, "force_refresh": force_refresh}

    def _has_due_work(self) -> bool:
        now = pd.Timestamp.now()
        return bool(self._candidate_specs(now))

    def run_once(self, wait_until_idle: bool = False) -> None:
        self._reconcile_stale_task_statuses()
        while True:
            self._poll_active_tasks()
            self._schedule_due_tasks()
            self._write_global_status()
            if not wait_until_idle:
                return
            if (not self.active_tasks) and (not self._has_due_work()):
                return
            time.sleep(1)

    def run_forever(self, poll_seconds: int = 15) -> None:
        write_worker_status(
            status="starting",
            current_task="",
            extra={
                "pid": os.getpid(),
                "max_concurrency": self.max_concurrency,
                "scheduled_hours": list(SCHEDULED_REFRESH_HOURS),
            },
        )

        while True:
            self.run_once(wait_until_idle=False)
            time.sleep(max(1, int(poll_seconds)))


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run quant precompute worker")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--poll-seconds", type=int, default=15, help="Polling interval in seconds")
    parser.add_argument("--max-concurrency", type=int, default=3, help="Maximum concurrent background tasks")
    return parser


def main() -> None:
    args = _build_arg_parser().parse_args()
    worker = PrecomputeWorker(max_concurrency=int(args.max_concurrency))
    if args.once:
        worker.run_once(wait_until_idle=True)
        return
    worker.run_forever(poll_seconds=int(args.poll_seconds))


if __name__ == "__main__":
    main()
