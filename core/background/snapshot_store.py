from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

from config import settings


SNAPSHOT_DIR = settings.cache_dir / "snapshots"
REQUEST_DIR = settings.cache_dir / "requests"
TASK_STATUS_DIR = settings.cache_dir / "task_status"
WORKER_STATUS_PATH = SNAPSHOT_DIR / "_worker_status.json"
TASK_PROGRESS_LOG_PATH = TASK_STATUS_DIR / "_progress_log.json"


def _ensure_dirs() -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    TASK_STATUS_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=str(path.parent), delete=False, encoding="utf-8") as tmp:
        tmp.write(text)
        temp_name = tmp.name
    Path(temp_name).replace(path)


def _serialize_obj(obj: Any) -> Any:
    if isinstance(obj, pd.DataFrame):
        safe = obj.copy()
        for col in safe.columns:
            if pd.api.types.is_datetime64_any_dtype(safe[col]):
                safe[col] = safe[col].dt.strftime("%Y-%m-%dT%H:%M:%S")
        safe = safe.where(pd.notna(safe), None)
        return {
            "__type__": "dataframe",
            "columns": list(safe.columns),
            "records": safe.to_dict(orient="records"),
        }
    if isinstance(obj, dict):
        return {str(k): _serialize_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize_obj(v) for v in obj]
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    return obj


def _deserialize_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        if obj.get("__type__") == "dataframe":
            cols = list(obj.get("columns") or [])
            records = obj.get("records") or []
            out = pd.DataFrame(records)
            if cols:
                for col in cols:
                    if col not in out.columns:
                        out[col] = None
                out = out[cols]
            return out
        return {k: _deserialize_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deserialize_obj(v) for v in obj]
    return obj


def _snapshot_path(task_name: str) -> Path:
    return SNAPSHOT_DIR / f"{task_name}.json"


def _request_path(task_name: str) -> Path:
    return REQUEST_DIR / f"{task_name}.json"


def _task_status_path(task_name: str) -> Path:
    return TASK_STATUS_DIR / f"{task_name}.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def write_snapshot(task_name: str, payload: dict[str, Any], status: str = "ok", error: str = "") -> None:
    _ensure_dirs()
    body = {
        "task_name": str(task_name),
        "generated_at": pd.Timestamp.now().isoformat(),
        "status": str(status),
        "error": str(error or ""),
        "payload": _serialize_obj(payload or {}),
    }
    _atomic_write_text(_snapshot_path(task_name), json.dumps(body, ensure_ascii=False, indent=2))


def read_snapshot(task_name: str) -> dict[str, Any] | None:
    data = _read_json(_snapshot_path(task_name))
    if not isinstance(data, dict):
        return None
    data["payload"] = _deserialize_obj(data.get("payload") or {})
    return data


def request_refresh(task_name: str, payload: dict[str, Any] | None = None, force: bool = False) -> None:
    _ensure_dirs()
    body = {
        "task_name": str(task_name),
        "requested_at": pd.Timestamp.now().isoformat(),
        "force": bool(force),
        "payload": _serialize_obj(payload or {}),
    }
    _atomic_write_text(_request_path(task_name), json.dumps(body, ensure_ascii=False, indent=2))


def read_request(task_name: str) -> dict[str, Any] | None:
    data = _read_json(_request_path(task_name))
    if not isinstance(data, dict):
        return None
    data["payload"] = _deserialize_obj(data.get("payload") or {})
    return data


def take_refresh_request(task_name: str) -> dict[str, Any] | None:
    path = _request_path(task_name)
    data = _read_json(path)
    if data is None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        return None
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
    data["payload"] = _deserialize_obj(data.get("payload") or {})
    return data


def has_pending_request(task_name: str) -> bool:
    return _request_path(task_name).exists()


def clear_request(task_name: str) -> None:
    try:
        _request_path(task_name).unlink(missing_ok=True)
    except Exception:
        pass


def write_task_status(task_name: str, status: str, error: str = "", extra: dict[str, Any] | None = None) -> None:
    _ensure_dirs()
    body = {
        "task_name": str(task_name),
        "updated_at": pd.Timestamp.now().isoformat(),
        "status": str(status or ""),
        "error": str(error or ""),
        "extra": _serialize_obj(extra or {}),
    }
    _atomic_write_text(_task_status_path(task_name), json.dumps(body, ensure_ascii=False, indent=2))


def read_task_status(task_name: str) -> dict[str, Any] | None:
    data = _read_json(_task_status_path(task_name))
    if not isinstance(data, dict):
        return None
    data["extra"] = _deserialize_obj(data.get("extra") or {})
    return data


def clear_task_status(task_name: str) -> None:
    try:
        _task_status_path(task_name).unlink(missing_ok=True)
    except Exception:
        pass


def write_worker_status(status: str, current_task: str = "", error: str = "", extra: dict[str, Any] | None = None) -> None:
    _ensure_dirs()
    body = {
        "updated_at": pd.Timestamp.now().isoformat(),
        "status": str(status or ""),
        "current_task": str(current_task or ""),
        "error": str(error or ""),
        "extra": _serialize_obj(extra or {}),
    }
    _atomic_write_text(WORKER_STATUS_PATH, json.dumps(body, ensure_ascii=False, indent=2))


def read_worker_status() -> dict[str, Any] | None:
    data = _read_json(WORKER_STATUS_PATH)
    if not isinstance(data, dict):
        return None
    data["extra"] = _deserialize_obj(data.get("extra") or {})
    return data


def append_task_progress_log(
    task_name: str,
    status: str,
    progress_pct: int | float,
    message: str = "",
    extra: dict[str, Any] | None = None,
    max_entries: int = 600,
) -> None:
    _ensure_dirs()
    max_entries = max(50, int(max_entries or 0))

    data = _read_json(TASK_PROGRESS_LOG_PATH)
    events_raw = data.get("events") if isinstance(data, dict) else []
    events = list(events_raw) if isinstance(events_raw, list) else []

    now_iso = pd.Timestamp.now().isoformat()
    events.append(
        _serialize_obj(
            {
                "event_at": now_iso,
                "task_name": str(task_name or ""),
                "status": str(status or ""),
                "progress_pct": int(max(0, min(100, float(progress_pct or 0)))),
                "message": str(message or ""),
                "extra": extra or {},
            }
        )
    )
    if len(events) > max_entries:
        events = events[-max_entries:]

    body = {
        "updated_at": now_iso,
        "max_entries": max_entries,
        "events": events,
    }
    _atomic_write_text(TASK_PROGRESS_LOG_PATH, json.dumps(body, ensure_ascii=False, indent=2))


def read_task_progress_log(limit: int = 120) -> list[dict[str, Any]]:
    data = _read_json(TASK_PROGRESS_LOG_PATH)
    if not isinstance(data, dict):
        return []
    events_raw = data.get("events")
    if not isinstance(events_raw, list):
        return []

    out: list[dict[str, Any]] = []
    for item in events_raw:
        if not isinstance(item, dict):
            continue
        event = _deserialize_obj(item)
        if isinstance(event, dict):
            event["extra"] = _deserialize_obj(event.get("extra") or {})
            out.append(event)

    if limit and int(limit) > 0:
        return out[-int(limit):]
    return out
