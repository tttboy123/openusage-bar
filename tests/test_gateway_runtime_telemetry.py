"""RED contracts for aggregate-only Gateway runtime telemetry.

The public seams under test are ``GatewayRuntime.__call__`` and the closeable
iterator returned by ``GatewayRuntime.stream_events``.  Telemetry is a local,
optional side effect: it may observe only the bounded aggregate allowlist and
must never change a response or make a partial stream cacheable.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from openusage_bar.gateway.cache import SQLiteGatewayCache
from openusage_bar.gateway.providers import GatewayProviderError, ProviderResult
from openusage_bar.gateway.runtime import GatewayRuntime
from openusage_bar.gateway.telemetry import GatewayTelemetryStore


_CLIENT_REQUEST_ID = "client-request-private-42"
_PROVIDER_REQUEST_ID = "provider-request-private-84"
_PROMPT = "private prompt jane@example.com"
_OUTPUT = "private response text"

_AGGREGATE_FIELDS = {
    "request_id",
    "provider_id",
    "model_id",
    "started_at",
    "finished_at",
    "status_class",
    "input_tokens",
    "output_tokens",
    "latency_ms",
    "estimated_cost",
    "actual_cost",
    "cache_outcome",
    "fallback_count",
    "error_code",
}


def _payload(*, stream: bool) -> dict[str, object]:
    return {
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "request": {
            "model": "gpt-4.1-mini",
            "input": _PROMPT,
            "request_id": _CLIENT_REQUEST_ID,
            "stream": stream,
        },
    }


def _json_result() -> ProviderResult:
    body = {
        "id": _PROVIDER_REQUEST_ID,
        "output_text": _OUTPUT,
        "usage": {"input_tokens": 3, "output_tokens": 4},
    }
    return ProviderResult(
        200,
        (("Content-Type", "application/json"),),
        (json.dumps(body, separators=(",", ":")).encode("utf-8"),),
    )


def _sse(event: str, payload: object) -> bytes:
    return (
        f"event: {event}\n".encode("ascii")
        + b"data: "
        + json.dumps(payload, separators=(",", ":")).encode("utf-8")
        + b"\n\n"
    )


def _stream_result(text: str = _OUTPUT) -> ProviderResult:
    return ProviderResult(
        200,
        (("Content-Type", "text/event-stream"),),
        (
            _sse(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "delta": text,
                    "provider_request_id": _PROVIDER_REQUEST_ID,
                },
            ),
            _sse(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "id": _PROVIDER_REQUEST_ID,
                        "usage": {"input_tokens": 5, "output_tokens": 2},
                    },
                },
            ),
        ),
    )


def _database_bytes(path: Path) -> bytes:
    return b"".join(
        candidate.read_bytes()
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    )


class GatewayRuntimeTelemetryTests(unittest.TestCase):
    def test_non_stream_success_records_only_bounded_aggregate_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-telemetry.sqlite3"
            telemetry = GatewayTelemetryStore(path)
            try:
                with patch.object(
                    telemetry,
                    "record_request",
                    wraps=telemetry.record_request,
                ) as record:
                    runtime = GatewayRuntime(
                        egress=lambda *_args, **_kwargs: _json_result(),
                        telemetry=telemetry,
                    )

                    response = runtime(_payload(stream=False))

                self.assertEqual(response["object"], "gateway.response")
                self.assertEqual(response["outputText"], _OUTPUT)
                record.assert_called_once()
                aggregate = record.call_args.kwargs
                burn_rate = telemetry.recent_burn_rate(
                    "openai",
                    "gpt-4.1-mini",
                    window_minutes=5,
                    max_rows=100,
                )
                self.assertEqual(set(aggregate), _AGGREGATE_FIELDS)
                self.assertEqual(aggregate["provider_id"], "openai")
                model_scope = aggregate["model_id"]
                self.assertNotIn("gpt-4.1-mini", repr(model_scope))
                self.assertEqual(burn_rate, 1.4)
                self.assertEqual(aggregate["status_class"], "2xx")
                self.assertEqual(aggregate["input_tokens"], 3)
                self.assertEqual(aggregate["output_tokens"], 4)
                self.assertEqual(aggregate["cache_outcome"], "disabled")
                self.assertEqual(aggregate["fallback_count"], 0)
                self.assertIsNone(aggregate["estimated_cost"])
                self.assertIsNone(aggregate["actual_cost"])
                self.assertIsNone(aggregate["error_code"])
                self.assertIs(type(aggregate["latency_ms"]), int)
                self.assertGreaterEqual(aggregate["latency_ms"], 0)
                self.assertIsInstance(aggregate["started_at"], datetime)
                self.assertIsInstance(aggregate["finished_at"], datetime)
                self.assertLessEqual(
                    aggregate["started_at"],
                    aggregate["finished_at"],
                )
                local_request_id = aggregate["request_id"]
                self.assertIs(type(local_request_id), str)
                self.assertTrue(local_request_id)
                self.assertNotEqual(local_request_id, _CLIENT_REQUEST_ID)
                self.assertNotEqual(local_request_id, _PROVIDER_REQUEST_ID)

                serialized_aggregate = repr(aggregate)
                for forbidden in (
                    _CLIENT_REQUEST_ID,
                    _PROVIDER_REQUEST_ID,
                    _PROMPT,
                    _OUTPUT,
                    "jane@example.com",
                    "gpt-4.1-mini",
                ):
                    with self.subTest(forbidden=forbidden):
                        self.assertNotIn(forbidden, serialized_aggregate)
            finally:
                telemetry.close()

            raw = _database_bytes(path)
            for forbidden in (
                _CLIENT_REQUEST_ID,
                _PROVIDER_REQUEST_ID,
                _PROMPT,
                _OUTPUT,
                "jane@example.com",
                "gpt-4.1-mini",
            ):
                with self.subTest(persisted_forbidden=forbidden):
                    self.assertNotIn(forbidden.encode("utf-8"), raw)

    def test_stream_records_only_after_consumer_accepts_valid_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            telemetry = GatewayTelemetryStore(
                Path(directory) / "gateway-telemetry.sqlite3"
            )
            try:
                with patch.object(
                    telemetry,
                    "record_request",
                    wraps=telemetry.record_request,
                ) as record:
                    runtime = GatewayRuntime(
                        egress=lambda *_args, **_kwargs: _stream_result(),
                        telemetry=telemetry,
                    )
                    events = runtime.stream_events(_payload(stream=True))

                    self.assertEqual(
                        next(events)["type"],
                        "gateway.response.started",
                    )
                    self.assertEqual(
                        next(events)["type"],
                        "gateway.output_text.delta",
                    )
                    self.assertEqual(record.call_count, 0)

                    terminal = next(events)
                    self.assertEqual(
                        terminal["type"],
                        "gateway.response.completed",
                    )
                    self.assertEqual(terminal["status"], "complete")
                    self.assertEqual(record.call_count, 0)

                    with self.assertRaises(StopIteration):
                        next(events)

                    record.assert_called_once()
                    aggregate = record.call_args.kwargs
                    self.assertEqual(aggregate["status_class"], "2xx")
                    self.assertEqual(aggregate["input_tokens"], 5)
                    self.assertEqual(aggregate["output_tokens"], 2)
                    self.assertEqual(aggregate["cache_outcome"], "disabled")
            finally:
                telemetry.close()

    def test_non_stream_event_form_records_only_after_terminal_resume(self) -> None:
        """A JSON request represented as events keeps the same delivery gate."""

        with tempfile.TemporaryDirectory() as directory:
            telemetry = GatewayTelemetryStore(
                Path(directory) / "gateway-telemetry.sqlite3"
            )
            try:
                with patch.object(
                    telemetry,
                    "record_request",
                    wraps=telemetry.record_request,
                ) as record:
                    runtime = GatewayRuntime(
                        egress=lambda *_args, **_kwargs: _json_result(),
                        telemetry=telemetry,
                    )
                    events = runtime.stream_events(_payload(stream=False))
                    observed_types: list[str] = []

                    while True:
                        event = next(events)
                        observed_types.append(event["type"])
                        self.assertEqual(record.call_count, 0)
                        if event["type"] == "gateway.response.completed":
                            break

                    with self.assertRaises(StopIteration):
                        next(events)

                    record.assert_called_once()
                    self.assertEqual(
                        observed_types,
                        [
                            "gateway.response.started",
                            "gateway.output_text.delta",
                            "gateway.response.completed",
                        ],
                    )
                    aggregate = record.call_args.kwargs
                    self.assertEqual(aggregate["status_class"], "2xx")
                    self.assertEqual(aggregate["input_tokens"], 3)
                    self.assertEqual(aggregate["output_tokens"], 4)
            finally:
                telemetry.close()

    def test_accepted_runtime_error_records_only_closed_sanitized_fields(self) -> None:
        raw_provider_body = (
            "raw provider timeout body with sk-private and jane@example.com"
        )
        provider_error = GatewayProviderError("upstream_timeout", True)
        provider_error.raw_body = raw_provider_body

        def egress(*_args, **_kwargs) -> ProviderResult:
            raise provider_error

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-telemetry.sqlite3"
            telemetry = GatewayTelemetryStore(path)
            try:
                with patch.object(
                    telemetry,
                    "record_request",
                    wraps=telemetry.record_request,
                ) as record:
                    runtime = GatewayRuntime(
                        egress=egress,
                        telemetry=telemetry,
                    )
                    events = runtime.stream_events(_payload(stream=True))

                    self.assertEqual(
                        next(events)["type"],
                        "gateway.response.started",
                    )
                    terminal = next(events)
                    self.assertEqual(terminal["type"], "gateway.error")
                    self.assertEqual(
                        terminal["error"],
                        {
                            "code": "upstream_timeout",
                            "message": "Provider request failed.",
                            "retryable": True,
                        },
                    )
                    self.assertEqual(record.call_count, 0)

                    with self.assertRaises(StopIteration):
                        next(events)

                    record.assert_called_once()
                    aggregate = record.call_args.kwargs
                    self.assertEqual(set(aggregate), _AGGREGATE_FIELDS)
                    self.assertEqual(aggregate["status_class"], "5xx")
                    self.assertEqual(
                        aggregate["error_code"],
                        "upstream_timeout",
                    )
                    self.assertIsNone(aggregate["input_tokens"])
                    self.assertIsNone(aggregate["output_tokens"])
                    self.assertIsNone(aggregate["estimated_cost"])
                    self.assertIsNone(aggregate["actual_cost"])
                    self.assertEqual(aggregate["fallback_count"], 0)
                    self.assertNotIn(raw_provider_body, repr(terminal))
                    self.assertNotIn(raw_provider_body, repr(aggregate))
            finally:
                telemetry.close()

            self.assertNotIn(
                raw_provider_body.encode("utf-8"),
                _database_bytes(path),
            )

    def test_stream_abort_records_nothing_and_does_not_prime_cache(self) -> None:
        provider_calls = 0

        def egress(*_args, **_kwargs) -> ProviderResult:
            nonlocal provider_calls
            provider_calls += 1
            return _stream_result("partial" if provider_calls == 1 else "fresh")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            telemetry = GatewayTelemetryStore(
                root / "gateway-telemetry.sqlite3"
            )
            cache = SQLiteGatewayCache(root / "gateway-cache.sqlite3")
            try:
                with patch.object(
                    telemetry,
                    "record_request",
                    wraps=telemetry.record_request,
                ) as record:
                    runtime = GatewayRuntime(
                        egress=egress,
                        cache=cache,
                        telemetry=telemetry,
                    )

                    aborted = runtime.stream_events(_payload(stream=True))
                    self.assertEqual(
                        next(aborted)["type"],
                        "gateway.response.started",
                    )
                    self.assertEqual(
                        next(aborted)["type"],
                        "gateway.output_text.delta",
                    )
                    aborted.close()

                    self.assertEqual(record.call_count, 0)
                    completed = list(runtime.stream_events(_payload(stream=True)))

                    self.assertEqual(provider_calls, 2)
                    self.assertEqual(record.call_count, 1)
                    self.assertNotIn(
                        "exact_hit",
                        [
                            event.get("cache", {}).get("outcome")
                            for event in completed
                        ],
                    )
                    self.assertEqual(
                        [
                            event["status"]
                            for event in completed
                            if event["type"] == "gateway.response.completed"
                        ],
                        ["complete"],
                    )
            finally:
                cache.close()
                telemetry.close()

    def test_telemetry_write_failure_is_fail_open_for_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            telemetry = GatewayTelemetryStore(
                Path(directory) / "gateway-telemetry.sqlite3"
            )
            try:
                with patch.object(
                    telemetry,
                    "record_request",
                    side_effect=RuntimeError(
                        "private telemetry failure must not escape"
                    ),
                ) as record:
                    runtime = GatewayRuntime(
                        egress=lambda *_args, **_kwargs: _json_result(),
                        telemetry=telemetry,
                    )

                    response = runtime(_payload(stream=False))

                record.assert_called_once()
                self.assertEqual(response["object"], "gateway.response")
                self.assertEqual(response["status"], "complete")
                self.assertEqual(response["outputText"], _OUTPUT)
                self.assertNotIn("telemetry", repr(response).casefold())
                self.assertNotIn("failure", repr(response).casefold())
            finally:
                telemetry.close()


if __name__ == "__main__":
    unittest.main()
