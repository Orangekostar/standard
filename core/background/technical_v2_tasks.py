from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from core.technical_v2.contracts import ContractError, json_safe, sha256_json

TECHNICAL_V2_TASKS = (
    "data_v2_sync",
    "features_v2",
    "formula_v2",
    "jev_v2",
    "paper_v2",
    "evaluate_matured_v2",
    "publish_technical_v2",
)

TECHNICAL_V2_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "data_v2_sync": (),
    "features_v2": ("data_v2_sync",),
    "formula_v2": ("features_v2",),
    "jev_v2": ("features_v2",),
    "paper_v2": ("formula_v2",),
    "evaluate_matured_v2": ("data_v2_sync",),
    "publish_technical_v2": ("formula_v2", "paper_v2", "evaluate_matured_v2"),
}

TECHNICAL_V2_OPTIONAL_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "paper_v2": ("jev_v2",),
    "publish_technical_v2": ("jev_v2",),
}


@dataclass(frozen=True)
class TechnicalTaskDefinition:
    name: str
    priority: int
    dependencies: tuple[str, ...]
    interval_seconds: int = 60
    timeout_seconds: int = 0


def technical_v2_task_definitions() -> tuple[TechnicalTaskDefinition, ...]:
    return tuple(
        TechnicalTaskDefinition(
            name=task_name,
            priority=(index + 1) * 10,
            dependencies=TECHNICAL_V2_DEPENDENCIES[task_name],
        )
        for index, task_name in enumerate(TECHNICAL_V2_TASKS)
    )


def artifact_binding(payload: Mapping[str, Any]) -> dict[str, str]:
    binding = {
        "run_id": str(payload.get("run_id") or "").strip(),
        "as_of_trade_date": str(payload.get("as_of_trade_date") or "").replace("-", "")[:8],
        "data_hash": str(payload.get("data_hash") or "").strip(),
    }
    if not all(binding.values()):
        raise ContractError("technical V2 task requires run_id, as_of_trade_date, and data_hash")
    return binding


class TechnicalV2TaskRunner:
    def __init__(
        self,
        handlers: Mapping[str, Callable[[dict[str, Any], bool], Mapping[str, Any]]] | None = None,
    ) -> None:
        if handlers is None:
            self.handlers = {
                task_name: self._default_handler(task_name)
                for task_name in TECHNICAL_V2_TASKS
            }
        else:
            self.handlers = dict(handlers)

    @staticmethod
    def _default_handler(
        task_name: str,
    ) -> Callable[[dict[str, Any], bool], Mapping[str, Any]]:
        def handle(params: dict[str, Any], force_refresh: bool) -> Mapping[str, Any]:
            from scripts.v2 import run_worker_task

            return run_worker_task(task_name, params, force_refresh)

        return handle

    def dispatch(self, task_name: str, params: dict[str, Any], force_refresh: bool) -> dict[str, Any]:
        if task_name not in TECHNICAL_V2_DEPENDENCIES:
            raise ContractError(f"unknown Technical V2 task: {task_name}")
        handler = self.handlers.get(task_name)
        if handler is None:
            result: dict[str, Any] = {
                "status": "UNAVAILABLE_INPUT",
                "code": "TASK_HANDLER_NOT_CONFIGURED",
                "message": f"{task_name} requires an explicit runtime handler",
                "completed_entities": 0,
                "total_entities": int(params.get("total_entities", 0) or 0),
            }
        else:
            result = dict(handler(dict(params), bool(force_refresh)))
            result.setdefault("status", "ERROR")
            result.setdefault("code", "")
            result.setdefault("message", "")
        if task_name == "data_v2_sync":
            try:
                binding = artifact_binding(result)
            except ContractError:
                if result["status"] in {"OK", "PARTIAL"}:
                    raise
                binding = {"run_id": "", "as_of_trade_date": "", "data_hash": ""}
        else:
            binding = artifact_binding(params)
            returned_binding = {
                key: str(result.pop(key, binding[key]) or "").replace("-", "")
                if key == "as_of_trade_date"
                else str(result.pop(key, binding[key]) or "")
                for key in binding
            }
            if returned_binding != binding:
                raise ContractError("technical V2 task returned a conflicting artifact binding")
        for key in binding:
            result.pop(key, None)
        completed = int(result.get("completed_entities", 0) or 0)
        total = int(result.get("total_entities", 0) or 0)
        if completed < 0 or total < 0 or completed > total:
            raise ContractError("technical V2 task entity progress is invalid")
        progress = min(100, completed * 100 // total) if total else 100 if result["status"] == "OK" else 0
        payload = {
            **binding,
            "task_name": task_name,
            "status": str(result.pop("status")),
            "params": json_safe(params),
            "dependency_artifacts": json_safe(params.get("dependency_artifacts", {})),
            **json_safe(result),
            "progress_pct": progress,
        }
        payload["artifact_id"] = sha256_json(payload)
        return payload
