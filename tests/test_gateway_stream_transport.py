from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path

import openusage_bar.gateway.server as gateway_server_module
from openusage_bar.gateway.api import GatewayRouter
from openusage_bar.gateway.cache import SQLiteGatewayCache
from openusage_bar.gateway.config import GatewayMode
from openusage_bar.gateway.providers import ProviderResult
from openusage_bar.gateway.runtime import GatewayRuntime


def _parse_sse(raw: bytes) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for block in raw.split(b"\n\n"):
        if not block:
            continue
        lines = block.splitlines()
        if (
            len(lines) != 2
            or not lines[0].startswith(b"event: ")
            or not lines[1].startswith(b"data: ")
        ):
            raise AssertionError(f"malformed Gateway SSE block: {block!r}")
        wire_name = lines[0].removeprefix(b"event: ").decode("ascii")
        data = json.loads(lines[1].removeprefix(b"data: ").decode("utf-8"))
        if type(data) is not dict:
            raise AssertionError("Gateway SSE data must be an object")
        events.append((wire_name, data))
    return events


class GatewayStreamTransportTests(unittest.TestCase):
    def test_exact_cache_hit_replays_gateway_native_sse_without_upstream_headers(
        self,
    ) -> None:
        calls: list[str] = []

        def egress(provider_id: str, _request_body: bytes, **_kwargs):
            calls.append(provider_id)
            return ProviderResult(
                200,
                (("Content-Type", "text/event-stream"),),
                (
                    b"event: response.output_text.delta\n"
                    b'data: {"type":"response.output_text.delta","delta":"cached"}\n\n'
                    b"event: response.completed\n"
                    b'data: {"type":"response.completed","response":'
                    b'{"usage":{"input_tokens":1,"output_tokens":1}}}\n\n',
                ),
            )

        payload = {
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "request": {
                "model": "gpt-4.1-mini",
                "input": "cache me",
                "stream": True,
            },
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            cache = SQLiteGatewayCache(
                Path(directory) / "gateway-cache.sqlite3"
            )
            stack.callback(cache.close)
            router = GatewayRouter(
                GatewayMode.GATEWAY,
                policy=None,
                proxy=GatewayRuntime(
                    egress=egress,
                    cache=cache,
                ),
            )

            first_status, first = router.dispatch("POST", "/gateway/v1/responses", body)
            second_status, encoded = router.dispatch_sse(
                "POST", "/gateway/v1/responses", body
            )

        self.assertEqual((first_status, second_status), (200, 200))
        with self.subTest(gate="bounded JSON contract"):
            self.assertEqual(
                {
                    "cache": first.get("cache"),
                    "tool": first.get("tool"),
                    "fallback": first.get("fallback"),
                },
                {
                    "cache": {"outcome": "miss"},
                    "tool": {"observed": False},
                    "fallback": {
                        "attempted": False,
                        "attemptCount": 1,
                        "finalAction": "none",
                    },
                },
            )
        self.assertEqual(calls, ["openai"])
        events = _parse_sse(encoded)
        expected_names = [
            "gateway.response.started",
            "gateway.cache.event",
            "gateway.output_text.delta",
            "gateway.response.completed",
        ]
        with self.subTest(gate="exact-hit event order"):
            self.assertEqual([name for name, _ in events], expected_names)
        framing_failures: list[str] = []
        for sequence, (wire_name, data) in enumerate(events):
            if wire_name != data.get("type"):
                framing_failures.append(f"event {sequence}: wire name != data.type")
            if data.get("apiVersion") != "gateway.openusage/v1":
                framing_failures.append(f"event {sequence}: missing apiVersion")
            if data.get("sequence") != sequence:
                framing_failures.append(f"event {sequence}: invalid sequence")
        with self.subTest(gate="Gateway-native framing"):
            self.assertEqual(framing_failures, [])

        if len(events) == 4:
            started = events[0][1]
            self.assertEqual(started["provider"], "openai")
            self.assertEqual(started["model"], "gpt-4.1-mini")
            self.assertEqual(events[1][1]["cache"], {"outcome": "exact_hit"})
            self.assertEqual(events[2][1]["text"], "cached")
            terminal = events[3][1]
            self.assertEqual(terminal["provider"], "openai")
            self.assertEqual(terminal["model"], "gpt-4.1-mini")
            self.assertEqual(terminal["status"], "complete")
            self.assertIsNone(terminal["usage"])
            self.assertEqual(terminal["tool"], {"observed": False})
            self.assertEqual(terminal["cache"], {"outcome": "exact_hit"})
            self.assertEqual(
                terminal["fallback"],
                {
                    "attempted": False,
                    "attemptCount": 0,
                    "finalAction": "none",
                },
            )
        self.assertNotIn(b"Content-Type", encoded)
        self.assertNotIn(b"response.output_text.delta", encoded)
        self.assertNotIn(b"event: response.completed\n", encoded)
        self.assertNotIn(b'"type":"response.completed"', encoded)

        # TODO(Task 9B): exercise a blocking, closeable ProviderEventStream
        # through the HTTP listener to prove slow-client backpressure and
        # client-disconnect closure once that public transport seam lands.

    def test_send_sse_closes_normalized_iterator_after_normal_completion(
        self,
    ) -> None:
        events = _CloseableEvents((_gateway_error_event(),))
        handler = _bare_handler(_Writer())

        gateway_server_module._GatewayHandler._send_sse(handler, 200, events)

        self.assertEqual(events.close_calls, 1)

    def test_send_sse_closes_normalized_iterator_on_every_failure_path(
        self,
    ) -> None:
        # This proves ownership of the already-normalized iterator only.  It
        # does not claim that Provider egress is incrementally streamed.
        cases = {
            "encoding": (
                _CloseableEvents(
                    (
                        {
                            **_gateway_error_event(),
                            "unencodable": object(),
                        },
                    )
                ),
                _Writer(),
            ),
            "write disconnect": (
                _CloseableEvents((_gateway_error_event(),)),
                _Writer(fail_at="write"),
            ),
            "flush disconnect": (
                _CloseableEvents((_gateway_error_event(),)),
                _Writer(fail_at="flush"),
            ),
        }
        for name, (events, writer) in cases.items():
            with self.subTest(case=name):
                handler = _bare_handler(writer)
                try:
                    gateway_server_module._GatewayHandler._send_sse(
                        handler, 200, events
                    )
                except (OSError, TypeError, ValueError):
                    pass
                self.assertEqual(events.close_calls, 1)


class _CloseableEvents:
    def __init__(self, events: tuple[dict[str, object], ...]) -> None:
        self._events = events
        self._index = 0
        self.close_calls = 0

    def __iter__(self):
        return self

    def __next__(self) -> dict[str, object]:
        if self._index >= len(self._events):
            raise StopIteration
        event = self._events[self._index]
        self._index += 1
        return event

    def close(self) -> None:
        self.close_calls += 1
        self._index = len(self._events)


class _Writer:
    def __init__(self, *, fail_at: str | None = None) -> None:
        self.fail_at = fail_at
        self.writes: list[bytes] = []

    def write(self, value: bytes) -> None:
        if self.fail_at == "write":
            raise BrokenPipeError("client disconnected")
        self.writes.append(value)

    def flush(self) -> None:
        if self.fail_at == "flush":
            raise BrokenPipeError("client disconnected")


def _gateway_error_event() -> dict[str, object]:
    return {
        "apiVersion": "gateway.openusage/v1",
        "type": "gateway.error",
        "sequence": 0,
        "error": {
            "code": "upstream_timeout",
            "message": "Provider request failed.",
            "retryable": True,
        },
    }


def _bare_handler(writer: _Writer):
    handler = object.__new__(gateway_server_module._GatewayHandler)
    handler.wfile = writer
    handler.close_connection = False
    handler.send_response = lambda _status: None
    handler.send_header = lambda _name, _value: None
    handler.end_headers = lambda: None
    return handler


if __name__ == "__main__":
    unittest.main()
