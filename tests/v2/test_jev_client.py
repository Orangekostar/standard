from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from core.models.jev_client import JevBudget, JevClient, JevPricing
from core.strategies.jev_v2 import build_questions


@dataclass
class FakeResponse:
    status_code: int
    body: dict
    headers: dict[str, str] | None = None

    def json(self) -> dict:
        return self.body


class CountingTransport:
    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


class SlowTransport:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, **_kwargs):
        with self._lock:
            self.calls += 1
        time.sleep(0.05)
        return self.response


def _valid_body(model: str = "jev-1.13.0") -> dict:
    answers = {}
    for question_id in build_questions("stock"):
        answers[question_id] = {
            "type": "choice",
            "choice": "flat",
            "probabilities": {"up": 0.2, "flat": 0.6, "down": 0.2},
            "confidence": 0.5,
        }
    return {"model": model, "answers": answers, "usage": {"input_tokens": 100, "output_tokens": 20}}


def _pricing() -> JevPricing:
    return JevPricing(
        model="jev-1.13.0",
        input_usd_per_million_tokens=Decimal("0.042"),
        output_usd_per_million_tokens=Decimal(0),
        max_total_tokens=64_000,
        max_state_plus_question_tokens=32_000,
        verified_on="2026-09-23",
        source="https://docs.typesafe.ai/models",
    )


def _budget(**overrides) -> JevBudget:
    values = {
        "pricing": _pricing(),
        "development_limit_usd": Decimal(15),
        "daily_limit_usd": Decimal(5),
    }
    values.update(overrides)
    return JevBudget(**values)


class JevClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = {
            "entity_type": "stock",
            "entity_id": "anonymous_123",
            "technical_groups": {},
            "targets": {"h5": {"delta": 0.01}},
            "missing_fields": [],
        }
        self.questions = build_questions("stock")
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp.name) / "jev.sqlite"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _client(self, transport: CountingTransport, **kwargs) -> JevClient:
        return JevClient(
            api_key="test-secret",
            transport=transport,
            cache_path=self.cache_path,
            sleeper=lambda _seconds: None,
            **kwargs,
        )

    def test_missing_key_returns_unavailable_without_transport_call(self) -> None:
        transport = CountingTransport()

        result = JevClient(
            api_key="",
            transport=transport,
            cache_path=self.cache_path,
        ).predict(self.state, self.questions, _budget())

        self.assertEqual(result.status, "UNAVAILABLE_CREDENTIALS")
        self.assertEqual(len(transport.calls), 0)

    def test_unverified_price_stops_before_transport(self) -> None:
        transport = CountingTransport()
        budget = JevBudget(pricing=None)

        result = self._client(transport).predict(self.state, self.questions, budget)

        self.assertEqual(result.status, "PRICING_UNVERIFIED")
        self.assertEqual(len(transport.calls), 0)

    def test_rejects_non_anonymous_state_and_non_native_question_set(self) -> None:
        transport = CountingTransport()
        raw_state = dict(self.state, entity_id="600000.SH", code="600000.SH")
        incomplete_questions = dict(list(self.questions.items())[:-1])

        raw_result = self._client(transport).predict(raw_state, self.questions, _budget())
        question_result = self._client(transport).predict(
            self.state, incomplete_questions, _budget()
        )

        self.assertEqual(raw_result.status, "INVALID_REQUEST")
        self.assertEqual(question_result.status, "INVALID_REQUEST")
        self.assertEqual(len(transport.calls), 0)

    def test_posts_native_request_and_reuses_first_cached_response(self) -> None:
        transport = CountingTransport([FakeResponse(200, _valid_body())])
        client = self._client(transport)
        budget = _budget()

        first = client.predict(self.state, self.questions, budget)
        second = client.predict(self.state, self.questions, budget)

        self.assertEqual(first.status, "OK")
        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertEqual(len(transport.calls), 1)
        request = transport.calls[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["url"], "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(request["timeout"], 30.0)
        self.assertEqual(request["headers"]["Authorization"], "Bearer test-secret")
        self.assertEqual(request["json"]["model"], "jev-1.13.0")
        self.assertEqual(len(request["json"]["questions"]), 15)
        self.assertEqual(first.cache_key, second.cache_key)
        self.assertEqual(budget.reserved_usd, Decimal(0))
        self.assertEqual(budget.development_spent_usd, Decimal("0.0000042"))
        self.assertEqual(second.cost_actual_usd, Decimal(0))

    def test_cache_key_records_both_requested_and_returned_model(self) -> None:
        transport = CountingTransport([FakeResponse(200, _valid_body("jev-1.13.0"))])
        result = self._client(transport).predict(self.state, self.questions, _budget())

        import sqlite3

        with closing(sqlite3.connect(self.cache_path)) as conn:
            row = conn.execute(
                "SELECT model_requested, model_returned, state_hash, instructions_hash, schema_version, response_json "
                "FROM jev_response_cache"
            ).fetchone()
        self.assertEqual(row[:2], ("jev-1.13.0", "jev-1.13.0"))
        self.assertEqual(row[4], "standard.jev-questions.v1")
        self.assertEqual(json.loads(row[5]), result.response)
        self.assertEqual(len(row[2]), 64)
        self.assertEqual(len(row[3]), 64)

    def test_concurrent_identical_requests_are_charged_once(self) -> None:
        transport = SlowTransport(FakeResponse(200, _valid_body()))
        client = JevClient(
            api_key="test-secret",
            transport=transport,
            cache_path=self.cache_path,
            sleeper=lambda _seconds: None,
        )
        budget = _budget()
        start = threading.Barrier(3)

        def invoke():
            start.wait()
            return client.predict(self.state, self.questions, budget)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(invoke) for _ in range(2)]
            start.wait()
            results = [future.result() for future in futures]

        self.assertEqual(transport.calls, 1)
        self.assertEqual(sorted(result.from_cache for result in results), [False, True])

    def test_retries_429_and_5xx_at_most_twice(self) -> None:
        transport = CountingTransport(
            [
                FakeResponse(429, {"detail": "rate"}, {"Retry-After": "1"}),
                FakeResponse(529, {"detail": "overloaded"}),
                FakeResponse(200, _valid_body()),
            ]
        )
        sleeps: list[float] = []
        client = JevClient(
            api_key="test-secret",
            transport=transport,
            cache_path=self.cache_path,
            sleeper=sleeps.append,
        )

        result = client.predict(self.state, self.questions, _budget())

        self.assertEqual(result.status, "OK")
        self.assertEqual(result.attempts, 3)
        self.assertEqual(sleeps, [1.0, 5.0])

    def test_terminal_status_is_not_retried(self) -> None:
        for status_code in (401, 403, 422):
            with self.subTest(status_code=status_code):
                transport = CountingTransport([FakeResponse(status_code, {"detail": "terminal"})])
                with tempfile.TemporaryDirectory() as tmp_dir:
                    client = JevClient(
                        api_key="test-secret",
                        transport=transport,
                        cache_path=Path(tmp_dir) / "jev.sqlite",
                        sleeper=lambda _seconds: None,
                    )
                    result = client.predict(self.state, self.questions, _budget())
                self.assertEqual(result.status, f"HTTP_{status_code}")
                self.assertEqual(result.attempts, 1)
                self.assertEqual(len(transport.calls), 1)

    def test_budget_exhaustion_keeps_not_evaluated_status_row(self) -> None:
        transport = CountingTransport()
        budget = _budget(
            development_limit_usd=Decimal("0.000000001"),
            daily_limit_usd=Decimal("0.000000001"),
        )

        result = self._client(transport).predict(self.state, self.questions, budget)

        self.assertEqual(result.status, "NOT_EVALUATED_BUDGET")
        self.assertEqual(len(transport.calls), 0)

    def test_conservative_utf8_size_rejects_context_before_transport(self) -> None:
        transport = CountingTransport()
        state = dict(self.state)
        state["technical_groups"] = {"T": {"F01": "x" * 70_000}}

        result = self._client(transport).predict(state, self.questions, _budget())

        self.assertEqual(result.status, "INPUT_TOO_LARGE")
        self.assertEqual(len(transport.calls), 0)


if __name__ == "__main__":
    unittest.main()
