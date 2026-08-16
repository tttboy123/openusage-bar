"""In-process Gateway runtime composition.

The daemon remains the process boundary.  This runtime is the callable proxy
used by ``GatewayRouter`` for ``POST /gateway/v1/responses``; it converts the
public Gateway envelope into one credential-isolated egress call and returns a
Gateway-native response shape instead of provider-native headers or bodies.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import cache as _cache_module
from . import telemetry as _telemetry_module
from .cache import CacheCandidate, CacheKeySet, ExactHit, SQLiteGatewayCache
from .egress import execute_provider_call
from .fallback import (
    Candidate,
    FallbackAction,
    FallbackSelector,
    FailureSignal,
    ReplayState,
)
from .ingress import GatewayRequest, parse_gateway_request
from .pii import Redactor
from .providers import (
    GatewayProvider,
    GatewayProviderError,
    ProviderEventStream,
    ProviderResult,
)
from .response import (
    API_VERSION,
    MAX_DELTA_TEXT_BYTES,
    MAX_SEQUENCE,
    gateway_events,
    sanitized_gateway_error,
    validate_gateway_payload,
)
from .streaming import (
    IncrementalStreamParser,
    StreamResult,
    TokenUsage,
)
from .telemetry import (
    GatewayTelemetryStore,
    SANITIZED_ERROR_CODES,
    sealed_telemetry_model_scope,
)


_API_VERSION = "gateway.openusage/v1"
_MAX_NATIVE_JSON_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _FallbackMetadata:
    attempted: bool
    attempt_count: int
    final_action: str
    error_code: str | None = None
    retryable: bool | None = None

    def payload(self) -> dict[str, object]:
        result: dict[str, object] = {
            "attempted": self.attempted,
            "attemptCount": self.attempt_count,
            "finalAction": self.final_action,
        }
        if self.error_code is not None and self.retryable is not None:
            result["errorCode"] = self.error_code
            result["retryable"] = self.retryable
        return result


_PRIMARY_FALLBACK = _FallbackMetadata(False, 1, "none")
_EXACT_HIT_FALLBACK = _FallbackMetadata(False, 0, "none")


@dataclass(frozen=True, slots=True)
class _TelemetryLifecycle:
    request_id: str
    started_at: datetime
    monotonic_started_at: float


class GatewayRuntime:
    """Callable runtime adapter for ``GatewayRouter(proxy=...)``."""

    def __init__(
        self,
        *,
        egress: Callable[..., ProviderResult | ProviderEventStream] = execute_provider_call,
        providers: Iterable[GatewayProvider] | None = None,
        keychain: Any | None = None,
        cache: SQLiteGatewayCache | None = None,
        telemetry: GatewayTelemetryStore | None = None,
        fallback_selector: FallbackSelector | None = None,
        fallback_candidates: Iterable[Candidate] = (),
        _unsafe_test_allow_cache_double: bool = False,
    ) -> None:
        if not callable(egress):
            raise ValueError("invalid Gateway runtime")
        if (
            cache is not None
            and type(cache) is not _cache_module.SQLiteGatewayCache
            and not _unsafe_test_allow_cache_double
        ):
            raise ValueError("invalid Gateway runtime")
        if (
            telemetry is not None
            and type(telemetry) is not _telemetry_module.GatewayTelemetryStore
        ):
            raise ValueError("invalid Gateway runtime")
        if fallback_selector is not None and type(fallback_selector) is not FallbackSelector:
            raise ValueError("invalid Gateway runtime")
        candidates = tuple(fallback_candidates)
        if any(type(candidate) is not Candidate for candidate in candidates):
            raise ValueError("invalid Gateway runtime")
        self._egress = egress
        self._providers = providers
        self._keychain = keychain
        self._cache = cache
        self._telemetry = telemetry
        self._fallback_selector = fallback_selector
        self._fallback_candidates = candidates

    def __call__(self, payload: dict[str, object]) -> dict[str, object]:
        try:
            request = parse_gateway_request(payload)
        except ValueError:
            return _error_payload("invalid_request", False)
        except Exception:
            return _error_payload("gateway_unavailable", True)
        lifecycle = self._begin_telemetry()
        try:
            response = self._execute_with_fallback(request, payload)
        except Exception:
            response = _error_payload("gateway_unavailable", True)
        self._record_response_telemetry(lifecycle, request, response)
        return response

    def close(self) -> None:
        """Release the optional persistent cache owned by this runtime."""

        cache = self._cache
        self._cache = None
        if cache is not None:
            cache.close()

    def stream_events(
        self,
        payload: dict[str, object],
    ) -> Iterator[dict[str, object]]:
        """Return one closeable, pull-driven Gateway-native event stream."""

        return self._stream_events_iter(payload)

    def _stream_events_iter(
        self,
        payload: dict[str, object],
    ) -> Iterator[dict[str, object]]:
        try:
            request = parse_gateway_request(payload)
        except ValueError:
            yield from _bounded_gateway_events(
                _error_payload("invalid_request", False)
            )
            return
        except Exception:
            yield from _bounded_gateway_events(
                _error_payload("gateway_unavailable", True)
            )
            return

        lifecycle = self._begin_telemetry()
        if not request.stream:
            try:
                response = self._execute_with_fallback(request, payload)
            except Exception:
                response = _error_payload("gateway_unavailable", True)
            yield from _bounded_gateway_events(
                response,
                accepted_provider=request.provider_id,
                accepted_model=request.model,
            )
            self._record_response_telemetry(lifecycle, request, response)
            return

        active_cache = self._cache
        request_redaction, keys = _cache_key_set(request, active_cache)
        cache_outcome = "miss" if active_cache is not None else None
        cache_available = True
        if active_cache is not None and keys is not None:
            try:
                hit = active_cache.lookup(keys)
            except Exception:
                cache_outcome = "bypass"
                cache_available = False
            else:
                if type(hit) is ExactHit:
                    cached_response = _strict_payload(
                        _cached_stream_payload(
                            request,
                            request_redaction,
                            hit,
                        )
                    )
                    yield from _bounded_gateway_events(
                        cached_response,
                        accepted_provider=request.provider_id,
                        accepted_model=request.model,
                    )
                    self._record_response_telemetry(
                        lifecycle,
                        request,
                        cached_response,
                    )
                    return

        sequence = 0
        yield _gateway_event(
            sequence,
            "gateway.response.started",
            provider=request.provider_id,
            model=request.model,
            stream=True,
        )
        sequence += 1

        active_request = request
        fallback_payload: _FallbackMetadata | None = None
        fallback_selected = False
        emitted_delta_count = 0

        while True:
            result: ProviderResult | ProviderEventStream | None = None
            parser: IncrementalStreamParser | None = None
            completion: StreamResult | None = None
            failure: GatewayProviderError | None = None
            sequence_limited = False
            try:
                raw_result = self._egress(
                    active_request.provider_id,
                    active_request.request_body,
                    providers=self._providers,
                    keychain=self._keychain,
                )
                result, chunks, content_type = _live_stream_source(raw_result)
                try:
                    parser = IncrementalStreamParser(
                        active_request.provider_id,
                        content_type,
                    )
                except (TypeError, ValueError):
                    raise GatewayProviderError(
                        "invalid_upstream_response",
                        False,
                    ) from None

                for chunk in chunks:
                    updates = parser.feed(chunk)
                    for update in updates:
                        if update.kind == "text":
                            text = update.text
                            if type(text) is not str:
                                sequence_limited = True
                                break
                            for part in _live_delta_text_parts(text):
                                if sequence >= MAX_SEQUENCE:
                                    sequence_limited = True
                                    break
                                yield _gateway_event(
                                    sequence,
                                    "gateway.output_text.delta",
                                    text=part,
                                )
                                sequence += 1
                                emitted_delta_count += 1
                            if sequence_limited:
                                break
                        elif update.kind == "tool":
                            if sequence >= MAX_SEQUENCE:
                                sequence_limited = True
                                break
                            yield _gateway_event(
                                sequence,
                                "gateway.tool.observed",
                                tool={"observed": True},
                            )
                            sequence += 1
                    if sequence_limited or parser.invalid or parser.terminal:
                        break
                completion = parser.finish(interrupted=sequence_limited)
            except GatewayProviderError as error:
                if parser is not None and (
                    parser.output_observed or parser.tool_observed
                ):
                    completion = parser.finish(interrupted=True)
                else:
                    failure = error
            except Exception:
                if parser is not None and (
                    parser.output_observed or parser.tool_observed
                ):
                    completion = parser.finish(interrupted=True)
                else:
                    failure = GatewayProviderError(
                        "upstream_unavailable",
                        True,
                    )
            finally:
                if isinstance(result, ProviderEventStream):
                    result.close()

            if completion is not None:
                break
            if failure is None:
                failure = GatewayProviderError("upstream_unavailable", True)

            if not fallback_selected:
                fallback = self._fallback_request(payload, failure)
                if fallback is not None and sequence < MAX_SEQUENCE:
                    active_request, fallback_payload = fallback
                    fallback_selected = True
                    yield _gateway_event(
                        sequence,
                        "gateway.fallback.event",
                        fallback=fallback_payload.payload(),
                    )
                    sequence += 1
                    continue

            if fallback_selected:
                terminal_fallback = _FallbackMetadata(
                    True,
                    2,
                    "fail",
                    "fallback_exhausted",
                    False,
                )
                terminal_event = _gateway_error_event(
                    sequence,
                    "fallback_exhausted",
                    False,
                    fallback=terminal_fallback,
                )
            else:
                terminal_fallback = _PRIMARY_FALLBACK
                terminal_event = _gateway_error_event(
                    sequence,
                    failure.code,
                    failure.retryable,
                )
            yield terminal_event
            self._record_telemetry(
                lifecycle,
                provider_id=active_request.provider_id,
                model_id=active_request.model,
                status_class=_error_status_class(terminal_event),
                usage=None,
                cache_outcome=cache_outcome or "disabled",
                fallback_count=_fallback_count(terminal_fallback.payload()),
                error_code=_telemetry_error_code(terminal_event),
            )
            return

        assert completion is not None
        terminal_cache = _cache_payload(
            cache_outcome or "miss"
        )
        cache_store_allowed = (
            not fallback_selected
            and active_cache is not None
            and keys is not None
            and cache_available
            and completion.cacheable
        )

        if emitted_delta_count == 0 and sequence < MAX_SEQUENCE:
            yield _gateway_event(
                sequence,
                "gateway.output_text.delta",
                text="",
            )
            sequence += 1

        terminal_type = {
            "complete": "gateway.response.completed",
            "interrupted": "gateway.response.interrupted",
            "uncertain": "gateway.response.uncertain",
        }[completion.status]
        terminal_event = _gateway_event(
            min(sequence, MAX_SEQUENCE),
            terminal_type,
            provider=active_request.provider_id,
            model=active_request.model,
            status=completion.status,
            usage=_usage_payload(completion.usage),
            tool={"observed": completion.has_tool_use},
            cache=terminal_cache,
            fallback=(fallback_payload or _PRIMARY_FALLBACK).payload(),
        )
        yield terminal_event

        self._record_telemetry(
            lifecycle,
            provider_id=active_request.provider_id,
            model_id=active_request.model,
            status_class=(
                "2xx" if completion.status == "complete" else "5xx"
            ),
            usage=_usage_payload(completion.usage),
            cache_outcome=(
                terminal_cache["outcome"]
                if active_cache is not None
                else "disabled"
            ),
            fallback_count=_fallback_count(
                (fallback_payload or _PRIMARY_FALLBACK).payload()
            ),
            error_code=(
                None
                if completion.status == "complete"
                else (
                    "stream_interrupted"
                    if completion.status == "interrupted"
                    else "invalid_upstream_response"
                )
            ),
        )

        # A generator is resumed only after the consumer has accepted the
        # yielded terminal event.  Deferring persistence until that resume
        # means GeneratorExit from a failed terminal write cannot cache a
        # delivery the client never observed as complete.
        if cache_store_allowed:
            try:
                _store_stream_cache(
                    active_cache,
                    keys,
                    request_redaction,
                    completion.text,
                    completion,
                )
            except Exception:
                # The terminal event is already delivered.  A cache-store
                # failure is fail-open and must not create a post-terminal
                # event or expose storage details.
                pass

    def _begin_telemetry(self) -> _TelemetryLifecycle | None:
        if self._telemetry is None:
            return None
        try:
            return _TelemetryLifecycle(
                request_id=secrets.token_hex(16),
                started_at=datetime.now(timezone.utc),
                monotonic_started_at=time.monotonic(),
            )
        except Exception:
            return None

    def _record_response_telemetry(
        self,
        lifecycle: _TelemetryLifecycle | None,
        request: GatewayRequest,
        response: dict[str, object],
    ) -> None:
        if response.get("object") == "gateway.response":
            usage = response.get("usage")
            cache = response.get("cache")
            fallback = response.get("fallback")
            status = response.get("status")
            self._record_telemetry(
                lifecycle,
                provider_id=response.get("provider", request.provider_id),
                model_id=response.get("model", request.model),
                status_class="2xx" if status == "complete" else "5xx",
                usage=usage if type(usage) is dict else None,
                cache_outcome=_telemetry_cache_outcome(
                    cache,
                    cache_configured=self._cache is not None,
                ),
                fallback_count=_fallback_count(fallback),
                error_code=(
                    None
                    if status == "complete"
                    else (
                        "stream_interrupted"
                        if status == "interrupted"
                        else "invalid_upstream_response"
                    )
                ),
            )
            return

        fallback = response.get("fallback")
        self._record_telemetry(
            lifecycle,
            provider_id=request.provider_id,
            model_id=request.model,
            status_class=_error_status_class(response),
            usage=None,
            cache_outcome=None,
            fallback_count=_fallback_count(fallback),
            error_code=_telemetry_error_code(response),
        )

    def _record_telemetry(
        self,
        lifecycle: _TelemetryLifecycle | None,
        *,
        provider_id: object,
        model_id: object,
        status_class: str,
        usage: dict[str, object] | None,
        cache_outcome: object,
        fallback_count: int,
        error_code: str | None,
    ) -> None:
        telemetry = self._telemetry
        if lifecycle is None or telemetry is None:
            return
        try:
            model_scope = sealed_telemetry_model_scope(model_id)
            finished_at = datetime.now(timezone.utc)
            elapsed = time.monotonic() - lifecycle.monotonic_started_at
            latency_ms = max(0, int(elapsed * 1_000))
            telemetry.record_request(
                request_id=lifecycle.request_id,
                provider_id=provider_id,
                model_id=model_scope,
                started_at=lifecycle.started_at,
                finished_at=finished_at,
                status_class=status_class,
                input_tokens=_telemetry_token(usage, "inputTokens"),
                output_tokens=_telemetry_token(usage, "outputTokens"),
                latency_ms=latency_ms,
                estimated_cost=None,
                actual_cost=None,
                cache_outcome=(
                    cache_outcome if type(cache_outcome) is str else None
                ),
                fallback_count=fallback_count,
                error_code=error_code,
            )
        except Exception:
            # Local telemetry is optional functional state.  Its clock,
            # validation, or storage failure must never change a safe Gateway
            # result or append a post-terminal stream event.
            pass

    def _execute_with_fallback(
        self,
        request: GatewayRequest,
        payload: dict[str, object],
    ) -> dict[str, object]:
        try:
            return self._execute(request)
        except GatewayProviderError as error:
            fallback = self._fallback_request(payload, error)
            if fallback is None:
                return _error_payload(error.code, error.retryable)
            fallback_request, fallback_payload = fallback
            try:
                return self._execute(
                    fallback_request,
                    fallback_payload=fallback_payload,
                )
            except GatewayProviderError:
                return _error_payload(
                    "fallback_exhausted",
                    False,
                    fallback=_FallbackMetadata(
                        True,
                        2,
                        "fail",
                        "fallback_exhausted",
                        False,
                    ),
                )
            except Exception:
                return _error_payload(
                    "gateway_unavailable",
                    True,
                    fallback=_FallbackMetadata(
                        True,
                        2,
                        "fail",
                        "gateway_unavailable",
                        True,
                    ),
                )

    def _execute(
        self,
        request: GatewayRequest,
        *,
        fallback_payload: _FallbackMetadata | None = None,
    ) -> dict[str, object]:
        # A selected fallback represents the one permitted second Provider
        # call in v1.  Exact-cache replay at that point would make the public
        # attempt count and exact-hit invariants contradictory, so it bypasses
        # cache lookup and performs the selected transport call.
        active_cache = self._cache if fallback_payload is None else None
        request_redaction, keys = _cache_key_set(request, active_cache)
        cache_outcome = "miss" if request.stream and active_cache is not None else None
        cache_available = True
        if request.stream and active_cache is not None and keys is not None:
            try:
                hit = active_cache.lookup(keys)
            except Exception:
                cache_outcome = "bypass"
                cache_available = False
            else:
                if type(hit) is ExactHit:
                    return _strict_payload(
                        _cached_stream_payload(
                            request,
                            request_redaction,
                            hit,
                            fallback_payload=fallback_payload,
                        )
                    )
        result = self._egress(
            request.provider_id,
            request.request_body,
            providers=self._providers,
            keychain=self._keychain,
        )
        response = _response_payload(
            request,
            result,
            fallback_payload=fallback_payload,
            cache_outcome=cache_outcome,
        )
        if (
            request.stream
            and active_cache is not None
            and keys is not None
            and cache_available
            and type(response.get("_stream_result")) is StreamResult
            and type(response.get("outputText")) is str
        ):
            try:
                _store_stream_cache(
                    active_cache,
                    keys,
                    request_redaction,
                    response["outputText"],
                    response["_stream_result"],
                )
            except Exception:
                response["cache"] = {
                    "outcome": "store_failed",
                    "reason": "cache_store_failed",
                }
        response.pop("_stream_result", None)
        return _strict_payload(response)

    def _fallback_request(
        self,
        payload: dict[str, object],
        error: GatewayProviderError,
    ) -> tuple[GatewayRequest, _FallbackMetadata] | None:
        if self._fallback_selector is None:
            return None
        signal = _failure_signal(error)
        if signal is None:
            return None
        primary = parse_gateway_request(payload)
        decision = self._fallback_selector.decide(
            failure=signal,
            retries_used=0,
            primary_cost=1.0,
            candidates=self._fallback_candidates,
            sticky_provider_id=primary.provider_id,
            replay_state=ReplayState(),
        )
        if (
            decision.action is not FallbackAction.RETRY
            and decision.action is not FallbackAction.DEGRADE_TO_CHEAP
        ) or decision.candidate is None:
            return None
        candidate = decision.candidate
        fallback_envelope = _candidate_payload(payload, candidate)
        fallback_request = parse_gateway_request(fallback_envelope)
        final_action = decision.action.value
        return fallback_request, _FallbackMetadata(
            True,
            2,
            final_action,
            error.code,
            error.retryable,
        )

    def __repr__(self) -> str:
        return "GatewayRuntime(<credential-isolated>)"


def _telemetry_token(
    usage: dict[str, object] | None,
    key: str,
) -> int | None:
    if type(usage) is not dict:
        return None
    value = usage.get(key)
    return value if type(value) is int and value >= 0 else None


def _telemetry_cache_outcome(
    value: object,
    *,
    cache_configured: bool,
) -> str | None:
    if type(value) is not dict:
        return None
    outcome = value.get("outcome")
    if outcome == "miss" and not cache_configured:
        return "disabled"
    return outcome if type(outcome) is str else None


def _fallback_count(value: object) -> int:
    if type(value) is not dict:
        return 0
    attempt_count = value.get("attemptCount")
    if type(attempt_count) is not int or attempt_count < 0:
        return 0
    return max(0, attempt_count - 1)


def _error_detail(value: object) -> dict[str, object] | None:
    if type(value) is not dict:
        return None
    error = value.get("error")
    return error if type(error) is dict else None


def _error_status_class(value: object) -> str:
    error = _error_detail(value)
    code = error.get("code") if error is not None else None
    if code in {
        "invalid_request",
        "upstream_authentication_failed",
        "upstream_client_error",
        "upstream_rate_limited",
    }:
        return "4xx"
    return "5xx"


def _telemetry_error_code(value: object) -> str:
    error = _error_detail(value)
    code = error.get("code") if error is not None else None
    if type(code) is str and code in SANITIZED_ERROR_CODES:
        return code
    if code in {"gateway_unavailable", "upstream_unavailable"}:
        return "unavailable"
    if code in {"credential_unavailable", "unsupported_provider"}:
        return "provider_unavailable"
    return "failed"


def _bounded_gateway_events(
    payload: object,
    *,
    accepted_provider: object = None,
    accepted_model: object = None,
) -> Iterator[dict[str, object]]:
    events = gateway_events(
        payload,
        accepted_provider=accepted_provider,
        accepted_model=accepted_model,
    )
    if events is None:
        events = gateway_events(_error_payload("gateway_unavailable", True))
    if events is None:
        return
    try:
        yield from events
    finally:
        events.close()


def _gateway_event(
    sequence: int,
    event_type: str,
    **fields: object,
) -> dict[str, object]:
    if (
        type(sequence) is not int
        or not 0 <= sequence <= MAX_SEQUENCE
        or type(event_type) is not str
        or not event_type.startswith("gateway.")
    ):
        raise RuntimeError("invalid Gateway event")
    return {
        "apiVersion": API_VERSION,
        "type": event_type,
        "sequence": sequence,
        **fields,
    }


def _gateway_error_event(
    sequence: int,
    code: str,
    retryable: bool,
    *,
    fallback: _FallbackMetadata | None = None,
) -> dict[str, object]:
    payload = _error_payload(code, retryable, fallback=fallback)
    event = _gateway_event(
        sequence,
        "gateway.error",
        error=payload["error"],
    )
    if "fallback" in payload:
        event["fallback"] = payload["fallback"]
    return event


def _live_delta_text_parts(text: str) -> tuple[str, ...]:
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


def _live_stream_source(
    value: object,
) -> tuple[
    ProviderResult | ProviderEventStream,
    Iterator[bytes],
    str,
]:
    live = isinstance(value, ProviderEventStream)
    try:
        if not live and type(value) is not ProviderResult:
            raise GatewayProviderError("invalid_upstream_response", False)
        result = value
        assert isinstance(result, (ProviderResult, ProviderEventStream))
        status = result.status_code
        if type(status) is not int or not 100 <= status <= 599:
            raise GatewayProviderError("invalid_upstream_response", False)
        if not 200 <= status <= 299:
            raise _runtime_status_error(status)
        content_type = _single_content_type(result)
        if live:
            chunks = iter(result)
        else:
            if type(result.body_chunks) is not tuple:
                raise GatewayProviderError("invalid_upstream_response", False)
            chunks = iter(result.body_chunks)
        return result, chunks, content_type
    except GatewayProviderError:
        if live:
            value.close()
        raise
    except Exception:
        if live:
            value.close()
        raise GatewayProviderError("invalid_upstream_response", False) from None


def _runtime_status_error(status: int) -> GatewayProviderError:
    if status in {401, 403}:
        return GatewayProviderError("upstream_authentication_failed", False)
    if status == 429:
        return GatewayProviderError("upstream_rate_limited", True)
    if 300 <= status < 500:
        return GatewayProviderError("upstream_client_error", False)
    if 500 <= status <= 599:
        return GatewayProviderError("upstream_server_error", True)
    return GatewayProviderError("invalid_upstream_response", False)


def _error_payload(
    code: str,
    retryable: bool,
    *,
    fallback: _FallbackMetadata | None = None,
) -> dict[str, object]:
    if type(code) is not str or not code:
        code = "gateway_unavailable"
    if type(retryable) is not bool:
        retryable = True
    return sanitized_gateway_error(
        code,
        message="Provider request failed.",
        retryable=retryable,
        fallback=None if fallback is None else fallback.payload(),
    )


def _strict_payload(value: object) -> dict[str, object]:
    validated = validate_gateway_payload(value)
    if validated is None:
        raise RuntimeError("Gateway runtime produced an invalid response")
    return validated


def _response_payload(
    request: GatewayRequest,
    result: ProviderResult | ProviderEventStream,
    *,
    fallback_payload: _FallbackMetadata | None = None,
    cache_outcome: str | None = None,
) -> dict[str, object]:
    if not isinstance(result, (ProviderResult, ProviderEventStream)):
        raise ValueError("invalid provider result")
    if request.stream:
        stream = _aggregate_stream(request.provider_id, result)
        text = stream.text
        usage = stream.usage
        status = stream.status
        tool_observed = stream.has_tool_use
    else:
        if type(result) is not ProviderResult:
            result.close()
            raise ValueError("invalid provider result")
        body = _native_json_body(result)
        text, usage = _extract_non_stream_result(request.provider_id, body)
        status = "complete"
        tool_observed = _native_tool_observed(request.provider_id, body)

    return {
        "apiVersion": _API_VERSION,
        "object": "gateway.response",
        "provider": request.provider_id,
        "model": request.model,
        "stream": request.stream,
        "status": status,
        "outputText": text,
        "usage": _usage_payload(usage),
        "tool": {"observed": tool_observed},
        "cache": _cache_payload(
            cache_outcome or ("miss" if request.stream else "disabled")
        ),
        "fallback": (fallback_payload or _PRIMARY_FALLBACK).payload(),
        "_stream_result": stream if request.stream else None,
    }


def _cache_key_set(
    request: GatewayRequest,
    cache: SQLiteGatewayCache | None,
) -> tuple[object | None, CacheKeySet | None]:
    if cache is None or not request.stream:
        return None, None
    redacted = Redactor().redact(request.request_body.decode("utf-8"))
    keys = CacheKeySet.from_request(
        provider_id=request.provider_id,
        model=request.model,
        parameters={"stream": request.stream},
        request=redacted,
        prefixes=(),
        stream=request.stream,
    )
    return redacted, keys


def _cache_payload(outcome: str) -> dict[str, object]:
    if outcome == "bypass":
        return {"outcome": "bypass", "reason": "cache_unavailable"}
    return {"outcome": outcome}


def _cached_stream_payload(
    request: GatewayRequest,
    request_redaction: object | None,
    hit: ExactHit,
    *,
    fallback_payload: _FallbackMetadata | None = None,
) -> dict[str, object]:
    output = hit.redacted_response
    if request_redaction is not None:
        rehydrate = getattr(request_redaction, "rehydrate", None)
        if callable(rehydrate):
            output = rehydrate(output)
    return {
        "apiVersion": _API_VERSION,
        "object": "gateway.response",
        "provider": request.provider_id,
        "model": request.model,
        "stream": request.stream,
        "status": "complete",
        "outputText": output,
        "usage": _usage_payload(None),
        "tool": {"observed": False},
        "cache": {"outcome": "exact_hit"},
        "fallback": (fallback_payload or _EXACT_HIT_FALLBACK).payload(),
    }


def _failure_signal(error: GatewayProviderError) -> FailureSignal | None:
    if not error.retryable:
        return None
    return {
        "upstream_rate_limited": FailureSignal.RATE_LIMIT,
        "upstream_server_error": FailureSignal.SERVER_ERROR,
        "upstream_timeout": FailureSignal.TIMEOUT,
    }.get(error.code)


def _candidate_payload(
    payload: dict[str, object],
    candidate: Candidate,
) -> dict[str, object]:
    request = payload.get("request")
    if type(request) is not dict:
        raise ValueError("invalid gateway request")
    fallback_request = dict(request)
    fallback_request["model"] = candidate.model
    return {
        "provider": candidate.provider_id,
        "model": candidate.model,
        "request": fallback_request,
    }


def _store_stream_cache(
    cache: SQLiteGatewayCache,
    keys: CacheKeySet,
    request_redaction: object | None,
    output_text: str,
    stream: StreamResult,
) -> None:
    if request_redaction is None:
        return
    redact_response = getattr(request_redaction, "redact_response", None)
    if not callable(redact_response):
        return
    response_redaction = redact_response(output_text)
    candidate = CacheCandidate.create(
        keys=keys,
        request=request_redaction,
        response=response_redaction,
        completion=stream,
        provider_metadata={},
    )
    if candidate is not None:
        cache.store(candidate)


def _aggregate_stream(
    provider_id: str,
    result: ProviderResult | ProviderEventStream,
) -> StreamResult:
    owned, chunks, content_type = _live_stream_source(result)
    try:
        parser = IncrementalStreamParser(provider_id, content_type)
    except (TypeError, ValueError):
        if isinstance(owned, ProviderEventStream):
            owned.close()
        raise GatewayProviderError("invalid_upstream_response", False) from None
    try:
        for chunk in chunks:
            parser.feed(chunk)
            if parser.invalid or parser.terminal:
                break
        return parser.finish()
    except GatewayProviderError:
        if parser.output_observed or parser.tool_observed:
            return parser.finish(interrupted=True)
        raise
    except Exception:
        if parser.output_observed or parser.tool_observed:
            return parser.finish(interrupted=True)
        raise GatewayProviderError("upstream_unavailable", True) from None
    finally:
        if isinstance(owned, ProviderEventStream):
            owned.close()


def _single_content_type(
    result: ProviderResult | ProviderEventStream,
) -> str:
    if type(result.headers) is not tuple:
        raise ValueError("invalid provider result")
    values: list[str] = []
    for item in result.headers:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError("invalid provider result")
        name, value = item
        if type(name) is not str or type(value) is not str:
            raise ValueError("invalid provider result")
        if name.casefold() == "content-type":
            values.append(value)
    if len(values) != 1:
        raise ValueError("invalid provider result")
    return values[0]


def _native_json_body(result: ProviderResult) -> object:
    content_type = _single_content_type(result).split(";", 1)[0].strip().casefold()
    if content_type != "application/json":
        raise ValueError("invalid provider result")
    if type(result.body_chunks) is not tuple or not result.body_chunks:
        raise ValueError("invalid provider result")
    total = 0
    chunks: list[bytes] = []
    for chunk in result.body_chunks:
        if type(chunk) is not bytes:
            raise ValueError("invalid provider result")
        total += len(chunk)
        if total > _MAX_NATIVE_JSON_BYTES:
            raise ValueError("invalid provider result")
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("invalid provider result") from None


def _extract_non_stream_result(
    provider_id: str,
    body: object,
) -> tuple[str, TokenUsage | None]:
    if type(body) is not dict:
        raise ValueError("invalid provider result")
    if provider_id == "openai":
        return _extract_openai(body)
    if provider_id == "anthropic":
        return _extract_anthropic(body)
    if provider_id in {"deepseek", "openrouter"}:
        return _extract_openai_compatible(body)
    if provider_id == "ollama":
        return _extract_ollama(body)
    raise ValueError("invalid provider result")


def _native_tool_observed(provider_id: str, body: object) -> bool:
    if type(body) is not dict:
        return False
    if provider_id == "openai":
        output = body.get("output")
        if type(output) is list:
            for item in output:
                if type(item) is not dict:
                    continue
                item_type = item.get("type")
                if type(item_type) is str and (
                    item_type.endswith("_call")
                    or item_type in {"function_call", "custom_tool_call"}
                ):
                    return True
        return bool(body.get("tool_calls"))
    if provider_id == "anthropic":
        content = body.get("content")
        return type(content) is list and any(
            type(item) is dict and item.get("type") == "tool_use"
            for item in content
        )
    if provider_id in {"deepseek", "openrouter"}:
        choices = body.get("choices")
        if type(choices) is list:
            for choice in choices:
                message = choice.get("message") if type(choice) is dict else None
                if type(message) is dict and (
                    message.get("tool_calls") or message.get("function_call")
                ):
                    return True
        return bool(body.get("tool_calls"))
    if provider_id == "ollama":
        message = body.get("message")
        return type(message) is dict and bool(message.get("tool_calls"))
    return False


def _extract_openai(body: dict[str, object]) -> tuple[str, TokenUsage | None]:
    text = body.get("output_text")
    if type(text) is str:
        output = text
    else:
        parts: list[str] = []
        raw_output = body.get("output")
        if type(raw_output) is list:
            for item in raw_output:
                if type(item) is not dict:
                    continue
                content = item.get("content")
                if type(content) is not list:
                    continue
                for part in content:
                    if type(part) is dict and part.get("type") == "output_text":
                        value = part.get("text")
                        if type(value) is str:
                            parts.append(value)
        output = "".join(parts)
    return output, _usage_from_mapping(
        body.get("usage"), "input_tokens", "output_tokens"
    )


def _extract_anthropic(body: dict[str, object]) -> tuple[str, TokenUsage | None]:
    parts: list[str] = []
    content = body.get("content")
    if type(content) is list:
        for item in content:
            if type(item) is dict and item.get("type") == "text":
                text = item.get("text")
                if type(text) is str:
                    parts.append(text)
    return "".join(parts), _usage_from_mapping(
        body.get("usage"), "input_tokens", "output_tokens"
    )


def _extract_openai_compatible(
    body: dict[str, object]
) -> tuple[str, TokenUsage | None]:
    parts: list[str] = []
    choices = body.get("choices")
    if type(choices) is list:
        for choice in choices:
            if type(choice) is not dict:
                continue
            message = choice.get("message")
            if type(message) is dict:
                content = message.get("content")
                if type(content) is str:
                    parts.append(content)
    return "".join(parts), _usage_from_mapping(
        body.get("usage"), "prompt_tokens", "completion_tokens"
    )


def _extract_ollama(body: dict[str, object]) -> tuple[str, TokenUsage | None]:
    text = ""
    message = body.get("message")
    if type(message) is dict and type(message.get("content")) is str:
        text = message["content"]
    return text, _usage_from_values(
        body.get("prompt_eval_count"), body.get("eval_count")
    )


def _usage_from_mapping(
    value: object,
    input_key: str,
    output_key: str,
) -> TokenUsage | None:
    if value is None:
        return None
    if type(value) is not dict:
        raise ValueError("invalid provider result")
    return _usage_from_values(value.get(input_key), value.get(output_key))


def _usage_from_values(
    input_value: object,
    output_value: object,
) -> TokenUsage | None:
    input_tokens = _optional_nonnegative_int(input_value)
    output_tokens = _optional_nonnegative_int(output_value)
    if input_tokens is None and output_tokens is None:
        return None
    return TokenUsage(input_tokens, output_tokens)


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("invalid provider result")
    return value


def _usage_payload(usage: TokenUsage | None) -> dict[str, int | None] | None:
    if usage is None:
        return None
    return {
        "inputTokens": usage.input_tokens,
        "outputTokens": usage.output_tokens,
    }


__all__ = ["GatewayRuntime"]
