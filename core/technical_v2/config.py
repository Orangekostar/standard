from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from core.technical_v2.contracts import ContractError, json_safe


_GROUPS = ("T", "R", "S", "V", "C")
_HORIZONS = (1, 3, 5)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return ConfigSection(value)
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, ConfigSection):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


class ConfigSection(Mapping[str, Any]):
    """Immutable attribute-access view over validated configuration data."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        self._values = {str(key): _freeze(value) for key, value in values.items()}

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def to_dict(self) -> dict[str, Any]:
        return _thaw(self)


class TechnicalV2Config(ConfigSection):
    @classmethod
    def load(cls, path: str | Path) -> "TechnicalV2Config":
        config_path = Path(path)
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContractError(f"cannot load Technical V2 config {config_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ContractError("Technical V2 config root must be an object")
        cls._validate(raw)
        return cls(raw)

    @staticmethod
    def _validate(raw: dict[str, Any]) -> None:
        if raw.get("schema_version") != "standard.technical-v2.plan.v1":
            raise ContractError("unexpected Technical V2 schema_version")

        data = _required_mapping(raw, "data")
        if data.get("default_mode") != "real" or bool(data.get("allow_silent_mock")):
            raise ContractError("real must be the default mode and silent mock must be disabled")

        targets = _required_mapping(raw, "targets")
        horizons = tuple(int(value) for value in targets.get("horizons", ()))
        if horizons != _HORIZONS or int(targets.get("primary_horizon", -1)) != 5:
            raise ContractError("targets must use horizons 1, 3, 5 with primary horizon 5")

        if int(data.get("factor_count", -1)) != 15 or int(data.get("risk_count", -1)) != 6:
            raise ContractError("factors must declare 15 direction factors and 6 risk metrics")
        factors = _required_mapping(raw, "factors")
        groups = _required_mapping(factors, "groups")
        factor_ids = [factor_id for group in _GROUPS for factor_id in groups.get(group, [])]
        expected_ids = [f"F{index:02d}" for index in range(1, 16)]
        if sorted(factor_ids) != expected_ids or len(factor_ids) != len(set(factor_ids)):
            raise ContractError("factor groups must cover F01-F15 exactly once")

        base_weights = _required_mapping(factors, "base_weights")
        variants = _required_mapping(factors, "formula_variants")
        for horizon in _HORIZONS:
            base = _numeric_group_weights(base_weights, str(horizon), f"base_weights horizon {horizon}")
            for variant_id, deltas_raw in variants.items():
                if not isinstance(deltas_raw, dict):
                    raise ContractError(f"formula variant {variant_id} must be an object")
                weights = {group: base[group] + float(deltas_raw.get(group, 0.0)) for group in _GROUPS}
                _validate_weights(weights, f"base_weights horizon {horizon} variant {variant_id}")

        sector_weights = _required_mapping(factors, "sector_weights")
        for horizon in _HORIZONS:
            weights = sector_weights.get(str(horizon))
            if not isinstance(weights, list) or len(weights) != 5:
                raise ContractError(f"sector_weights horizon {horizon} must contain five values")
            _validate_weights({str(index): float(value) for index, value in enumerate(weights)}, f"sector_weights horizon {horizon}")

        jev = _required_mapping(raw, "jev")
        if jev.get("model") != "jev-1.13.0" or jev.get("endpoint") != "/v1/systemone":
            raise ContractError("Jev model and endpoint must use the frozen native API contract")
        if float(jev.get("development_budget_usd", 0)) <= 0 or float(jev.get("daily_budget_usd", 0)) <= 0:
            raise ContractError("Jev budgets must be positive")

        portfolio = _required_mapping(raw, "portfolio")
        if bool(portfolio.get("connect_real_broker")):
            raise ContractError("connect_real_broker must remain false")

        json_safe(raw)


def _required_mapping(parent: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ContractError(f"{key} must be an object")
    return value


def _numeric_group_weights(parent: Mapping[str, Any], key: str, label: str) -> dict[str, float]:
    raw = parent.get(key)
    if not isinstance(raw, dict) or set(raw) != set(_GROUPS):
        raise ContractError(f"{label} must contain T/R/S/V/C")
    weights = {group: float(raw[group]) for group in _GROUPS}
    _validate_weights(weights, label)
    return weights


def _validate_weights(weights: Mapping[str, float], label: str) -> None:
    if any(not math.isfinite(value) or value < 0 for value in weights.values()):
        raise ContractError(f"{label} weights must be finite and non-negative")
    if not math.isclose(sum(weights.values()), 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ContractError(f"{label} weights must sum to 1")
