from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from core.technical_v2.contracts import ContractError, canonical_json, sha256_json

DEFAULT_API_BASE = "https://api.typesafe.ai"
DEFAULT_ENDPOINT = "/v1/systemone"
DEFAULT_MODEL = "jev-1.13.0"
DEFAULT_SCHEMA_VERSION = "standard.jev-questions.v1"
DEFAULT_CACHE_PATH = Path("cache/v2/jev_responses.db")
DEFAULT_PRICING_PATH = Path(__file__).resolve().parents[2] / "configs" / "jev_pricing_v1.json"


@dataclass(frozen=True)
class JevPricing:
    model: str
    input_usd_per_million_tokens: Decimal
    output_usd_per_million_tokens: Decimal
    max_total_tokens: int
    max_state_plus_question_tokens: int
    verified_on: str
    source: str

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PRICING_PATH) -> JevPricing:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            if payload.get("schema_version") != "standard.jev-pricing-cache.v1":
                raise ContractError("unsupported Jev pricing cache version")
            pricing = cls(
                model=str(payload["model"]),
                input_usd_per_million_tokens=Decimal(str(payload["input_usd_per_million_tokens"])),
                output_usd_per_million_tokens=Decimal(str(payload["output_usd_per_million_tokens"])),
                max_total_tokens=int(payload["max_total_tokens"]),
                max_state_plus_question_tokens=int(payload["max_state_plus_question_tokens"]),
                verified_on=str(payload["verified_on"]),
                source=str(payload["source"]),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError(f"cannot load verified Jev pricing: {exc}") from exc
        try:
            verified_date = date.fromisoformat(pricing.verified_on)
        except ValueError as exc:
            raise ContractError("invalid Jev pricing verification date") from exc
        if (
            not pricing.input_usd_per_million_tokens.is_finite()
            or not pricing.output_usd_per_million_tokens.is_finite()
            or pricing.input_usd_per_million_tokens < 0
            or pricing.output_usd_per_million_tokens < 0
            or pricing.max_total_tokens <= 0
            or pricing.max_state_plus_question_tokens <= 0
            or verified_date > datetime.now(UTC).date()
            or not pricing.source.startswith("https://")
        ):
            raise ContractError("invalid verified Jev pricing snapshot")
        return pricing

    def estimate_upper_bound(self, input_utf8_bytes: int, attempts: int = 1) -> Decimal:
        input_tokens_upper = max(0, int(input_utf8_bytes))
        return (
            Decimal(input_tokens_upper)
            * self.input_usd_per_million_tokens
            * Decimal(max(1, int(attempts)))
            / Decimal(1_000_000)
        )

    def actual_cost(self, usage: Mapping[str, Any]) -> Decimal:
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        if input_tokens < 0 or output_tokens < 0:
            raise ContractError("Jev usage cannot contain negative token counts")
        return (
            Decimal(input_tokens) * self.input_usd_per_million_tokens
            + Decimal(output_tokens) * self.output_usd_per_million_tokens
        ) / Decimal(1_000_000)


class JevBudget:
    def __init__(
        self,
        *,
        pricing: JevPricing | None,
        development_limit_usd: Decimal | str | float = Decimal(15),
        daily_limit_usd: Decimal | str | float = Decimal(5),
        development_spent_usd: Decimal | str | float = Decimal(0),
        daily_spent_usd: Decimal | str | float = Decimal(0),
    ) -> None:
        self.pricing = pricing
        self.development_limit_usd = Decimal(str(development_limit_usd))
        self.daily_limit_usd = Decimal(str(daily_limit_usd))
        self.development_spent_usd = Decimal(str(development_spent_usd))
        self.daily_spent_usd = Decimal(str(daily_spent_usd))
        self._reserved_usd = Decimal(0)
        self._lock = threading.Lock()
        if (
            self.development_limit_usd < 0
            or self.daily_limit_usd < 0
            or self.development_spent_usd < 0
            or self.daily_spent_usd < 0
        ):
            raise ContractError("Jev budget values cannot be negative")

    @classmethod
    def from_verified_snapshot(
        cls,
        path: str | Path = DEFAULT_PRICING_PATH,
        **kwargs: Any,
    ) -> JevBudget:
        return cls(pricing=JevPricing.load(path), **kwargs)

    @property
    def reserved_usd(self) -> Decimal:
        with self._lock:
            return self._reserved_usd

    def reserve(self, amount: Decimal) -> bool:
        if amount < 0:
            raise ContractError("budget reservation cannot be negative")
        with self._lock:
            projected_development = self.development_spent_usd + self._reserved_usd + amount
            projected_daily = self.daily_spent_usd + self._reserved_usd + amount
            if (
                projected_development > self.development_limit_usd
                or projected_daily > self.daily_limit_usd
            ):
                return False
            self._reserved_usd += amount
            return True

    def settle(self, reserved: Decimal, actual: Decimal) -> None:
        if reserved < 0 or actual < 0:
            raise ContractError("budget settlement cannot be negative")
        with self._lock:
            if reserved > self._reserved_usd:
                raise ContractError("budget settlement exceeds reserved amount")
            self._reserved_usd -= reserved
            self.development_spent_usd += actual
            self.daily_spent_usd += actual


@dataclass(frozen=True)
class JevClientResult:
    status: str
    response: dict[str, Any] | None
    model_requested: str
    model_returned: str | None
    attempts: int
    from_cache: bool
    cache_key: str | None
    usage: dict[str, int]
    cost_reserved_usd: Decimal
    cost_actual_usd: Decimal
    error: str | None = None


class RequestRateLimiter:
    def __init__(
        self,
        requests_per_minute: int = 240,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        self.requests_per_minute = int(requests_per_minute)
        self.clock = clock
        self.sleeper = sleeper
        self._request_times: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self.clock()
                while self._request_times and now - self._request_times[0] >= 60.0:
                    self._request_times.popleft()
                if len(self._request_times) < self.requests_per_minute:
                    self._request_times.append(now)
                    return
                delay = max(0.0, 60.0 - (now - self._request_times[0]))
            self.sleeper(delay)


class JevClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_base: str = DEFAULT_API_BASE,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = DEFAULT_MODEL,
        schema_version: str = DEFAULT_SCHEMA_VERSION,
        timeout_seconds: float = 30.0,
        extra_retries: int = 2,
        requests_per_minute: int = 240,
        cache_path: str | Path = DEFAULT_CACHE_PATH,
        transport: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        rate_limiter: RequestRateLimiter | None = None,
        default_budget: JevBudget | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if extra_retries < 0:
            raise ValueError("extra_retries cannot be negative")
        self.api_key = os.getenv("TYPESAFE_API_KEY", "") if api_key is None else str(api_key)
        self.api_base = api_base.rstrip("/")
        self.endpoint = "/" + endpoint.lstrip("/")
        self.model = str(model)
        self.schema_version = str(schema_version)
        self.timeout_seconds = float(timeout_seconds)
        self.extra_retries = int(extra_retries)
        self.cache_path = Path(cache_path)
        self.transport = transport
        self.sleeper = sleeper
        self.rate_limiter = rate_limiter or RequestRateLimiter(
            requests_per_minute=requests_per_minute,
            sleeper=sleeper,
        )
        if default_budget is None:
            try:
                default_budget = JevBudget.from_verified_snapshot()
            except ContractError:
                default_budget = JevBudget(pricing=None)
        self.default_budget = default_budget
        self._request_locks = tuple(threading.Lock() for _ in range(64))
        self._initialize_cache()

    def _connect(self) -> sqlite3.Connection:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.cache_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_cache(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jev_response_cache (
                    cache_key TEXT PRIMARY KEY,
                    model_requested TEXT NOT NULL,
                    model_returned TEXT NOT NULL,
                    state_hash TEXT NOT NULL,
                    instructions_hash TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    first_received_at TEXT NOT NULL,
                    UNIQUE (
                        model_requested, model_returned, state_hash,
                        instructions_hash, schema_version
                    )
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_jev_cache_request
                ON jev_response_cache (
                    model_requested, state_hash, instructions_hash, schema_version,
                    first_received_at
                )
                """
            )

    def _cached_response(self, state_hash: str, instructions_hash: str) -> tuple[str, dict[str, Any]] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT cache_key, response_json
                FROM jev_response_cache
                WHERE model_requested = ? AND state_hash = ?
                  AND instructions_hash = ? AND schema_version = ?
                ORDER BY first_received_at ASC, cache_key ASC
                LIMIT 1
                """,
                (self.model, state_hash, instructions_hash, self.schema_version),
            ).fetchone()
        if row is None:
            return None
        try:
            response = json.loads(row["response_json"])
        except json.JSONDecodeError as exc:
            raise ContractError("cached Jev response is not valid JSON") from exc
        return str(row["cache_key"]), response

    def _cache_response(
        self,
        *,
        state_hash: str,
        instructions_hash: str,
        response: dict[str, Any],
    ) -> str | None:
        model_returned = response.get("model")
        if not isinstance(model_returned, str) or not model_returned:
            return None
        cache_fields = {
            "model_requested": self.model,
            "model_returned": model_returned,
            "state_hash": state_hash,
            "instructions_hash": instructions_hash,
            "schema_version": self.schema_version,
        }
        cache_key = sha256_json(cache_fields)
        response_json = canonical_json(response)
        received_at = datetime.now(UTC).isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO jev_response_cache (
                    cache_key, model_requested, model_returned, state_hash,
                    instructions_hash, schema_version, response_json, first_received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    self.model,
                    model_returned,
                    state_hash,
                    instructions_hash,
                    self.schema_version,
                    response_json,
                    received_at,
                ),
            )
        return cache_key

    def _send(self, payload: dict[str, Any]) -> Any:
        kwargs = {
            "method": "POST",
            "url": f"{self.api_base}{self.endpoint}",
            "headers": {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            "json": payload,
            "timeout": self.timeout_seconds,
        }
        if self.transport is not None:
            return self.transport(**kwargs)
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("httpx==0.28.1 is required for Jev HTTP requests") from exc
        return httpx.request(**kwargs)

    @staticmethod
    def _response_json(response: Any) -> dict[str, Any]:
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("Jev response body must be a JSON object")
        canonical_json(payload)
        return payload

    @staticmethod
    def _retry_after(response: Any, retry_index: int) -> float:
        headers = getattr(response, "headers", None) or {}
        value = None
        for key, item in headers.items():
            if str(key).lower() == "retry-after":
                value = item
                break
        if value is not None:
            try:
                delay = float(value)
                if delay >= 0:
                    return delay
            except (TypeError, ValueError):
                pass
        return (2.0, 5.0)[min(retry_index, 1)]

    @staticmethod
    def _usage(response: Mapping[str, Any]) -> dict[str, int]:
        raw = response.get("usage")
        if not isinstance(raw, Mapping):
            return {}
        result: dict[str, int] = {}
        for key in ("input_tokens", "output_tokens"):
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[key] = value
        return result if len(result) == 2 else {}

    @staticmethod
    def _validate_request_contract(state: Mapping[str, Any], questions: Mapping[str, Any]) -> None:
        if not isinstance(state, Mapping) or not isinstance(questions, Mapping):
            raise ContractError("Jev state and questions must be mappings")
        if len(questions) != 15:
            raise ContractError("a Jev entity request must contain exactly 15 questions")
        for question_id, question in questions.items():
            if not isinstance(question_id, str) or not isinstance(question, Mapping):
                raise ContractError("Jev questions must be keyed objects")
            if question.get("type") != "choice":
                raise ContractError("all Jev questions must use the native choice type")
            if not isinstance(question.get("instructions"), (str, dict, list)):
                raise ContractError("every Jev question requires instructions")
            criteria = question.get("criteria")
            if not isinstance(criteria, Mapping) or set(criteria) != {"up", "flat", "down"}:
                raise ContractError("choice criteria must be exactly up/flat/down")
        entity_id = state.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id.startswith("anonymous_"):
            raise ContractError("Jev state requires a locally anonymized entity_id")
        forbidden_keys = {
            "account",
            "action",
            "as_of",
            "as_of_trade_date",
            "cash",
            "code",
            "date",
            "formula_score",
            "future_return",
            "holding",
            "holdings",
            "label",
            "name",
            "position",
            "rank",
            "symbol",
            "trade_date",
            "ts_code",
        }

        def visit(value: Any) -> None:
            if isinstance(value, Mapping):
                for key, item in value.items():
                    if str(key).lower() in forbidden_keys:
                        raise ContractError(f"forbidden Jev state field: {key}")
                    visit(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    visit(item)

        visit(state)

    def _result(
        self,
        status: str,
        *,
        response: dict[str, Any] | None = None,
        attempts: int = 0,
        from_cache: bool = False,
        cache_key: str | None = None,
        reserved: Decimal = Decimal(0),
        actual: Decimal = Decimal(0),
        error: str | None = None,
    ) -> JevClientResult:
        model_returned = response.get("model") if isinstance(response, Mapping) else None
        if not isinstance(model_returned, str):
            model_returned = None
        return JevClientResult(
            status=status,
            response=response,
            model_requested=self.model,
            model_returned=model_returned,
            attempts=attempts,
            from_cache=from_cache,
            cache_key=cache_key,
            usage=self._usage(response or {}),
            cost_reserved_usd=reserved,
            cost_actual_usd=actual,
            error=error,
        )

    def predict(
        self,
        state: Mapping[str, Any],
        questions: Mapping[str, Any],
        budget: JevBudget | None = None,
    ) -> JevClientResult:
        try:
            self._validate_request_contract(state, questions)
            state_hash = sha256_json(state)
            instructions_hash = sha256_json(questions)
            payload = {
                "state": state,
                "model": self.model,
                "questions": questions,
            }
            payload_json = canonical_json(payload)
        except ContractError as exc:
            return self._result("INVALID_REQUEST", error=str(exc))

        request_hash = sha256_json(
            {
                "model_requested": self.model,
                "state_hash": state_hash,
                "instructions_hash": instructions_hash,
                "schema_version": self.schema_version,
            }
        )
        request_lock = self._request_locks[int(request_hash[:8], 16) % len(self._request_locks)]
        with request_lock:
            return self._predict_locked(
                state=state,
                questions=questions,
                budget=budget,
                state_hash=state_hash,
                instructions_hash=instructions_hash,
                payload=payload,
                payload_json=payload_json,
            )

    def _predict_locked(
        self,
        *,
        state: Mapping[str, Any],
        questions: Mapping[str, Any],
        budget: JevBudget | None,
        state_hash: str,
        instructions_hash: str,
        payload: dict[str, Any],
        payload_json: str,
    ) -> JevClientResult:

        cached = self._cached_response(state_hash, instructions_hash)
        if cached is not None:
            cache_key, response = cached
            return self._result(
                "OK",
                response=response,
                from_cache=True,
                cache_key=cache_key,
            )
        if not self.api_key:
            return self._result("UNAVAILABLE_CREDENTIALS")

        active_budget = budget or self.default_budget
        pricing = active_budget.pricing
        if pricing is None or pricing.model != self.model:
            return self._result("PRICING_UNVERIFIED")

        payload_bytes = len(payload_json.encode("utf-8"))
        state_bytes = len(canonical_json(state).encode("utf-8"))
        longest_question_bytes = max(
            (len(canonical_json(question).encode("utf-8")) for question in questions.values()),
            default=0,
        )
        if (
            payload_bytes > pricing.max_total_tokens
            or state_bytes + longest_question_bytes > pricing.max_state_plus_question_tokens
        ):
            return self._result("INPUT_TOO_LARGE")

        max_attempts = self.extra_retries + 1
        reserved = pricing.estimate_upper_bound(payload_bytes, attempts=max_attempts)
        if not active_budget.reserve(reserved):
            return self._result("NOT_EVALUATED_BUDGET", reserved=reserved)

        attempts = 0
        response_payload: dict[str, Any] | None = None
        final_status = "TRANSPORT_ERROR"
        final_error: str | None = None
        cache_key: str | None = None
        for attempt_index in range(max_attempts):
            attempts += 1
            self.rate_limiter.acquire()
            try:
                response = self._send(payload)
            except Exception as exc:  # noqa: BLE001 - injected transports have no shared exception base
                final_status = "TRANSPORT_ERROR"
                final_error = f"{type(exc).__name__}: {exc}"
                if attempt_index < self.extra_retries:
                    self.sleeper((2.0, 5.0)[min(attempt_index, 1)])
                    continue
                break
            status_code = int(getattr(response, "status_code", 0) or 0)
            try:
                body = self._response_json(response)
            except (TypeError, ValueError, ContractError, json.JSONDecodeError) as exc:
                final_status = "INVALID_RESPONSE"
                final_error = str(exc)
                break
            if 200 <= status_code < 300:
                response_payload = body
                if not isinstance(body.get("model"), str) or not body.get("model"):
                    final_status = "INVALID_RESPONSE"
                    final_error = "Jev response model is missing"
                    break
                cache_key = self._cache_response(
                    state_hash=state_hash,
                    instructions_hash=instructions_hash,
                    response=body,
                )
                final_status = "OK"
                break
            final_status = f"HTTP_{status_code}" if status_code else "TRANSPORT_ERROR"
            final_error = canonical_json(body)
            retryable = status_code == 429 or status_code >= 500
            if retryable and attempt_index < self.extra_retries:
                self.sleeper(self._retry_after(response, attempt_index))
                continue
            break

        usage = self._usage(response_payload or {})
        actual = pricing.actual_cost(usage) if usage else Decimal(0)
        active_budget.settle(reserved, actual)
        return self._result(
            final_status,
            response=response_payload,
            attempts=attempts,
            cache_key=cache_key,
            reserved=reserved,
            actual=actual,
            error=final_error,
        )
