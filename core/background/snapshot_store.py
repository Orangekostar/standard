from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

import pandas as pd

from config import settings
from core.technical_v2.contracts import (
    ContractError,
    canonical_json,
    json_safe,
    sha256_json,
)

SNAPSHOT_DIR = settings.cache_dir / "snapshots"
REQUEST_DIR = settings.cache_dir / "requests"
TASK_STATUS_DIR = settings.cache_dir / "task_status"
WORKER_STATUS_PATH = SNAPSHOT_DIR / "_worker_status.json"
TASK_PROGRESS_LOG_PATH = TASK_STATUS_DIR / "_progress_log.json"


class ArtifactMismatch(ContractError):
    """Raised when an immutable artifact or binding conflicts with recorded state."""


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


def _atomic_create_text(path: Path, text: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=str(path.parent), delete=False, encoding="utf-8") as tmp:
        tmp.write(text)
        temp_name = Path(tmp.name)
    try:
        os.link(temp_name, path)
        return True
    except FileExistsError:
        return False
    finally:
        temp_name.unlink(missing_ok=True)


def _safe_component(value: Any, label: str) -> str:
    component = str(value or "").strip()
    if not component or component in {".", ".."} or "/" in component or "\\" in component:
        raise ContractError(f"invalid {label}: {value}")
    return component


def write_immutable_json(path: str | Path, payload: dict[str, Any]) -> str:
    target = Path(path)
    safe_payload = json_safe(payload)
    if not isinstance(safe_payload, dict):
        raise ContractError("immutable JSON payload must be an object")
    artifact_hash = sha256_json(safe_payload)
    rendered = json.dumps(safe_payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if _atomic_create_text(target, rendered):
        return artifact_hash
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactMismatch(f"cannot validate existing artifact {target}: {exc}") from exc
    if canonical_json(existing) != canonical_json(safe_payload):
        raise ArtifactMismatch(f"immutable artifact conflict: {target}")
    return artifact_hash


def write_run_manifest(artifact_root: str | Path, manifest: dict[str, Any]) -> tuple[Path, str]:
    run_id = _safe_component(manifest.get("run_id"), "run_id")
    path = Path(artifact_root) / run_id / "run_manifest.json"
    return path, write_immutable_json(path, manifest)


def write_publication_manifest(artifact_root: str | Path, manifest: dict[str, Any]) -> dict[str, Any]:
    root = Path(artifact_root)
    required = {"run_id", "as_of_trade_date", "data_hash", "config_hash", "code_hash", "routes"}
    if missing := sorted(required.difference(manifest)):
        raise ContractError(f"publication manifest missing fields: {', '.join(missing)}")
    base = json_safe(manifest)
    if not isinstance(base, dict):
        raise ContractError("publication manifest must be an object")
    publication_id = sha256_json(base)
    body = {**base, "publication_id": publication_id}
    body["manifest_hash"] = sha256_json(body)
    path = root / "publications" / f"{publication_id}.json"
    write_immutable_json(path, body)
    current = read_latest_manifest(root)
    if current is not None:
        current_as_of = str(current.get("as_of_trade_date") or "")
        candidate_as_of = str(body.get("as_of_trade_date") or "")
        if current_as_of > candidate_as_of:
            raise ArtifactMismatch("latest publication cannot move to an older as-of date")
        same_run_binding = all(
            current.get(key) == body.get(key)
            for key in ("run_id", "as_of_trade_date", "data_hash", "config_hash", "code_hash")
        )
        if same_run_binding and _publication_dominates(current, body):
            return current
    pointer = {
        "publication_id": publication_id,
        "manifest_hash": body["manifest_hash"],
        "run_id": body["run_id"],
        "as_of_trade_date": body["as_of_trade_date"],
        "data_hash": body["data_hash"],
        "path": str(path.relative_to(root)),
    }
    _atomic_write_text(
        root / "latest.json",
        json.dumps(pointer, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
    )
    return body


def _publication_dominates(current: dict[str, Any], candidate: dict[str, Any]) -> bool:
    status_rank = {
        "PENDING": 0,
        "NOT_EVALUATED": 0,
        "UNAVAILABLE": 1,
        "UNAVAILABLE_CREDENTIALS": 1,
        "PARTIAL": 2,
        "OK": 3,
    }
    current_routes = current.get("routes") if isinstance(current.get("routes"), dict) else {}
    candidate_routes = candidate.get("routes") if isinstance(candidate.get("routes"), dict) else {}
    for method in set(current_routes) | set(candidate_routes):
        current_route = current_routes.get(method) if isinstance(current_routes.get(method), dict) else {}
        candidate_route = candidate_routes.get(method) if isinstance(candidate_routes.get(method), dict) else {}
        current_score = (
            status_rank.get(str(current_route.get("status") or ""), 1),
            int(current_route.get("recorded_rows", 0) or 0),
        )
        candidate_score = (
            status_rank.get(str(candidate_route.get("status") or ""), 1),
            int(candidate_route.get("recorded_rows", 0) or 0),
        )
        if current_score < candidate_score:
            return False
    return True


def read_latest_manifest(artifact_root: str | Path) -> dict[str, Any] | None:
    root = Path(artifact_root)
    pointer = _read_json(root / "latest.json")
    if pointer is None:
        return None
    publication_id = _safe_component(pointer.get("publication_id"), "publication_id")
    path = root / "publications" / f"{publication_id}.json"
    manifest = _read_json(path)
    if manifest is None:
        raise ArtifactMismatch(f"latest publication is missing or invalid: {path}")
    recorded_hash = str(manifest.get("manifest_hash") or "")
    unhashed = dict(manifest)
    unhashed.pop("manifest_hash", None)
    if not recorded_hash or sha256_json(unhashed) != recorded_hash:
        raise ArtifactMismatch(f"publication manifest hash mismatch: {path}")
    content = dict(unhashed)
    content.pop("publication_id", None)
    if sha256_json(content) != publication_id:
        raise ArtifactMismatch(f"publication ID is not content-addressed: {path}")
    if (
        pointer.get("manifest_hash") != recorded_hash
        or manifest.get("publication_id") != publication_id
        or pointer.get("run_id") != manifest.get("run_id")
        or pointer.get("as_of_trade_date") != manifest.get("as_of_trade_date")
        or pointer.get("data_hash") != manifest.get("data_hash")
    ):
        raise ArtifactMismatch("latest pointer does not match publication manifest")
    return manifest


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
    except (OSError, json.JSONDecodeError):
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
        with suppress(OSError):
            path.unlink(missing_ok=True)
        return None
    with suppress(OSError):
        path.unlink(missing_ok=True)
    data["payload"] = _deserialize_obj(data.get("payload") or {})
    return data


def has_pending_request(task_name: str) -> bool:
    return _request_path(task_name).exists()


def clear_request(task_name: str) -> None:
    with suppress(OSError):
        _request_path(task_name).unlink(missing_ok=True)


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
    with suppress(OSError):
        _task_status_path(task_name).unlink(missing_ok=True)


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
    progress_pct: float,
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
