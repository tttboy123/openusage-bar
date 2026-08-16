from __future__ import annotations

import json
import unittest

from openusage_bar.gateway.streaming import (
    StreamAggregator,
    StreamResult,
    TokenUsage,
)


def _sse(event: str | None, data: object) -> bytes:
    event_line = b"" if event is None else f"event: {event}\n".encode("utf-8")
    if data == "[DONE]":
        payload = b"[DONE]"
    else:
        payload = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    return event_line + b"data: " + payload + b"\n\n"


def _ndjson(data: object) -> bytes:
    return (
        json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _complete_wire(provider_id: str, text: str = "héllo") -> tuple[str, bytes]:
    if provider_id == "openai":
        return (
            "text/event-stream",
            b"".join(
                (
                    _sse(
                        "response.output_text.delta",
                        {"type": "response.output_text.delta", "delta": text[:2]},
                    ),
                    _sse(
                        "response.output_text.delta",
                        {"type": "response.output_text.delta", "delta": text[2:]},
                    ),
                    _sse(
                        "response.completed",
                        {
                            "type": "response.completed",
                            "response": {
                                "usage": {"input_tokens": 4, "output_tokens": 2}
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
                            "message": {"usage": {"input_tokens": 4}},
                        },
                    ),
                    _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
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
                    _sse(None, {"choices": [{"delta": {"content": text}}]}),
                    _sse(
                        None,
                        {
                            "choices": [],
                            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
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
    raise AssertionError(f"unknown test provider: {provider_id}")


def _aggregate(
    provider_id: str,
    content_type: str,
    wire: bytes,
    *,
    one_byte_at_a_time: bool = False,
    interrupted: bool = False,
) -> StreamResult:
    aggregator = StreamAggregator(provider_id, content_type)
    chunks = (bytes((byte,)) for byte in wire) if one_byte_at_a_time else (wire,)
    for chunk in chunks:
        aggregator.feed(chunk)
    return aggregator.finish(interrupted=interrupted)


class GatewayStreamAggregationTests(unittest.TestCase):
    def test_five_native_streams_survive_arbitrary_utf8_byte_splits(self) -> None:
        for provider_id in (
            "openai",
            "anthropic",
            "deepseek",
            "openrouter",
            "ollama",
        ):
            with self.subTest(provider=provider_id):
                content_type, wire = _complete_wire(provider_id)
                result = _aggregate(
                    provider_id,
                    content_type,
                    wire,
                    one_byte_at_a_time=True,
                )

                self.assertIsInstance(result, StreamResult)
                self.assertEqual(result.status, "complete")
                self.assertEqual(result.text, "héllo")
                self.assertEqual(result.usage, TokenUsage(4, 2))
                self.assertFalse(result.has_tool_use)
                self.assertTrue(result.cacheable)
                self.assertGreater(result.event_count, 0)
                self.assertIsInstance(result.canonical_body, bytes)

    def test_final_usage_snapshots_are_not_added_together(self) -> None:
        wire = b"".join(
            (
                _sse(
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {"usage": {"input_tokens": 7}},
                    },
                ),
                _sse(
                    "message_delta",
                    {"type": "message_delta", "usage": {"output_tokens": 1}},
                ),
                _sse(
                    "message_delta",
                    {"type": "message_delta", "usage": {"output_tokens": 3}},
                ),
                _sse("message_stop", {"type": "message_stop"}),
            )
        )

        result = _aggregate("anthropic", "text/event-stream", wire)

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.usage, TokenUsage(7, 3))

    def test_provider_native_tool_calls_are_never_cacheable(self) -> None:
        fixtures = {
            "openai": (
                "text/event-stream",
                b"".join(
                    (
                        _sse(
                            "response.output_item.added",
                            {
                                "type": "response.output_item.added",
                                "item": {"type": "function_call", "name": "lookup"},
                            },
                        ),
                        _sse(
                            "response.completed",
                            {
                                "type": "response.completed",
                                "response": {
                                    "usage": {
                                        "input_tokens": 1,
                                        "output_tokens": 1,
                                    }
                                },
                            },
                        ),
                    )
                ),
            ),
            "anthropic": (
                "text/event-stream",
                b"".join(
                    (
                        _sse(
                            "message_start",
                            {
                                "type": "message_start",
                                "message": {"usage": {"input_tokens": 1}},
                            },
                        ),
                        _sse(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "content_block": {"type": "tool_use", "name": "lookup"},
                            },
                        ),
                        _sse(
                            "message_delta",
                            {
                                "type": "message_delta",
                                "usage": {"output_tokens": 1},
                            },
                        ),
                        _sse("message_stop", {"type": "message_stop"}),
                    )
                ),
            ),
            "deepseek": (
                "text/event-stream",
                b"".join(
                    (
                        _sse(
                            None,
                            {
                                "choices": [
                                    {"delta": {"tool_calls": [{"id": "call-1"}]}}
                                ]
                            },
                        ),
                        _sse(None, "[DONE]"),
                    )
                ),
            ),
            "openrouter": (
                "text/event-stream",
                b"".join(
                    (
                        _sse(
                            None,
                            {
                                "choices": [
                                    {"delta": {"tool_calls": [{"id": "call-1"}]}}
                                ]
                            },
                        ),
                        _sse(None, "[DONE]"),
                    )
                ),
            ),
            "ollama": (
                "application/x-ndjson",
                _ndjson(
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{"function": {"name": "lookup"}}],
                        },
                        "done": True,
                    }
                ),
            ),
        }

        for provider_id, (content_type, wire) in fixtures.items():
            with self.subTest(provider=provider_id):
                result = _aggregate(provider_id, content_type, wire)

                self.assertEqual(result.status, "complete")
                self.assertTrue(result.has_tool_use)
                self.assertFalse(result.cacheable)

    def test_interrupted_stream_overrides_a_terminal_marker(self) -> None:
        content_type, wire = _complete_wire("openai")

        result = _aggregate(
            "openai",
            content_type,
            wire,
            interrupted=True,
        )

        self.assertEqual(result.status, "interrupted")
        self.assertFalse(result.cacheable)

    def test_missing_terminal_marker_is_uncertain_and_never_cacheable(self) -> None:
        fixtures = (
            (
                "openai",
                "text/event-stream",
                _sse(
                    "response.output_text.delta",
                    {"type": "response.output_text.delta", "delta": "partial"},
                ),
            ),
            (
                "anthropic",
                "text/event-stream",
                _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "partial"},
                    },
                ),
            ),
            (
                "deepseek",
                "text/event-stream",
                _sse(None, {"choices": [{"delta": {"content": "partial"}}]}),
            ),
            (
                "openrouter",
                "text/event-stream",
                _sse(None, {"choices": [{"delta": {"content": "partial"}}]}),
            ),
            (
                "ollama",
                "application/x-ndjson",
                _ndjson(
                    {
                        "message": {"role": "assistant", "content": "partial"},
                        "done": False,
                    }
                ),
            ),
        )

        for provider_id, content_type, wire in fixtures:
            with self.subTest(provider=provider_id):
                result = _aggregate(provider_id, content_type, wire)

                self.assertEqual(result.status, "uncertain")
                self.assertFalse(result.cacheable)

    def test_unknown_or_malformed_event_fails_closed(self) -> None:
        cases = (
            b"".join(
                (
                    _sse(
                        "response.future_event",
                        {"type": "response.future_event", "payload": "unknown"},
                    ),
                    _sse(
                        "response.completed",
                        {"type": "response.completed", "response": {"usage": {}}},
                    ),
                )
            ),
            b"event: response.completed\ndata: {not-json}\n\n",
        )

        for wire in cases:
            with self.subTest(wire=wire[:32]):
                result = _aggregate("openai", "text/event-stream", wire)

                self.assertEqual(result.status, "uncertain")
                self.assertFalse(result.cacheable)

    def test_invalid_usage_is_uncertain_instead_of_estimated(self) -> None:
        wire = _sse(
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "usage": {"input_tokens": -1, "output_tokens": "two"}
                },
            },
        )

        result = _aggregate("openai", "text/event-stream", wire)

        self.assertEqual(result.status, "uncertain")
        self.assertFalse(result.cacheable)

    def test_result_repr_never_contains_response_content(self) -> None:
        private_fragment = "private-response-fragment"
        content_type, wire = _complete_wire("openai", private_fragment)

        result = _aggregate("openai", content_type, wire)

        self.assertEqual(result.text, private_fragment)
        self.assertNotIn(private_fragment, repr(result))


if __name__ == "__main__":
    unittest.main()
