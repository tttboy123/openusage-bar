"""Bounded, fail-closed aggregation of provider-native response streams."""

from __future__ import annotations

import codecs
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal


MAX_STREAM_BYTES = 16 * 1024 * 1024
MAX_STREAM_EVENTS = 4096

_PROVIDERS = frozenset(
    {"anthropic", "deepseek", "ollama", "openai", "openrouter"}
)
_SSE_PROVIDERS = frozenset({"anthropic", "deepseek", "openai", "openrouter"})
_SEAL = object()


@dataclass(frozen=True)
class TokenUsage:
    """A provider-reported token snapshot; values are never estimated."""

    input_tokens: int | None
    output_tokens: int | None

    def __post_init__(self) -> None:
        for value in (self.input_tokens, self.output_tokens):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("invalid token usage")


class StreamResult:
    """Sealed aggregation result whose representation excludes response content."""

    __slots__ = (
        "_canonical_body",
        "_cacheable",
        "_event_count",
        "_has_tool_use",
        "_seal",
        "_status",
        "_text",
        "_usage",
    )

    def __init__(
        self,
        *,
        status: Literal["complete", "interrupted", "uncertain"],
        text: str,
        usage: TokenUsage | None,
        has_tool_use: bool,
        event_count: int,
        canonical_body: bytes,
        cacheable: bool,
        _seal: object,
    ) -> None:
        if _seal is not _SEAL:
            raise TypeError("StreamResult values are created by StreamAggregator")
        self._status = status
        self._text = text
        self._usage = usage
        self._has_tool_use = has_tool_use
        self._event_count = event_count
        self._canonical_body = canonical_body
        self._cacheable = cacheable
        self._seal = _SEAL

    @property
    def status(self) -> str:
        return self._status

    @property
    def text(self) -> str:
        return self._text

    @property
    def usage(self) -> TokenUsage | None:
        return self._usage

    @property
    def has_tool_use(self) -> bool:
        return self._has_tool_use

    @property
    def event_count(self) -> int:
        return self._event_count

    @property
    def canonical_body(self) -> bytes:
        return self._canonical_body

    @property
    def cacheable(self) -> bool:
        return self._cacheable

    def __repr__(self) -> str:
        return (
            "StreamResult("
            f"status={self._status!r}, usage={self._usage!r}, "
            f"has_tool_use={self._has_tool_use!r}, "
            f"event_count={self._event_count!r}, "
            f"cacheable={self._cacheable!r})"
        )


@dataclass(frozen=True, repr=False)
class _StreamProvenance:
    status: str
    cacheable: bool
    has_tool_use: bool
    text_sha256: str
    canonical_body_sha256: str

    def __repr__(self) -> str:
        return (
            "_StreamProvenance("
            f"status={self.status!r}, cacheable={self.cacheable!r}, "
            f"has_tool_use={self.has_tool_use!r})"
        )


def _stream_provenance(value: object) -> _StreamProvenance | None:
    """Return sealed cache-boundary evidence without exposing response content."""

    if type(value) is not StreamResult or value._seal is not _SEAL:
        return None
    return _StreamProvenance(
        status=value._status,
        cacheable=value._cacheable,
        has_tool_use=value._has_tool_use,
        text_sha256=hashlib.sha256(value._text.encode("utf-8")).hexdigest(),
        canonical_body_sha256=hashlib.sha256(value._canonical_body).hexdigest(),
    )


def _nonnegative_int(value: object) -> int | None:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError("invalid usage")
    return value


def _reject_json_constant(value: str) -> object:
    del value
    raise ValueError("non-finite JSON number")


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _strict_json_loads(value: str) -> object:
    return json.loads(
        value,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_reject_duplicate_json_keys,
    )


def _usage_snapshot(
    value: object,
    input_key: str,
    output_key: str,
) -> tuple[int | None, int | None]:
    if value is None:
        return None, None
    if type(value) is not dict:
        raise ValueError("invalid usage")
    input_value = (
        _nonnegative_int(value[input_key]) if input_key in value else None
    )
    output_value = (
        _nonnegative_int(value[output_key]) if output_key in value else None
    )
    return input_value, output_value


def _canonical_body(
    *,
    provider_id: str,
    status: str,
    text: str,
    usage: TokenUsage | None,
    has_tool_use: bool,
) -> bytes:
    payload: dict[str, object] = {
        "has_tool_use": has_tool_use,
        "provider": provider_id,
        "status": status,
        "text": text,
        "usage": None,
    }
    if usage is not None:
        payload["usage"] = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class _AggregationState:
    __slots__ = (
        "event_count",
        "has_tool_use",
        "input_tokens",
        "invalid",
        "output_tokens",
        "terminal",
        "text_parts",
    )

    def __init__(self) -> None:
        self.event_count = 0
        self.has_tool_use = False
        self.input_tokens: int | None = None
        self.invalid = False
        self.output_tokens: int | None = None
        self.terminal = False
        self.text_parts: list[str] = []

    def set_usage(self, input_tokens: int | None, output_tokens: int | None) -> None:
        if input_tokens is not None:
            self.input_tokens = input_tokens
        if output_tokens is not None:
            self.output_tokens = output_tokens


def _append_text(state: _AggregationState, value: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("invalid text delta") from None
    state.text_parts.append(value)


class StreamAggregator:
    """Consume one bounded provider-native stream and derive cache-safe semantics."""

    __slots__ = (
        "_buffer",
        "_content_type",
        "_finished",
        "_oversized",
        "_provider_id",
        "_result",
        "_total_bytes",
    )

    def __init__(self, provider_id: str, content_type: str) -> None:
        if type(provider_id) is not str or provider_id not in _PROVIDERS:
            raise ValueError("unsupported stream provider")
        if type(content_type) is not str:
            raise TypeError("content_type must be a string")
        media_type = content_type.split(";", 1)[0].strip().lower()
        expected = "application/x-ndjson" if provider_id == "ollama" else "text/event-stream"
        if media_type != expected:
            raise ValueError("invalid provider stream content type")
        self._provider_id = provider_id
        self._content_type = media_type
        self._buffer = bytearray()
        self._total_bytes = 0
        self._oversized = False
        self._finished = False
        self._result: StreamResult | None = None

    def feed(self, chunk: bytes) -> None:
        if self._finished:
            raise RuntimeError("stream is already finished")
        if type(chunk) is not bytes:
            raise TypeError("stream chunks must be bytes")
        self._total_bytes += len(chunk)
        if self._total_bytes > MAX_STREAM_BYTES:
            self._oversized = True
            self._buffer.clear()
            return
        if not self._oversized:
            self._buffer.extend(chunk)

    def finish(self, *, interrupted: bool = False) -> StreamResult:
        if type(interrupted) is not bool:
            raise TypeError("interrupted must be a bool")
        if self._finished:
            if self._result is None:
                raise RuntimeError("stream result unavailable")
            return self._result
        self._finished = True

        state = _AggregationState()
        if self._oversized:
            state.invalid = True
        else:
            try:
                decoded = bytes(self._buffer).decode("utf-8", errors="strict")
                if self._provider_id in _SSE_PROVIDERS:
                    self._parse_sse(decoded, state)
                else:
                    self._parse_ndjson(decoded, state)
            except (
                UnicodeDecodeError,
                ValueError,
                TypeError,
                KeyError,
                OverflowError,
                RecursionError,
            ):
                state.invalid = True
            finally:
                self._buffer.clear()

        if interrupted:
            status = "interrupted"
        elif state.terminal and not state.invalid:
            status = "complete"
        else:
            status = "uncertain"
        text = "".join(state.text_parts)
        usage = None
        if state.input_tokens is not None or state.output_tokens is not None:
            usage = TokenUsage(state.input_tokens, state.output_tokens)
        cacheable = status == "complete" and not state.has_tool_use
        canonical = _canonical_body(
            provider_id=self._provider_id,
            status=status,
            text=text,
            usage=usage,
            has_tool_use=state.has_tool_use,
        )
        self._result = StreamResult(
            status=status,
            text=text,
            usage=usage,
            has_tool_use=state.has_tool_use,
            event_count=state.event_count,
            canonical_body=canonical,
            cacheable=cacheable,
            _seal=_SEAL,
        )
        return self._result

    def _parse_sse(self, decoded: str, state: _AggregationState) -> None:
        if "\r" in decoded:
            decoded = decoded.replace("\r\n", "\n")
            if "\r" in decoded:
                raise ValueError("malformed SSE")
        if not decoded or not decoded.endswith("\n\n"):
            raise ValueError("truncated SSE")
        blocks = decoded[:-2].split("\n\n")
        for block in blocks:
            if not block:
                continue
            event_name: str | None = None
            data_lines: list[str] = []
            for line in block.split("\n"):
                if line.startswith(":"):
                    continue
                if ":" in line:
                    field, value = line.split(":", 1)
                    if value.startswith(" "):
                        value = value[1:]
                else:
                    field, value = line, ""
                if field == "event":
                    if event_name is not None or not value:
                        raise ValueError("malformed SSE")
                    event_name = value
                elif field == "data":
                    data_lines.append(value)
                elif field not in {"id", "retry"}:
                    raise ValueError("malformed SSE")
            if not data_lines:
                continue
            state.event_count += 1
            if state.event_count > MAX_STREAM_EVENTS or state.terminal:
                raise ValueError("invalid stream event count")
            data = "\n".join(data_lines)
            if self._provider_id in {"deepseek", "openrouter"} and data == "[DONE]":
                state.terminal = True
                continue
            payload = _strict_json_loads(data)
            if type(payload) is not dict:
                raise ValueError("invalid stream event")
            if self._provider_id == "openai":
                self._handle_openai(event_name, payload, state)
            elif self._provider_id == "anthropic":
                self._handle_anthropic(event_name, payload, state)
            else:
                self._handle_openai_compatible(event_name, payload, state)

    def _parse_ndjson(self, decoded: str, state: _AggregationState) -> None:
        if "\r" in decoded:
            decoded = decoded.replace("\r\n", "\n")
            if "\r" in decoded:
                raise ValueError("malformed NDJSON")
        if not decoded or not decoded.endswith("\n"):
            raise ValueError("truncated NDJSON")
        for line in decoded.splitlines():
            if not line:
                continue
            state.event_count += 1
            if state.event_count > MAX_STREAM_EVENTS or state.terminal:
                raise ValueError("invalid stream event count")
            payload = _strict_json_loads(line)
            if type(payload) is not dict:
                raise ValueError("invalid stream event")
            self._handle_ollama(payload, state)

    @staticmethod
    def _event_type(event_name: str | None, payload: dict[str, Any]) -> str:
        payload_type = payload.get("type")
        if type(payload_type) is not str or not payload_type:
            raise ValueError("missing stream event type")
        if event_name not in (None, "message", payload_type):
            raise ValueError("mismatched stream event type")
        return payload_type

    def _handle_openai(
        self,
        event_name: str | None,
        payload: dict[str, Any],
        state: _AggregationState,
    ) -> None:
        event_type = self._event_type(event_name, payload)
        if event_type == "response.output_text.delta":
            delta = payload.get("delta")
            if type(delta) is not str:
                raise ValueError("invalid text delta")
            _append_text(state, delta)
            return
        if event_type == "response.output_item.added":
            item = payload.get("item")
            if type(item) is not dict:
                raise ValueError("invalid output item")
            item_type = item.get("type")
            if type(item_type) is not str:
                raise ValueError("invalid output item")
            if item_type.endswith("_call") or item_type in {"function_call", "custom_tool_call"}:
                state.has_tool_use = True
            return
        if event_type == "response.completed":
            response = payload.get("response")
            if type(response) is not dict:
                raise ValueError("invalid completed response")
            input_tokens, output_tokens = _usage_snapshot(
                response.get("usage"), "input_tokens", "output_tokens"
            )
            state.set_usage(input_tokens, output_tokens)
            output = response.get("output", [])
            if type(output) is not list:
                raise ValueError("invalid completed response")
            for item in output:
                if type(item) is not dict:
                    raise ValueError("invalid completed response")
                item_type = item.get("type")
                if type(item_type) is str and (
                    item_type.endswith("_call")
                    or item_type in {"function_call", "custom_tool_call"}
                ):
                    state.has_tool_use = True
            state.terminal = True
            return
        if event_type.startswith("response.function_call_arguments."):
            state.has_tool_use = True
            return
        if event_type in {
            "response.created",
            "response.in_progress",
            "response.output_item.done",
            "response.content_part.added",
            "response.content_part.done",
            "response.output_text.done",
            "response.refusal.delta",
            "response.refusal.done",
        }:
            return
        raise ValueError("unknown OpenAI stream event")

    def _handle_anthropic(
        self,
        event_name: str | None,
        payload: dict[str, Any],
        state: _AggregationState,
    ) -> None:
        event_type = self._event_type(event_name, payload)
        if event_type == "message_start":
            message = payload.get("message")
            if type(message) is not dict:
                raise ValueError("invalid message start")
            input_tokens, output_tokens = _usage_snapshot(
                message.get("usage"), "input_tokens", "output_tokens"
            )
            state.set_usage(input_tokens, output_tokens)
            return
        if event_type == "content_block_start":
            content_block = payload.get("content_block")
            if type(content_block) is not dict:
                raise ValueError("invalid content block")
            block_type = content_block.get("type")
            if type(block_type) is not str:
                raise ValueError("invalid content block")
            if block_type == "tool_use":
                state.has_tool_use = True
            return
        if event_type == "content_block_delta":
            delta = payload.get("delta")
            if type(delta) is not dict or type(delta.get("type")) is not str:
                raise ValueError("invalid content delta")
            delta_type = delta["type"]
            if delta_type == "text_delta":
                text = delta.get("text")
                if type(text) is not str:
                    raise ValueError("invalid text delta")
                _append_text(state, text)
            elif delta_type == "input_json_delta":
                state.has_tool_use = True
            elif delta_type not in {"thinking_delta", "signature_delta"}:
                raise ValueError("unknown content delta")
            return
        if event_type == "message_delta":
            input_tokens, output_tokens = _usage_snapshot(
                payload.get("usage"), "input_tokens", "output_tokens"
            )
            state.set_usage(input_tokens, output_tokens)
            return
        if event_type == "message_stop":
            state.terminal = True
            return
        if event_type in {"content_block_stop", "ping"}:
            return
        raise ValueError("unknown Anthropic stream event")

    @staticmethod
    def _handle_openai_compatible(
        event_name: str | None,
        payload: dict[str, Any],
        state: _AggregationState,
    ) -> None:
        if event_name not in (None, "message"):
            raise ValueError("invalid compatible stream event")
        if "error" in payload:
            raise ValueError("upstream error event")
        if "usage" in payload:
            input_tokens, output_tokens = _usage_snapshot(
                payload["usage"], "prompt_tokens", "completion_tokens"
            )
            state.set_usage(input_tokens, output_tokens)
        choices = payload.get("choices")
        if type(choices) is not list:
            raise ValueError("invalid compatible stream event")
        for choice in choices:
            if type(choice) is not dict:
                raise ValueError("invalid compatible stream choice")
            delta = choice.get("delta")
            if delta is None:
                continue
            if type(delta) is not dict:
                raise ValueError("invalid compatible stream delta")
            content = delta.get("content")
            if content is not None:
                if type(content) is not str:
                    raise ValueError("invalid compatible stream text")
                _append_text(state, content)
            if delta.get("tool_calls") or delta.get("function_call"):
                state.has_tool_use = True

    @staticmethod
    def _handle_ollama(payload: dict[str, Any], state: _AggregationState) -> None:
        done = payload.get("done")
        if type(done) is not bool:
            raise ValueError("invalid Ollama completion marker")
        message = payload.get("message")
        if message is not None:
            if type(message) is not dict:
                raise ValueError("invalid Ollama message")
            content = message.get("content")
            if content is not None:
                if type(content) is not str:
                    raise ValueError("invalid Ollama text")
                _append_text(state, content)
            if message.get("tool_calls"):
                state.has_tool_use = True
        if payload.get("tool_calls"):
            state.has_tool_use = True
        input_tokens = (
            _nonnegative_int(payload["prompt_eval_count"])
            if "prompt_eval_count" in payload
            else None
        )
        output_tokens = (
            _nonnegative_int(payload["eval_count"])
            if "eval_count" in payload
            else None
        )
        state.set_usage(input_tokens, output_tokens)
        if done:
            state.terminal = True


_UPDATE_SEAL = object()


class ProviderStreamUpdate:
    """One normalized incremental observation with a content-free repr."""

    __slots__ = ("_kind", "_seal", "_text")

    def __init__(self, kind: Literal["text", "tool"], text: str | None, *, _seal: object) -> None:
        if _seal is not _UPDATE_SEAL or kind not in {"text", "tool"}:
            raise TypeError("ProviderStreamUpdate values are internally created")
        if kind == "text" and type(text) is not str:
            raise TypeError("invalid Provider stream update")
        if kind == "tool" and text is not None:
            raise TypeError("invalid Provider stream update")
        self._kind = kind
        self._text = text
        self._seal = _UPDATE_SEAL

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def text(self) -> str | None:
        return self._text

    def __repr__(self) -> str:
        return "ProviderStreamUpdate(<sanitized>)"


class IncrementalStreamParser:
    """Incrementally normalize one provider-native SSE or NDJSON stream.

    UTF-8 decoding and line framing are retained across arbitrary byte
    boundaries.  Malformed input is recorded as an uncertain completion
    instead of exposing provider-native data through an exception message.
    """

    __slots__ = (
        "_content_type",
        "_decoder",
        "_finished",
        "_handler",
        "_invalid",
        "_provider_id",
        "_result",
        "_sse_lines",
        "_state",
        "_text_buffer",
        "_total_bytes",
    )

    def __init__(self, provider_id: str, content_type: str) -> None:
        # Reuse the established provider/content-type validation and event
        # handlers so buffered and live paths retain one semantic vocabulary.
        handler = StreamAggregator(provider_id, content_type)
        self._provider_id = provider_id
        self._content_type = handler._content_type
        self._handler = handler
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._text_buffer = ""
        self._sse_lines: list[str] = []
        self._state = _AggregationState()
        self._total_bytes = 0
        self._invalid = False
        self._finished = False
        self._result: StreamResult | None = None

    @property
    def terminal(self) -> bool:
        return self._state.terminal and not self._invalid

    @property
    def invalid(self) -> bool:
        return self._invalid

    @property
    def output_observed(self) -> bool:
        return bool(self._state.text_parts)

    @property
    def tool_observed(self) -> bool:
        return self._state.has_tool_use

    def feed(self, chunk: bytes) -> tuple[ProviderStreamUpdate, ...]:
        if self._finished:
            raise RuntimeError("stream is already finished")
        if self._invalid:
            return ()
        if type(chunk) is not bytes:
            self._invalid = True
            return ()
        self._total_bytes += len(chunk)
        if self._total_bytes > MAX_STREAM_BYTES:
            self._invalid = True
            return ()
        try:
            decoded = self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError:
            self._invalid = True
            return ()
        return self._consume(decoded)

    def finish(self, *, interrupted: bool = False) -> StreamResult:
        if type(interrupted) is not bool:
            raise TypeError("interrupted must be a bool")
        if self._finished:
            if self._result is None:
                raise RuntimeError("stream result unavailable")
            return self._result
        self._finished = True
        if not self._invalid:
            try:
                tail = self._decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                self._invalid = True
            else:
                if tail:
                    self._consume(tail)
                if self._text_buffer or self._sse_lines:
                    self._invalid = True

        if interrupted:
            status = "interrupted"
        elif self._state.terminal and not self._invalid:
            status = "complete"
        else:
            status = "uncertain"
        text = "".join(self._state.text_parts)
        usage = None
        if (
            self._state.input_tokens is not None
            or self._state.output_tokens is not None
        ):
            usage = TokenUsage(
                self._state.input_tokens,
                self._state.output_tokens,
            )
        cacheable = status == "complete" and not self._state.has_tool_use
        canonical = _canonical_body(
            provider_id=self._provider_id,
            status=status,
            text=text,
            usage=usage,
            has_tool_use=self._state.has_tool_use,
        )
        self._result = StreamResult(
            status=status,
            text=text,
            usage=usage,
            has_tool_use=self._state.has_tool_use,
            event_count=self._state.event_count,
            canonical_body=canonical,
            cacheable=cacheable,
            _seal=_SEAL,
        )
        return self._result

    def _consume(self, decoded: str) -> tuple[ProviderStreamUpdate, ...]:
        if self._invalid or not decoded:
            return ()
        self._text_buffer += decoded
        updates: list[ProviderStreamUpdate] = []
        while "\n" in self._text_buffer and not self._invalid:
            line, self._text_buffer = self._text_buffer.split("\n", 1)
            if line.endswith("\r"):
                line = line[:-1]
            if "\r" in line:
                self._invalid = True
                break
            try:
                if self._provider_id in _SSE_PROVIDERS:
                    if line:
                        self._sse_lines.append(line)
                    else:
                        updates.extend(self._consume_sse_block())
                elif line:
                    updates.extend(self._consume_ndjson_line(line))
            except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
                self._invalid = True
        return tuple(updates)

    def _consume_sse_block(self) -> tuple[ProviderStreamUpdate, ...]:
        lines = self._sse_lines
        self._sse_lines = []
        if not lines:
            return ()
        event_name: str | None = None
        data_lines: list[str] = []
        for line in lines:
            if line.startswith(":"):
                continue
            if ":" in line:
                field, value = line.split(":", 1)
                if value.startswith(" "):
                    value = value[1:]
            else:
                field, value = line, ""
            if field == "event":
                if event_name is not None or not value:
                    raise ValueError("invalid provider stream")
                event_name = value
            elif field == "data":
                data_lines.append(value)
            elif field not in {"id", "retry"}:
                raise ValueError("invalid provider stream")
        if not data_lines:
            return ()
        if self._state.terminal:
            raise ValueError("invalid provider stream")
        self._state.event_count += 1
        if self._state.event_count > MAX_STREAM_EVENTS:
            raise ValueError("invalid provider stream")
        data = "\n".join(data_lines)
        if self._provider_id in {"deepseek", "openrouter"} and data == "[DONE]":
            self._state.terminal = True
            return ()
        payload = _strict_json_loads(data)
        if type(payload) is not dict:
            raise ValueError("invalid provider stream")
        return self._handle_event(event_name, payload)

    def _consume_ndjson_line(self, line: str) -> tuple[ProviderStreamUpdate, ...]:
        if self._state.terminal:
            raise ValueError("invalid provider stream")
        self._state.event_count += 1
        if self._state.event_count > MAX_STREAM_EVENTS:
            raise ValueError("invalid provider stream")
        payload = _strict_json_loads(line)
        if type(payload) is not dict:
            raise ValueError("invalid provider stream")
        return self._handle_event(None, payload)

    def _handle_event(
        self,
        event_name: str | None,
        payload: dict[str, Any],
    ) -> tuple[ProviderStreamUpdate, ...]:
        before_text = len(self._state.text_parts)
        before_tool = self._state.has_tool_use
        if self._provider_id == "openai":
            self._handler._handle_openai(event_name, payload, self._state)
        elif self._provider_id == "anthropic":
            self._handler._handle_anthropic(event_name, payload, self._state)
        elif self._provider_id in {"deepseek", "openrouter"}:
            self._handler._handle_openai_compatible(
                event_name,
                payload,
                self._state,
            )
        else:
            self._handler._handle_ollama(payload, self._state)

        updates = [
            ProviderStreamUpdate("text", text, _seal=_UPDATE_SEAL)
            for text in self._state.text_parts[before_text:]
        ]
        if not before_tool and self._state.has_tool_use:
            updates.append(
                ProviderStreamUpdate("tool", None, _seal=_UPDATE_SEAL)
            )
        return tuple(updates)

    def __repr__(self) -> str:
        return "IncrementalStreamParser(<sanitized>)"


__all__ = [
    "IncrementalStreamParser",
    "ProviderStreamUpdate",
    "StreamAggregator",
    "StreamResult",
    "TokenUsage",
]
