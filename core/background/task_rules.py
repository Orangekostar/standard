from __future__ import annotations

from datetime import datetime
from typing import Any


def _parse_iso_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _age_seconds(ts: datetime | None, now: datetime | None = None) -> float | None:
    if ts is None:
        return None
    current = now or datetime.now(ts.tzinfo)
    return max(0.0, (current - ts).total_seconds())


def _timeout_error_text(error_text: str) -> bool:
    return "后台任务执行超时" in str(error_text or "")


def _worker_is_healthy(
    worker_status: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    stale_seconds: int = 90,
) -> bool:
    if not isinstance(worker_status, dict):
        return False
    age = _age_seconds(_parse_iso_timestamp(worker_status.get("updated_at")), now=now)
    if age is None:
        return False
    return age <= max(30, int(stale_seconds))


def snapshot_is_successful(snapshot: dict[str, Any] | None) -> bool:
    return isinstance(snapshot, dict) and str(snapshot.get("status", "") or "") == "ok"


def should_write_error_snapshot(previous_snapshot: dict[str, Any] | None) -> bool:
    return not snapshot_is_successful(previous_snapshot)


def should_hide_legacy_timeout_error(
    task_status: dict[str, Any] | None,
    worker_status: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    worker_stale_seconds: int = 90,
    grace_seconds: int = 300,
) -> bool:
    if not isinstance(task_status, dict):
        return False
    if str(task_status.get("status", "") or "") != "error":
        return False
    if not _timeout_error_text(str(task_status.get("error", "") or "")):
        return False
    age = _age_seconds(_parse_iso_timestamp(task_status.get("updated_at")), now=now)
    if age is None or age <= max(60, int(grace_seconds)):
        return False
    return _worker_is_healthy(worker_status, now=now, stale_seconds=worker_stale_seconds)


def latest_success_timestamp(task_status: dict[str, Any] | None, snapshot: dict[str, Any] | None) -> str:
    if isinstance(task_status, dict) and str(task_status.get("status", "") or "") == "ok":
        extra = task_status.get("extra", {}) if isinstance(task_status.get("extra"), dict) else {}
        finished_at = str(extra.get("finished_at", "") or "")
        if finished_at:
            return finished_at
        updated_at = str(task_status.get("updated_at", "") or "")
        if updated_at:
            return updated_at
    if snapshot_is_successful(snapshot):
        return str(snapshot.get("generated_at", "") or "")
    return ""
