"""Closed Gateway response boundary and Gateway-native event validation.

This module deliberately uses only the standard library.  It accepts a value
from the in-process runtime only when the value is inside the frozen Gateway
response v1 contract, then rebuilds it from contract fields so unknown
provider-native material cannot cross the router boundary.

Both bounded responses and live event streams are rebuilt from closed v1
contract fields.  Live streams stay pull-driven: validating one event never
pre-consumes the next Provider event.
"""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from collections.abc import Iterable, Iterator


API_VERSION = "gateway.openusage/v1"
MAX_OUTPUT_TEXT_BYTES = 16 * 1024 * 1024
MAX_DELTA_TEXT_BYTES = 64 * 1024
MAX_SEQUENCE = 4095

_PROVIDERS = frozenset(
    {"openai", "anthropic", "deepseek", "openrouter", "ollama"}
)
_STATUSES = frozenset({"complete", "interrupted", "uncertain"})
_CACHE_PLAIN = frozenset({"disabled", "miss", "exact_hit"})
_CACHE_WITH_REASON = frozenset(
    {"bypass", "store_skipped", "store_failed"}
)
_FALLBACK_ACTIONS = frozenset(
    {"none", "retry", "queue", "fail", "degrade_to_cheap"}
)
_STABLE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ITERATOR_SEAL = object()
_VALIDATED_ITERATOR_SEAL = object()
_TERMINAL_EVENT_TYPES = frozenset(
    {
        "gateway.response.completed",
        "gateway.response.interrupted",
        "gateway.response.uncertain",
        "gateway.error",
    }
)
_TERMINAL_STATUS = {
    "gateway.response.completed": "complete",
    "gateway.response.interrupted": "interrupted",
    "gateway.response.uncertain": "uncertain",
}


def validate_gateway_payload(value: object) -> dict[str, object] | None:
    """Return a plain closed-contract copy, or ``None`` on any violation.

    Validation never interpolates or represents the rejected value.  This is
    important because the value may contain credentials, prompts, raw chunks,
    provider identifiers, or hostile objects supplied by an in-process proxy.
    """

    try:
        if type(value) is not dict:
            return None
        if value.get("apiVersion") != API_VERSION:
            return None
        object_type = value.get("object")
        if object_type == "gateway.response":
            return _validated_response(value)
        if object_type == "gateway.error":
            return _validated_error_envelope(value)
    except Exception:
        # A concurrently-mutated builtin container must fail closed just like
        # any other malformed producer value.
        return None
    return None


def sanitized_gateway_error(
    code: str = "internal_error",
    *,
    message: str = "Request could not be completed.",
    retryable: bool = True,
    fallback: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a known-safe versioned error without reflecting an exception."""

    if _stable_code(code) is None:
        code = "internal_error"
    if _bounded_text(message, 256) is None:
        message = "Request could not be completed."
    if type(retryable) is not bool:
        retryable = True
    result: dict[str, object] = {
        "apiVersion": API_VERSION,
        "object": "gateway.error",
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        },
    }
    if fallback is not None:
        safe_fallback = _validated_fallback(fallback)
        if safe_fallback is not None:
            result["fallback"] = safe_fallback
    # All fields above are locally constructed and independently bounded.
    return result


class GatewayEventIterator(Iterator[dict[str, object]]):
    """Small closeable iterator with a content-free public representation."""

    __slots__ = ("_events", "_index", "_seal")

    def __init__(
        self,
        events: tuple[dict[str, object], ...],
        *,
        _seal: object,
    ) -> None:
        if _seal is not _ITERATOR_SEAL:
            raise TypeError("GatewayEventIterator values are internally created")
        self._events = events
        self._index = 0
        self._seal = _ITERATOR_SEAL

    def __iter__(self) -> GatewayEventIterator:
        return self

    def __next__(self) -> dict[str, object]:
        if self._seal is not _ITERATOR_SEAL:
            raise RuntimeError("invalid Gateway event iterator")
        if type(self._events) is not tuple or type(self._index) is not int:
            raise RuntimeError("invalid Gateway event iterator")
        if self._index >= len(self._events):
            raise StopIteration
        event = self._events[self._index]
        self._index += 1
        # Events are built solely from a validated response.  Yielding a copy
        # prevents a consumer from changing the authoritative remaining data.
        return copy.deepcopy(event)

    def close(self) -> None:
        if self._seal is _ITERATOR_SEAL and type(self._events) is tuple:
            self._index = len(self._events)

    def __length_hint__(self) -> int:
        if (
            self._seal is not _ITERATOR_SEAL
            or type(self._events) is not tuple
            or type(self._index) is not int
        ):
            return 0
        return max(0, len(self._events) - self._index)

    def __repr__(self) -> str:
        if getattr(self, "_seal", None) is not _ITERATOR_SEAL:
            return "GatewayEventIterator(<invalid>)"
        return "GatewayEventIterator(<sanitized>)"


class ValidatedGatewayEventIterator(Iterator[dict[str, object]]):
    """Validate an untrusted live event source one pull at a time.

    A terminal event is returned before the source is resumed.  The following
    pull resumes the source exactly once, which preserves the runtime's
    post-terminal cache-admission boundary.  Closing after a failed terminal
    write instead propagates ``GeneratorExit`` before that admission point.
    """

    __slots__ = (
        "_accepted_model",
        "_accepted_provider",
        "_closed",
        "_expected_sequence",
        "_iterator",
        "_output_text_bytes",
        "_seal",
        "_source",
        "_started",
        "_terminal_yielded",
    )

    def __init__(
        self,
        source: Iterable[dict[str, object]],
        iterator: Iterator[dict[str, object]],
        *,
        accepted_provider: str | None,
        accepted_model: str | None,
        _seal: object,
    ) -> None:
        if _seal is not _VALIDATED_ITERATOR_SEAL:
            raise TypeError(
                "ValidatedGatewayEventIterator values are internally created"
            )
        self._source = source
        self._iterator = iterator
        self._accepted_provider = accepted_provider
        self._accepted_model = accepted_model
        self._expected_sequence = 0
        self._output_text_bytes = 0
        self._started = False
        self._terminal_yielded = False
        self._closed = False
        self._seal = _VALIDATED_ITERATOR_SEAL

    def __iter__(self) -> ValidatedGatewayEventIterator:
        return self

    def __next__(self) -> dict[str, object]:
        if self._seal is not _VALIDATED_ITERATOR_SEAL:
            raise RuntimeError("invalid Gateway event validator")
        if self._closed:
            raise StopIteration

        if self._terminal_yielded:
            # Resume only after the terminal event has been accepted by the
            # consumer.  A correct source stops here; any post-terminal value
            # or exception is suppressed and the source is closed.
            try:
                next(self._iterator)
            except StopIteration:
                pass
            except Exception:
                pass
            self._close_source()
            raise StopIteration

        try:
            candidate = next(self._iterator)
        except StopIteration:
            return self._fail_closed_event()
        except Exception:
            return self._fail_closed_event()

        event = _validated_sse_event(
            candidate,
            expected_sequence=self._expected_sequence,
            accepted_provider=self._accepted_provider,
            accepted_model=self._accepted_model,
        )
        if event is None:
            return self._fail_closed_event()

        event_type = event["type"]
        if self._expected_sequence == 0:
            if event_type == "gateway.response.started":
                self._started = True
            elif event_type != "gateway.error":
                return self._fail_closed_event()
        elif event_type == "gateway.response.started" or not self._started:
            return self._fail_closed_event()

        if event_type == "gateway.output_text.delta":
            encoded_size = len(event["text"].encode("utf-8"))
            if self._output_text_bytes + encoded_size > MAX_OUTPUT_TEXT_BYTES:
                return self._fail_closed_event()
            self._output_text_bytes += encoded_size

        is_terminal = event_type in _TERMINAL_EVENT_TYPES
        if not is_terminal and self._expected_sequence >= MAX_SEQUENCE:
            # Reserve the final legal sequence value for a local terminal
            # error instead of emitting a stream that cannot terminate.
            return self._fail_closed_event()

        self._expected_sequence += 1
        if is_terminal:
            self._terminal_yielded = True
        return event

    def close(self) -> None:
        self._close_source()

    def __length_hint__(self) -> int:
        return 0

    def __repr__(self) -> str:
        if getattr(self, "_seal", None) is not _VALIDATED_ITERATOR_SEAL:
            return "ValidatedGatewayEventIterator(<invalid>)"
        return "ValidatedGatewayEventIterator(<sanitized>)"

    def _fail_closed_event(self) -> dict[str, object]:
        sequence = self._expected_sequence
        self._close_source()
        if sequence > MAX_SEQUENCE:
            raise StopIteration
        return {
            "apiVersion": API_VERSION,
            "type": "gateway.error",
            "sequence": sequence,
            "error": {
                "code": "internal_error",
                "message": "Request could not be completed.",
                "retryable": True,
            },
        }

    def _close_source(self) -> None:
        if self._closed:
            return
        self._closed = True
        target: object = self._source
        try:
            close = getattr(target, "close", None)
        except Exception:
            close = None
        if not callable(close) and self._iterator is not target:
            try:
                close = getattr(self._iterator, "close", None)
            except Exception:
                close = None
        if callable(close):
            try:
                close()
            except Exception:
                pass


def validated_gateway_event_stream(
    source: object,
    *,
    accepted_provider: object = None,
    accepted_model: object = None,
) -> ValidatedGatewayEventIterator | None:
    """Wrap one untrusted live source without pulling its first event."""

    if type(source) is dict or isinstance(
        source,
        (bytes, bytearray, memoryview, str),
    ):
        _close_unvalidated_source(source)
        return None
    try:
        iterator = iter(source)
    except Exception:
        _close_unvalidated_source(source)
        return None
    return ValidatedGatewayEventIterator(
        source,
        iterator,
        accepted_provider=_provider(accepted_provider),
        accepted_model=_model(accepted_model),
        _seal=_VALIDATED_ITERATOR_SEAL,
    )


def gateway_events(
    value: object,
    *,
    accepted_provider: object = None,
    accepted_model: object = None,
) -> GatewayEventIterator | None:
    """Convert one validated bounded result into Gateway-native events.

    The started event reports the accepted primary source when it is valid;
    terminal events report the effective source from the bounded result.
    """

    payload = validate_gateway_payload(value)
    if payload is None:
        return None
    events: list[dict[str, object]] = []
    if payload["object"] == "gateway.error":
        event: dict[str, object] = {
            "apiVersion": API_VERSION,
            "type": "gateway.error",
            "sequence": 0,
            "error": copy.deepcopy(payload["error"]),
        }
        if "fallback" in payload:
            event["fallback"] = copy.deepcopy(payload["fallback"])
        events.append(event)
        return GatewayEventIterator(tuple(events), _seal=_ITERATOR_SEAL)

    primary_provider = _provider(accepted_provider) or payload["provider"]
    primary_model = _model(accepted_model) or payload["model"]
    events.append(
        {
            "apiVersion": API_VERSION,
            "type": "gateway.response.started",
            "sequence": 0,
            "provider": primary_provider,
            "model": primary_model,
            "stream": True,
        }
    )

    cache = payload["cache"]
    if cache["outcome"] in {"exact_hit", "prefix_hint"}:
        _append_event(events, "gateway.cache.event", cache=cache)

    fallback = payload["fallback"]
    if fallback["attempted"] is True:
        _append_event(events, "gateway.fallback.event", fallback=fallback)

    tool = payload["tool"]
    if tool["observed"] is True:
        _append_event(
            events,
            "gateway.tool.observed",
            tool={"observed": True},
        )

    for text in _delta_text_parts(payload["outputText"]):
        _append_event(events, "gateway.output_text.delta", text=text)

    status = payload["status"]
    terminal_type = {
        "complete": "gateway.response.completed",
        "interrupted": "gateway.response.interrupted",
        "uncertain": "gateway.response.uncertain",
    }[status]
    _append_event(
        events,
        terminal_type,
        provider=payload["provider"],
        model=payload["model"],
        status=status,
        usage=payload["usage"],
        tool=tool,
        cache=cache,
        fallback=fallback,
    )
    return GatewayEventIterator(tuple(events), _seal=_ITERATOR_SEAL)


def encode_sse_event(event: object) -> bytes:
    """Encode one internally-built event with wire name equal to ``type``."""

    if type(event) is not dict:
        raise ValueError("invalid Gateway event")
    event_type = event.get("type")
    if (
        type(event_type) is not str
        or not event_type.startswith("gateway.")
        or "\n" in event_type
        or "\r" in event_type
    ):
        raise ValueError("invalid Gateway event")
    encoded = json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return (
        b"event: "
        + event_type.encode("ascii")
        + b"\ndata: "
        + encoded
        + b"\n\n"
    )


def _validated_sse_event(
    value: object,
    *,
    expected_sequence: int,
    accepted_provider: str | None,
    accepted_model: str | None,
) -> dict[str, object] | None:
    """Return a closed plain copy of one v1 event, or fail closed."""

    try:
        if type(value) is not dict:
            return None
        if value.get("apiVersion") != API_VERSION:
            return None
        event_type = value.get("type")
        sequence = value.get("sequence")
        if (
            type(event_type) is not str
            or type(sequence) is not int
            or sequence != expected_sequence
            or not 0 <= sequence <= MAX_SEQUENCE
        ):
            return None

        base: dict[str, object] = {
            "apiVersion": API_VERSION,
            "type": event_type,
            "sequence": sequence,
        }
        if event_type == "gateway.response.started":
            if set(value) != {
                "apiVersion",
                "type",
                "sequence",
                "provider",
                "model",
                "stream",
            }:
                return None
            provider = _provider(value.get("provider"))
            model = _model(value.get("model"))
            if (
                provider is None
                or model is None
                or value.get("stream") is not True
                or (
                    accepted_provider is not None
                    and provider != accepted_provider
                )
                or accepted_model is not None
                and model != accepted_model
            ):
                return None
            base.update(
                {"provider": provider, "model": model, "stream": True}
            )
            return base

        if event_type == "gateway.output_text.delta":
            if set(value) != {"apiVersion", "type", "sequence", "text"}:
                return None
            text = value.get("text")
            if type(text) is not str or not _utf8_within(
                text,
                MAX_DELTA_TEXT_BYTES,
            ):
                return None
            base["text"] = text
            return base

        if event_type == "gateway.usage.snapshot":
            if set(value) != {"apiVersion", "type", "sequence", "usage"}:
                return None
            usage = _validated_usage(value.get("usage"))
            if type(usage) is not dict:
                return None
            base["usage"] = usage
            return base

        if event_type == "gateway.tool.observed":
            if set(value) != {"apiVersion", "type", "sequence", "tool"}:
                return None
            tool = _validated_tool(value.get("tool"))
            if tool is None or tool["observed"] is not True:
                return None
            base["tool"] = tool
            return base

        if event_type == "gateway.cache.event":
            if set(value) != {"apiVersion", "type", "sequence", "cache"}:
                return None
            cache = _validated_cache(value.get("cache"))
            if cache is None:
                return None
            base["cache"] = cache
            return base

        if event_type == "gateway.fallback.event":
            if set(value) != {
                "apiVersion",
                "type",
                "sequence",
                "fallback",
            }:
                return None
            fallback = _validated_fallback(value.get("fallback"))
            if fallback is None:
                return None
            base["fallback"] = fallback
            return base

        if event_type in _TERMINAL_STATUS:
            if set(value) != {
                "apiVersion",
                "type",
                "sequence",
                "provider",
                "model",
                "status",
                "usage",
                "tool",
                "cache",
                "fallback",
            }:
                return None
            provider = _provider(value.get("provider"))
            model = _model(value.get("model"))
            status = value.get("status")
            usage = _validated_usage(value.get("usage"))
            tool = _validated_tool(value.get("tool"))
            cache = _validated_cache(value.get("cache"))
            fallback = _validated_fallback(value.get("fallback"))
            if (
                provider is None
                or model is None
                or status != _TERMINAL_STATUS[event_type]
                or usage is _INVALID
                or tool is None
                or cache is None
                or fallback is None
                or not _valid_response_state(status, tool, cache, fallback)
            ):
                return None
            base.update(
                {
                    "provider": provider,
                    "model": model,
                    "status": status,
                    "usage": usage,
                    "tool": tool,
                    "cache": cache,
                    "fallback": fallback,
                }
            )
            return base

        if event_type == "gateway.error":
            if not set(value) in (
                {"apiVersion", "type", "sequence", "error"},
                {
                    "apiVersion",
                    "type",
                    "sequence",
                    "error",
                    "fallback",
                },
            ):
                return None
            error = _validated_error_detail(value.get("error"))
            if error is None:
                return None
            base["error"] = error
            if "fallback" in value:
                fallback = _validated_fallback(value.get("fallback"))
                if fallback is None:
                    return None
                base["fallback"] = fallback
            return base
    except Exception:
        return None
    return None


def _close_unvalidated_source(source: object) -> None:
    try:
        close = getattr(source, "close", None)
    except Exception:
        return
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _validated_response(value: dict[object, object]) -> dict[str, object] | None:
    required = {
        "apiVersion",
        "object",
        "provider",
        "model",
        "stream",
        "status",
        "outputText",
        "usage",
        "tool",
        "cache",
        "fallback",
    }
    if set(value) != required:
        return None
    provider = _provider(value.get("provider"))
    model = _model(value.get("model"))
    stream = value.get("stream")
    status = value.get("status")
    output_text = value.get("outputText")
    usage = _validated_usage(value.get("usage"))
    tool = _validated_tool(value.get("tool"))
    cache = _validated_cache(value.get("cache"))
    fallback = _validated_fallback(value.get("fallback"))
    if (
        provider is None
        or model is None
        or type(stream) is not bool
        or type(status) is not str
        or status not in _STATUSES
        or type(output_text) is not str
        or not _utf8_within(output_text, MAX_OUTPUT_TEXT_BYTES)
        or usage is _INVALID
        or tool is None
        or cache is None
        or fallback is None
    ):
        return None
    if not _valid_response_state(status, tool, cache, fallback):
        return None
    return {
        "apiVersion": API_VERSION,
        "object": "gateway.response",
        "provider": provider,
        "model": model,
        "stream": stream,
        "status": status,
        "outputText": output_text,
        "usage": usage,
        "tool": tool,
        "cache": cache,
        "fallback": fallback,
    }


def _validated_error_envelope(
    value: dict[object, object],
) -> dict[str, object] | None:
    if not set(value) in (
        {"apiVersion", "object", "error"},
        {"apiVersion", "object", "error", "fallback"},
    ):
        return None
    error = _validated_error_detail(value.get("error"))
    if error is None:
        return None
    result: dict[str, object] = {
        "apiVersion": API_VERSION,
        "object": "gateway.error",
        "error": error,
    }
    if "fallback" in value:
        fallback = _validated_fallback(value.get("fallback"))
        if fallback is None:
            return None
        result["fallback"] = fallback
    return result


def _valid_response_state(
    status: object,
    tool: dict[str, bool],
    cache: dict[str, object],
    fallback: dict[str, object],
) -> bool:
    if cache["outcome"] == "exact_hit" and (
        status != "complete"
        or tool["observed"] is not False
        or fallback["attempted"] is not False
        or fallback["attemptCount"] != 0
        or fallback["finalAction"] != "none"
    ):
        return False
    if cache["outcome"] == "prefix_hint" and fallback["attemptCount"] < 1:
        return False
    if tool["observed"] is True and cache["outcome"] == "exact_hit":
        return False
    return True


def _validated_error_detail(value: object) -> dict[str, object] | None:
    if type(value) is not dict or set(value) != {"code", "message", "retryable"}:
        return None
    code = _stable_code(value.get("code"))
    message = _bounded_text(value.get("message"), 256)
    retryable = value.get("retryable")
    if code is None or message is None or type(retryable) is not bool:
        return None
    return {"code": code, "message": message, "retryable": retryable}


_INVALID = object()


def _validated_usage(value: object) -> dict[str, object] | None | object:
    if value is None:
        return None
    if type(value) is not dict or set(value) != {"inputTokens", "outputTokens"}:
        return _INVALID
    result: dict[str, object] = {}
    for key in ("inputTokens", "outputTokens"):
        item = value.get(key)
        if item is not None and (
            type(item) is not int or not 0 <= item <= 2**63 - 1
        ):
            return _INVALID
        result[key] = item
    return result


def _validated_tool(value: object) -> dict[str, bool] | None:
    if (
        type(value) is not dict
        or set(value) != {"observed"}
        or type(value.get("observed")) is not bool
    ):
        return None
    return {"observed": value["observed"]}


def _validated_cache(value: object) -> dict[str, object] | None:
    if type(value) is not dict:
        return None
    outcome = value.get("outcome")
    if type(outcome) is not str:
        return None
    if outcome in _CACHE_PLAIN:
        return {"outcome": outcome} if set(value) == {"outcome"} else None
    if outcome in _CACHE_WITH_REASON:
        reason = _stable_code(value.get("reason"))
        if set(value) != {"outcome", "reason"} or reason is None:
            return None
        return {"outcome": outcome, "reason": reason}
    if outcome == "prefix_hint":
        depth = value.get("hintDepth")
        if (
            set(value) != {"outcome", "hintDepth"}
            or type(depth) is not int
            or not 1 <= depth <= 64
        ):
            return None
        return {"outcome": outcome, "hintDepth": depth}
    return None


def _validated_fallback(value: object) -> dict[str, object] | None:
    if type(value) is not dict:
        return None
    required = {"attempted", "attemptCount", "finalAction"}
    optional = {"errorCode", "retryable"}
    if not required.issubset(value) or not set(value).issubset(required | optional):
        return None
    attempted = value.get("attempted")
    count = value.get("attemptCount")
    action = value.get("finalAction")
    if (
        type(attempted) is not bool
        or type(count) is not int
        or not 0 <= count <= 2
        or type(action) is not str
        or action not in _FALLBACK_ACTIONS
        or ("errorCode" in value) != ("retryable" in value)
    ):
        return None
    if attempted is False and (count > 1 or action != "none"):
        return None
    if attempted is True and action == "none":
        return None
    if action in {"retry", "degrade_to_cheap"} and count != 2:
        return None
    result: dict[str, object] = {
        "attempted": attempted,
        "attemptCount": count,
        "finalAction": action,
    }
    if "errorCode" in value:
        code = _stable_code(value.get("errorCode"))
        retryable = value.get("retryable")
        if code is None or type(retryable) is not bool:
            return None
        result["errorCode"] = code
        result["retryable"] = retryable
    return result


def _append_event(
    events: list[dict[str, object]],
    event_type: str,
    **fields: object,
) -> None:
    sequence = len(events)
    if sequence > MAX_SEQUENCE:
        raise ValueError("Gateway event limit exceeded")
    event: dict[str, object] = {
        "apiVersion": API_VERSION,
        "type": event_type,
        "sequence": sequence,
    }
    event.update(copy.deepcopy(fields))
    events.append(event)


def _delta_text_parts(text: object) -> tuple[str, ...]:
    if type(text) is not str:
        raise ValueError("invalid Gateway output text")
    if text == "":
        return ("",)
    result: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for character in text:
        encoded = character.encode("utf-8")
        if current and current_bytes + len(encoded) > MAX_DELTA_TEXT_BYTES:
            result.append("".join(current))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += len(encoded)
    if current:
        result.append("".join(current))
    return tuple(result)


def _provider(value: object) -> str | None:
    return value if type(value) is str and value in _PROVIDERS else None


def _model(value: object) -> str | None:
    text = _bounded_text(value, 256)
    return text


def _stable_code(value: object) -> str | None:
    if type(value) is not str or _STABLE_CODE.fullmatch(value) is None:
        return None
    return value


def _bounded_text(value: object, maximum: int) -> str | None:
    if type(value) is not str or not value or len(value) > maximum:
        return None
    if any(unicodedata.category(character).startswith("C") for character in value):
        return None
    return value if _utf8_within(value, maximum * 4) else None


def _utf8_within(value: str, maximum: int) -> bool:
    try:
        return len(value.encode("utf-8")) <= maximum
    except UnicodeEncodeError:
        return False


__all__ = [
    "API_VERSION",
    "GatewayEventIterator",
    "ValidatedGatewayEventIterator",
    "encode_sse_event",
    "gateway_events",
    "sanitized_gateway_error",
    "validated_gateway_event_stream",
    "validate_gateway_payload",
]
