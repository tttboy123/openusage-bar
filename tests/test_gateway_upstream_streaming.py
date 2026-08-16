"""RED contract tests for true, closeable upstream Gateway streaming.

These tests intentionally freeze only two new public seams:

* ``providers.ProviderEventStream`` owns a lazy upstream byte iterator.
* ``GatewayRuntime.stream_events(payload)`` is consumed through
  ``GatewayRouter.dispatch_events`` and remains closeable end to end.

All Provider traffic is represented by deterministic in-memory iterators.  No
test in this module opens a socket, reads a credential, or waits on wall time.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import Iterable
from unittest.mock import patch

import openusage_bar.gateway.providers as providers_module
import openusage_bar.gateway.server as gateway_server_module
from openusage_bar.gateway.api import GatewayRouter
from openusage_bar.gateway.cache import SQLiteGatewayCache
from openusage_bar.gateway.contracts import Decision, GatewayMode
from openusage_bar.gateway.fallback import Candidate, FallbackSelector
from openusage_bar.gateway.providers import GatewayProviderError
from openusage_bar.gateway.runtime import GatewayRuntime


_PROVIDERS = ("openai", "anthropic", "deepseek", "openrouter", "ollama")
_PRIVATE_HEADER = "Bearer sk-upstream-private"
_PRIVATE_REQUEST_ID = "provider-request-private-42"
_RAW_ERROR_CANARY = "raw-upstream-error-must-not-leak"
_PUBLIC_OUTPUT_CANARY = "deliberate-public-output"


def _sse(event: str | None, data: object) -> bytes:
    event_line = b"" if event is None else f"event: {event}\n".encode("ascii")
    payload = (
        b"[DONE]"
        if data == "[DONE]"
        else json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return event_line + b"data: " + payload + b"\n\n"


def _ndjson(data: object) -> bytes:
    return (
        json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _complete_wire(provider_id: str, text: str) -> tuple[str, bytes]:
    if provider_id == "openai":
        return (
            "text/event-stream",
            b"".join(
                (
                    _sse(
                        "response.output_text.delta",
                        {
                            "type": "response.output_text.delta",
                            "delta": text,
                            "provider_request_id": _PRIVATE_REQUEST_ID,
                        },
                    ),
                    _sse(
                        "response.completed",
                        {
                            "type": "response.completed",
                            "response": {
                                "id": _PRIVATE_REQUEST_ID,
                                "usage": {
                                    "input_tokens": 4,
                                    "output_tokens": 2,
                                },
                            },
                        },
                    ),
                )
            ),
        )
    if provider_id == "anthropic":
        return (
            "text/event-stream",
            b"".join(
                (
                    _sse(
                        "message_start",
                        {
                            "type": "message_start",
                            "message": {
                                "id": _PRIVATE_REQUEST_ID,
                                "usage": {"input_tokens": 4},
                            },
                        },
                    ),
                    _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": text},
                        },
                    ),
                    _sse(
                        "message_delta",
                        {
                            "type": "message_delta",
                            "usage": {"output_tokens": 2},
                        },
                    ),
                    _sse("message_stop", {"type": "message_stop"}),
                )
            ),
        )
    if provider_id in {"deepseek", "openrouter"}:
        return (
            "text/event-stream",
            b"".join(
                (
                    _sse(
                        None,
                        {
                            "id": _PRIVATE_REQUEST_ID,
                            "choices": [{"delta": {"content": text}}],
                        },
                    ),
                    _sse(
                        None,
                        {
                            "choices": [],
                            "usage": {
                                "prompt_tokens": 4,
                                "completion_tokens": 2,
                            },
                        },
                    ),
                    _sse(None, "[DONE]"),
                )
            ),
        )
    if provider_id == "ollama":
        return (
            "application/x-ndjson",
            b"".join(
                (
                    _ndjson(
                        {
                            "message": {"role": "assistant", "content": text},
                            "done": False,
                            "provider_request_id": _PRIVATE_REQUEST_ID,
                        }
                    ),
                    _ndjson(
                        {
                            "message": {"role": "assistant", "content": ""},
                            "done": True,
                            "prompt_eval_count": 4,
                            "eval_count": 2,
                        }
                    ),
                )
            ),
        )
    raise AssertionError(f"unsupported fixture provider: {provider_id}")


def _payload(provider_id: str = "openai") -> dict[str, object]:
    return {
        "provider": provider_id,
        "model": "test-model",
        "request": {
            "model": "test-model",
            "input": "offline fixture",
            "stream": True,
        },
    }


def _body(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class _CloseProbe:
    def __init__(self) -> None:
        self.calls = 0

    def close(self) -> None:
        self.calls += 1


class _TrackedChunks:
    def __init__(
        self,
        chunks: Iterable[bytes],
        *,
        failure: BaseException | None = None,
    ) -> None:
        self._chunks = tuple(chunks)
        self._index = 0
        self._failure = failure
        self._failure_raised = False
        self.pull_count = 0

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        self.pull_count += 1
        if self._index < len(self._chunks):
            chunk = self._chunks[self._index]
            self._index += 1
            return chunk
        if self._failure is not None and not self._failure_raised:
            self._failure_raised = True
            raise self._failure
        raise StopIteration


class _FakeHeaders:
    def __init__(self, items: tuple[object, ...]) -> None:
        self._items = items

    def items(self) -> tuple[object, ...]:
        return self._items


class _FakeUrllibResponse:
    fp = None

    def __init__(
        self,
        *,
        status: object = 200,
        headers: tuple[object, ...] = (
            ("Content-Type", "application/x-ndjson"),
        ),
        chunks: Iterable[bytes] = (),
    ) -> None:
        self.status = status
        self.headers = _FakeHeaders(headers)
        self._chunks = iter(chunks)
        self.read_calls = 0
        self.close_calls = 0

    def getcode(self) -> object:
        return self.status

    def read(self, _size: int) -> bytes:
        self.read_calls += 1
        return next(self._chunks, b"")

    def close(self) -> None:
        self.close_calls += 1


class _FakeOpener:
    def __init__(self, response: _FakeUrllibResponse) -> None:
        self._response = response
        self.open_calls = 0

    def open(self, _request, *, timeout: float) -> _FakeUrllibResponse:
        self.open_calls += 1
        self.timeout = timeout
        return self._response


def _production_ollama_provider(
    response: _FakeUrllibResponse,
):
    transport_type = getattr(
        providers_module,
        "_UrllibProviderTransport",
        None,
    )
    if transport_type is None:
        raise AssertionError("production urllib transport seam is unavailable")
    opener = _FakeOpener(response)
    transport = transport_type(
        opener=opener,
        monotonic=lambda: 0.0,
    )

    def forbidden_resolver(_host: str) -> list[str]:
        raise AssertionError("offline urllib acceptance must not resolve DNS")

    provider = next(
        item
        for item in providers_module.default_gateway_providers(
            transport=transport,
            resolver=forbidden_resolver,
        )
        if item.provider_id == "ollama"
    )
    return provider, opener


def _ollama_stream_body() -> bytes:
    return json.dumps(
        {
            "model": "test-model",
            "messages": [{"role": "user", "content": "offline fixture"}],
            "stream": True,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _provider_event_stream(
    content_type: str,
    chunks: Iterable[bytes],
    *,
    close_probe: _CloseProbe | None = None,
    private_headers: bool = False,
):
    stream_type = getattr(providers_module, "ProviderEventStream", None)
    if stream_type is None:
        raise AssertionError(
            "Task 9B requires providers.ProviderEventStream"
        )
    headers: tuple[tuple[str, str], ...] = (("Content-Type", content_type),)
    if private_headers:
        headers += (
            ("Authorization", _PRIVATE_HEADER),
            ("X-Request-Id", _PRIVATE_REQUEST_ID),
        )
    return stream_type(
        status_code=200,
        headers=headers,
        chunks=chunks,
        close_upstream=None if close_probe is None else close_probe.close,
    )


def _router(runtime: GatewayRuntime) -> GatewayRouter:
    return GatewayRouter(
        GatewayMode.GATEWAY,
        policy=None,
        proxy=runtime,
    )


def _dispatch_events(
    runtime: GatewayRuntime,
    payload: dict[str, object],
):
    status, delivery = _router(runtime).dispatch_events(
        "POST",
        "/gateway/v1/responses",
        _body(payload),
    )
    if status != 200 or type(delivery) is dict:
        raise AssertionError(f"expected accepted event stream, got status {status}")
    return delivery


def _text(events: Iterable[dict[str, object]]) -> str:
    return "".join(
        event["text"]
        for event in events
        if event.get("type") == "gateway.output_text.delta"
    )


def _terminal_events(
    events: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    terminal_types = {
        "gateway.response.completed",
        "gateway.response.interrupted",
        "gateway.response.uncertain",
        "gateway.error",
    }
    return [event for event in events if event.get("type") in terminal_types]


class _FailOnWrite:
    def __init__(
        self,
        fail_on_write: int | None = None,
        *,
        fail_on_flush: int | None = None,
    ) -> None:
        self._fail_on_write = fail_on_write
        self._fail_on_flush = fail_on_flush
        self.write_calls = 0
        self.flush_calls = 0
        self.accepted_writes: list[bytes] = []
        self._pending: bytes | None = None

    def write(self, value: bytes) -> None:
        self.write_calls += 1
        if self.write_calls == self._fail_on_write:
            raise BrokenPipeError("offline client abort")
        self._pending = value

    def flush(self) -> None:
        self.flush_calls += 1
        if self.flush_calls == self._fail_on_flush:
            raise BrokenPipeError("offline client abort")
        if self._pending is not None:
            self.accepted_writes.append(self._pending)
            self._pending = None


def _bare_handler(writer: _FailOnWrite):
    handler = object.__new__(gateway_server_module._GatewayHandler)
    handler.wfile = writer
    handler.close_connection = False
    handler.send_response = lambda _status: None
    handler.send_header = lambda _name, _value: None
    handler.end_headers = lambda: None
    return handler


class GatewayUpstreamStreamingContractTests(unittest.TestCase):
    def test_production_urllib_stream_is_lazy_and_close_is_idempotent(
        self,
    ) -> None:
        content_type, wire = _complete_wire("ollama", "lazy")
        response = _FakeUrllibResponse(
            headers=(("Content-Type", content_type),),
            chunks=(wire,),
        )
        provider, opener = _production_ollama_provider(response)

        stream = provider.call(_ollama_stream_body(), credential="")

        self.assertIsInstance(stream, providers_module.ProviderEventStream)
        self.assertEqual(opener.open_calls, 1)
        self.assertEqual(response.read_calls, 0)
        self.assertEqual(response.close_calls, 0)

        self.assertEqual(next(stream), wire)
        self.assertEqual(response.read_calls, 1)

        stream.close()
        stream.close()

        self.assertEqual(response.close_calls, 1)

    def test_production_urllib_stream_setup_failures_close_response_once(
        self,
    ) -> None:
        cases = {
            "invalid status": _FakeUrllibResponse(status=700),
            "malformed header shape": _FakeUrllibResponse(
                headers=(("Content-Type", "application/x-ndjson"), ("Broken",)),
            ),
            "constructor rejects header newline": _FakeUrllibResponse(
                headers=(
                    ("Content-Type", "application/x-ndjson"),
                    ("X-Broken", "line\nbreak"),
                ),
            ),
            "adapter rejects stream content type": _FakeUrllibResponse(
                headers=(("Content-Type", "text/event-stream"),),
            ),
            "constructor raises": _FakeUrllibResponse(),
        }
        for name, response in cases.items():
            with self.subTest(case=name):
                provider, opener = _production_ollama_provider(response)
                if name == "constructor raises":
                    with patch.object(
                        providers_module,
                        "ProviderEventStream",
                        side_effect=RuntimeError("offline constructor failure"),
                    ):
                        with self.assertRaises(GatewayProviderError):
                            provider.call(_ollama_stream_body(), credential="")
                else:
                    with self.assertRaises(GatewayProviderError):
                        provider.call(_ollama_stream_body(), credential="")

                self.assertEqual(opener.open_calls, 1)
                self.assertEqual(response.read_calls, 0)
                self.assertEqual(response.close_calls, 1)

    def test_provider_event_stream_is_lazy_closeable_and_log_safe(self) -> None:
        chunks = _TrackedChunks((b"first", b"second"))
        close_probe = _CloseProbe()

        stream = _provider_event_stream(
            "text/event-stream",
            chunks,
            close_probe=close_probe,
            private_headers=True,
        )

        self.assertEqual(chunks.pull_count, 0)
        self.assertIs(iter(stream), stream)
        self.assertEqual(next(stream), b"first")
        self.assertEqual(chunks.pull_count, 1)
        self.assertNotIn(_PRIVATE_HEADER, repr(stream))
        self.assertNotIn(_PRIVATE_REQUEST_ID, repr(stream))

        stream.close()
        stream.close()

        self.assertEqual(close_probe.calls, 1)
        with self.assertRaises(StopIteration):
            next(stream)

    def test_first_delta_is_visible_before_exhaustion_and_obeys_backpressure(
        self,
    ) -> None:
        delta = _sse(
            "response.output_text.delta",
            {"type": "response.output_text.delta", "delta": "first"},
        )
        terminal = _sse(
            "response.completed",
            {
                "type": "response.completed",
                "response": {"usage": {"input_tokens": 1, "output_tokens": 1}},
            },
        )
        chunks = _TrackedChunks((delta, terminal))
        stream = _provider_event_stream("text/event-stream", chunks)
        runtime = GatewayRuntime(egress=lambda *_args, **_kwargs: stream)

        self.assertTrue(callable(getattr(runtime, "stream_events", None)))
        delivery = _dispatch_events(runtime, _payload())
        self.assertTrue(callable(getattr(delivery, "close", None)))
        events = iter(delivery)
        started = next(events)
        first_delta = None
        for _ in range(4):
            candidate = next(events)
            if candidate.get("type") == "gateway.output_text.delta":
                first_delta = candidate
                break

        self.assertEqual(started["type"], "gateway.response.started")
        self.assertIsNotNone(first_delta)
        self.assertEqual(first_delta["apiVersion"], "gateway.openusage/v1")
        self.assertEqual(first_delta["type"], "gateway.output_text.delta")
        self.assertGreaterEqual(first_delta["sequence"], 1)
        self.assertEqual(first_delta["text"], "first")
        self.assertEqual(chunks.pull_count, 1)
        self.assertLess(chunks.pull_count, 2)

        remaining = list(events)

        self.assertGreaterEqual(chunks.pull_count, 2)
        self.assertLessEqual(chunks.pull_count, 3)
        self.assertEqual(_terminal_events(remaining)[0]["status"], "complete")

    def test_five_providers_normalize_arbitrary_utf8_and_framing_boundaries(
        self,
    ) -> None:
        expected_text = "hé🙂llo"
        for provider_id in _PROVIDERS:
            with self.subTest(provider=provider_id):
                content_type, wire = _complete_wire(provider_id, expected_text)
                chunks = _TrackedChunks(bytes((byte,)) for byte in wire)
                stream = _provider_event_stream(content_type, chunks)
                runtime = GatewayRuntime(egress=lambda *_args, **_kwargs: stream)

                events = list(_dispatch_events(runtime, _payload(provider_id)))

                self.assertEqual(events[0]["type"], "gateway.response.started")
                self.assertEqual(_text(events), expected_text)
                self.assertEqual(
                    [event["type"] for event in _terminal_events(events)],
                    ["gateway.response.completed"],
                )
                terminal = _terminal_events(events)[0]
                self.assertEqual(
                    terminal["usage"],
                    {"inputTokens": 4, "outputTokens": 2},
                )
                encoded = json.dumps(events, ensure_ascii=False, sort_keys=True)
                self.assertNotIn(_PRIVATE_REQUEST_ID, encoded)
                self.assertNotIn("response.output_text.delta", encoded)
                self.assertNotIn("message_stop", encoded)

    def test_client_abort_closes_upstream_once_and_never_caches_partial_output(
        self,
    ) -> None:
        partial = _sse(
            "response.output_text.delta",
            {"type": "response.output_text.delta", "delta": "partial"},
        )
        complete_type, complete_wire = _complete_wire("openai", "fresh")
        close_probe = _CloseProbe()
        calls = 0

        def egress(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return _provider_event_stream(
                    "text/event-stream",
                    _TrackedChunks((partial,)),
                    close_probe=close_probe,
                )
            return _provider_event_stream(
                complete_type,
                _TrackedChunks((complete_wire,)),
            )

        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            cache = SQLiteGatewayCache(
                Path(directory) / "gateway-cache.sqlite3"
            )
            stack.callback(cache.close)
            runtime = GatewayRuntime(
                egress=egress,
                cache=cache,
            )
            delivery = _dispatch_events(runtime, _payload())
            handler = _bare_handler(_FailOnWrite(fail_on_write=2))

            with self.assertRaises(BrokenPipeError):
                gateway_server_module._GatewayHandler._send_sse(
                    handler,
                    200,
                    delivery,
                )

            self.assertEqual(close_probe.calls, 1)
            second = list(_dispatch_events(runtime, _payload()))

        self.assertEqual(calls, 2)
        self.assertNotIn(
            "exact_hit",
            [event.get("cache", {}).get("outcome") for event in second],
        )
        self.assertEqual(_text(second), "fresh")

    def test_terminal_sse_write_or_flush_failure_does_not_prime_cache(
        self,
    ) -> None:
        content_type, terminal_wire = _complete_wire("openai", "terminal")
        _, retry_wire = _complete_wire("openai", "fresh")
        failures = {
            "terminal write": {"fail_on_write": 3},
            "terminal flush": {"fail_on_flush": 3},
        }
        for name, writer_options in failures.items():
            with (
                self.subTest(case=name),
                tempfile.TemporaryDirectory() as directory,
                ExitStack() as stack,
            ):
                close_probe = _CloseProbe()
                calls = 0

                def egress(*_args, **_kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        return _provider_event_stream(
                            content_type,
                            _TrackedChunks((terminal_wire,)),
                            close_probe=close_probe,
                        )
                    return _provider_event_stream(
                        content_type,
                        _TrackedChunks((retry_wire,)),
                    )

                cache = SQLiteGatewayCache(
                    Path(directory) / "gateway-cache.sqlite3"
                )
                stack.callback(cache.close)
                runtime = GatewayRuntime(
                    egress=egress,
                    cache=cache,
                )
                delivery = _dispatch_events(runtime, _payload())
                writer = _FailOnWrite(**writer_options)
                handler = _bare_handler(writer)

                with self.assertRaises(BrokenPipeError):
                    gateway_server_module._GatewayHandler._send_sse(
                        handler,
                        200,
                        delivery,
                    )

                self.assertEqual(
                    [
                        raw.split(b"\n", 1)[0]
                        for raw in writer.accepted_writes
                    ],
                    [
                        b"event: gateway.response.started",
                        b"event: gateway.output_text.delta",
                    ],
                )
                self.assertEqual(close_probe.calls, 1)

                second = list(_dispatch_events(runtime, _payload()))

                self.assertEqual(calls, 2)
                self.assertNotIn(
                    "exact_hit",
                    [event.get("cache", {}).get("outcome") for event in second],
                )
                self.assertEqual(_text(second), "fresh")

    def test_missing_or_malformed_terminal_is_uncertain_once_and_not_cached(
        self,
    ) -> None:
        delta = _sse(
            "response.output_text.delta",
            {"type": "response.output_text.delta", "delta": "partial"},
        )
        cases = {
            "missing terminal": (delta,),
            "malformed terminal": (delta, b"data: {not-json}\n\n"),
        }
        for name, first_chunks in cases.items():
            with (
                self.subTest(case=name),
                tempfile.TemporaryDirectory() as directory,
                ExitStack() as stack,
            ):
                calls = 0

                def egress(*_args, **_kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        return _provider_event_stream(
                            "text/event-stream",
                            _TrackedChunks(first_chunks),
                        )
                    content_type, wire = _complete_wire("openai", "fresh")
                    return _provider_event_stream(
                        content_type,
                        _TrackedChunks((wire,)),
                    )

                cache = SQLiteGatewayCache(
                    Path(directory) / "gateway-cache.sqlite3"
                )
                stack.callback(cache.close)
                runtime = GatewayRuntime(
                    egress=egress,
                    cache=cache,
                )

                first = list(_dispatch_events(runtime, _payload()))
                second = list(_dispatch_events(runtime, _payload()))

                self.assertEqual(
                    [event["type"] for event in _terminal_events(first)],
                    ["gateway.response.uncertain"],
                )
                self.assertEqual(calls, 2)
                self.assertEqual(_text(second), "fresh")
                self.assertNotIn(
                    "exact_hit",
                    [event.get("cache", {}).get("outcome") for event in second],
                )

    def test_retryable_failure_after_delta_or_tool_never_falls_back(self) -> None:
        visible_events = {
            "delta": _sse(
                "response.output_text.delta",
                {"type": "response.output_text.delta", "delta": "visible"},
            ),
            "tool": _sse(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "item": {"type": "function_call", "name": "side_effect"},
                },
            ),
        }
        for name, visible in visible_events.items():
            with self.subTest(replay_barrier=name):
                calls: list[str] = []

                def egress(provider_id: str, *_args, **_kwargs):
                    calls.append(provider_id)
                    return _provider_event_stream(
                        "text/event-stream",
                        _TrackedChunks(
                            (visible,),
                            failure=GatewayProviderError("upstream_timeout", True),
                        ),
                    )

                runtime = GatewayRuntime(
                    egress=egress,
                    fallback_selector=FallbackSelector(cost_cap_multiplier=3.0),
                    fallback_candidates=(
                        Candidate("ollama", "fallback-model", 0.0, Decision.YES),
                    ),
                )

                events = list(_dispatch_events(runtime, _payload()))

                self.assertEqual(calls, ["openai"])
                self.assertNotIn(
                    "gateway.fallback.event",
                    [event["type"] for event in events],
                )
                self.assertEqual(len(_terminal_events(events)), 1)
                if name == "delta":
                    self.assertEqual(_text(events), "visible")
                else:
                    self.assertIn(
                        "gateway.tool.observed",
                        [event["type"] for event in events],
                    )

    def test_json_aggregation_and_realtime_events_expose_identical_text(self) -> None:
        calls = 0

        def egress(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            content_type, wire = _complete_wire("openai", "same hé🙂 text")
            return _provider_event_stream(
                content_type,
                _TrackedChunks(bytes((byte,)) for byte in wire),
            )

        runtime = GatewayRuntime(egress=egress)
        payload = _payload()

        bounded = runtime(payload)
        realtime = list(_dispatch_events(runtime, payload))

        self.assertEqual(calls, 2)
        self.assertEqual(bounded["object"], "gateway.response")
        self.assertEqual(bounded["status"], "complete")
        self.assertEqual(_text(realtime), bounded["outputText"])
        self.assertEqual(
            _terminal_events(realtime)[0]["status"],
            bounded["status"],
        )

    def test_public_events_and_safe_reprs_strip_upstream_private_material(self) -> None:
        visible = _sse(
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "delta": _PUBLIC_OUTPUT_CANARY,
                "id": _PRIVATE_REQUEST_ID,
            },
        )
        chunks = _TrackedChunks(
            (visible,),
            failure=RuntimeError(_RAW_ERROR_CANARY),
        )
        stream = _provider_event_stream(
            "text/event-stream",
            chunks,
            private_headers=True,
        )
        runtime = GatewayRuntime(egress=lambda *_args, **_kwargs: stream)
        delivery = _dispatch_events(runtime, _payload())
        safe_reprs = repr(stream) + repr(delivery) + repr(runtime)

        events = list(delivery)
        encoded = json.dumps(events, ensure_ascii=False, sort_keys=True)

        self.assertIn(_PUBLIC_OUTPUT_CANARY, encoded)
        self.assertNotIn(_PUBLIC_OUTPUT_CANARY, safe_reprs)
        for forbidden in (
            _PRIVATE_HEADER,
            _PRIVATE_REQUEST_ID,
            _RAW_ERROR_CANARY,
            "Authorization",
            "X-Request-Id",
        ):
            self.assertNotIn(forbidden, encoded)
            self.assertNotIn(forbidden, safe_reprs)


if __name__ == "__main__":
    unittest.main()
