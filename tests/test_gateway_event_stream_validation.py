from __future__ import annotations

import json
import unittest

import openusage_bar.gateway.server as gateway_server
from openusage_bar.gateway.api import GatewayRouter
from openusage_bar.gateway.config import GatewayMode


API_VERSION = "gateway.openusage/v1"
PRIVATE_CANARY = "raw-provider-private-canary"


def _started(sequence: int = 0) -> dict[str, object]:
    return {
        "apiVersion": API_VERSION,
        "type": "gateway.response.started",
        "sequence": sequence,
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "stream": True,
    }


def _delta(sequence: int = 1) -> dict[str, object]:
    return {
        "apiVersion": API_VERSION,
        "type": "gateway.output_text.delta",
        "sequence": sequence,
        "text": "public output",
    }


def _completed(sequence: int = 2, status: str = "complete") -> dict[str, object]:
    return {
        "apiVersion": API_VERSION,
        "type": "gateway.response.completed",
        "sequence": sequence,
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "status": status,
        "usage": {"inputTokens": 1, "outputTokens": 2},
        "tool": {"observed": False},
        "cache": {"outcome": "miss"},
        "fallback": {
            "attempted": False,
            "attemptCount": 1,
            "finalAction": "none",
        },
    }


class _CloseableEvents:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self._events = iter(events)
        self.pull_count = 0
        self.close_calls = 0

    def __iter__(self):
        return self

    def __next__(self) -> dict[str, object]:
        self.pull_count += 1
        return next(self._events)

    def close(self) -> None:
        self.close_calls += 1


class _Proxy:
    def __init__(self, source: _CloseableEvents) -> None:
        self.source = source

    def __call__(self, _payload: dict[str, object]) -> dict[str, object]:
        return {
            "apiVersion": API_VERSION,
            "object": "gateway.error",
            "error": {
                "code": "internal_error",
                "message": "Request could not be completed.",
                "retryable": True,
            },
        }

    def stream_events(self, _payload: dict[str, object]) -> _CloseableEvents:
        return self.source


class _Writer:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, value: bytes) -> None:
        self.writes.append(value)

    def flush(self) -> None:
        pass


def _handler(writer: _Writer):
    handler = object.__new__(gateway_server._GatewayHandler)
    handler.wfile = writer
    handler.close_connection = False
    handler.send_response = lambda _status: None
    handler.send_header = lambda _name, _value: None
    handler.end_headers = lambda: None
    return handler


def _request_body() -> bytes:
    return json.dumps(
        {
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "request": {
                "model": "gpt-4.1-mini",
                "input": "offline fixture",
                "stream": True,
            },
        },
        separators=(",", ":"),
    ).encode()


def _parse_sse(raw: bytes) -> list[tuple[str, dict[str, object]]]:
    parsed: list[tuple[str, dict[str, object]]] = []
    for block in raw.split(b"\n\n"):
        if not block:
            continue
        event_line, data_line = block.splitlines()
        data = json.loads(
            data_line.removeprefix(b"data: "),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON value: {value}")
            ),
        )
        if type(data) is not dict:
            raise AssertionError("SSE data must be an object")
        parsed.append((event_line.removeprefix(b"event: ").decode(), data))
    return parsed


class GatewayEventStreamValidationTests(unittest.TestCase):
    def _delivery(self, source: _CloseableEvents):
        router = GatewayRouter(GatewayMode.GATEWAY, None, _Proxy(source))
        status, delivery = router.dispatch_events(
            "POST", "/gateway/v1/responses", _request_body()
        )
        self.assertEqual(status, 200)
        self.assertNotIsInstance(delivery, dict)
        return delivery

    def test_valid_event_sequence_stays_lazy_and_reaches_wire(self) -> None:
        source = _CloseableEvents([_started(), _delta(), _completed()])
        delivery = self._delivery(source)

        self.assertEqual(source.pull_count, 0, "dispatch must not pre-consume SSE")
        writer = _Writer()
        gateway_server._GatewayHandler._send_sse(_handler(writer), 200, delivery)

        events = _parse_sse(b"".join(writer.writes))
        self.assertEqual(
            [(name, data["sequence"]) for name, data in events],
            [
                ("gateway.response.started", 0),
                ("gateway.output_text.delta", 1),
                ("gateway.response.completed", 2),
            ],
        )
        self.assertEqual(source.close_calls, 1)

    def test_invalid_source_events_fail_closed_before_the_wire(self) -> None:
        private = {**_delta(), "rawProviderError": PRIVATE_CANARY}
        non_finite = _completed(1)
        non_finite["usage"] = {"inputTokens": float("nan"), "outputTokens": 2}
        cases = {
            "private extra": ([_started(), private], [b"rawProviderError", PRIVATE_CANARY.encode()]),
            "non-finite field": ([_started(), non_finite], [b"NaN"]),
            "wrong first sequence": ([_started(1)], [b'"sequence":1']),
            "sequence gap": ([_started(), _delta(2)], [b'"sequence":2']),
            "unknown type": (
                [_started(), {"apiVersion": API_VERSION, "type": "gateway.provider.raw", "sequence": 1}],
                [b"gateway.provider.raw"],
            ),
            "wrong type": (
                [_started(), {"apiVersion": API_VERSION, "type": 7, "sequence": 1}],
                [b'"type":7'],
            ),
            "terminal status mismatch": (
                [_started(), _completed(1, "interrupted")],
                [b'"status":"interrupted"', b"gateway.response.completed"],
            ),
        }
        for name, (events, forbidden) in cases.items():
            with self.subTest(case=name):
                source = _CloseableEvents(events)
                delivery = self._delivery(source)
                writer = _Writer()
                try:
                    gateway_server._GatewayHandler._send_sse(
                        _handler(writer), 200, delivery
                    )
                except (TypeError, ValueError, UnicodeError):
                    pass
                raw = b"".join(writer.writes)

                for marker in forbidden:
                    self.assertNotIn(marker, raw)
                self.assertEqual(source.close_calls, 1)
                safe = _parse_sse(raw)
                self.assertLessEqual(len(safe), 2)
                for sequence, (wire_name, event) in enumerate(safe):
                    self.assertEqual(wire_name, event.get("type"))
                    self.assertEqual(event.get("apiVersion"), API_VERSION)
                    self.assertEqual(event.get("sequence"), sequence)
                    self.assertIn(
                        wire_name,
                        {"gateway.response.started", "gateway.error"},
                    )
                errors = [event for wire, event in safe if wire == "gateway.error"]
                self.assertLessEqual(len(errors), 1)
                if errors:
                    self.assertIs(errors[0], safe[-1][1])
                    self.assertEqual(set(errors[0]), {"apiVersion", "type", "sequence", "error"})
                    detail = errors[0]["error"]
                    self.assertIs(type(detail), dict)
                    self.assertEqual(set(detail), {"code", "message", "retryable"})


if __name__ == "__main__":
    unittest.main()
