"""Pure, bounded request router for the optional Gateway API v1."""

from __future__ import annotations

import copy
import json
import math
import re
import unicodedata
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..runtime_capabilities import (
    OPERATIONAL_VALUES,
    build_runtime_capability,
    validate_runtime_capability,
)
from .contracts import Decision, GatewayMode, ShouldSendDecision, ShouldSendRequest
from .ingress import parse_gateway_request
from .pools import validate_account_pools_public_payload
from .response import (
    encode_sse_event,
    gateway_events,
    sanitized_gateway_error,
    validated_gateway_event_stream,
    validate_gateway_payload,
)


MAX_BODY_BYTES = 4 * 1024 * 1024
_MAX_PROVIDER_LENGTH = 128
_MAX_MODEL_LENGTH = 256
_MAX_WINDOW_LENGTH = 64
_MAX_TIMESTAMP_LENGTH = 64
_DEFER_UNTIL_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}"
    r"(?::[0-9]{2}(?:\.[0-9]{1,6})?)?"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})"
)
_REASONS = frozenset(
    {
        "approaching_limit",
        "burn_rate_too_high",
        "quota_healthy",
        "quota_low",
        "quota_unknown",
    }
)
_ROUTES = frozenset(
    {
        ("GET", "/gateway/v1/health"),
        ("GET", "/gateway/v1/account-pools"),
        ("GET", "/gateway/v1/schema"),
        ("POST", "/gateway/v1/should-send"),
        ("POST", "/gateway/v1/responses"),
    }
)
_PATHS = frozenset(path for _, path in _ROUTES)
_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "resources/gateway-api-v1.schema.json")
    .read_text(encoding="utf-8"),
    parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
)

_UNKNOWN_FEATURE = {
    "support": "unknown",
    "enabled": "unknown",
    "configured": "unknown",
    "operational": "unknown",
}

Policy = Callable[[ShouldSendRequest], ShouldSendDecision]
Proxy = Callable[[dict[str, object]], dict[str, object]]
GatewayEvent = dict[str, object]
GatewayDelivery = Iterable[GatewayEvent] | dict[str, object]
AccountPools = Callable[[], dict[str, object]]


def _problem(code: str, message: str, retryable: bool) -> dict[str, object]:
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        }
    }


def _invalid_request() -> tuple[int, dict[str, object]]:
    return 400, _problem("invalid_request", "Invalid request.", False)


def _feature_capability(
    *, enabled: bool, configured: bool, operational: str
) -> dict[str, object]:
    return {
        "support": "supported",
        "enabled": enabled,
        "configured": configured,
        "operational": operational,
    }


def _gateway_capability_source(
    mode: GatewayMode,
    *,
    listener_operational: str | None,
    policy_configured: bool,
    proxy_configured: bool,
) -> dict[str, object]:
    listener_enabled = mode is not GatewayMode.OBSERVE
    should_send_enabled = mode is not GatewayMode.OBSERVE
    responses_enabled = mode is GatewayMode.GATEWAY
    if not listener_enabled:
        operational = "disabled"
    elif (
        type(listener_operational) is str
        and listener_operational in OPERATIONAL_VALUES
    ):
        operational = listener_operational
    else:
        operational = "unknown"

    return {
        "mode": mode.value,
        "operational": operational,
        "features": {
            "listener": _feature_capability(
                enabled=listener_enabled,
                configured=listener_enabled,
                operational=operational,
            ),
            "should_send": _feature_capability(
                enabled=should_send_enabled,
                configured=policy_configured,
                operational="unknown" if should_send_enabled else "disabled",
            ),
            "responses": _feature_capability(
                enabled=responses_enabled,
                configured=proxy_configured,
                operational="unknown" if responses_enabled else "disabled",
            ),
            "cache": dict(_UNKNOWN_FEATURE),
            "fallback": dict(_UNKNOWN_FEATURE),
            "pii_redaction": dict(_UNKNOWN_FEATURE),
            "streaming": dict(_UNKNOWN_FEATURE),
        },
        "configuredProviderCount": None,
        "healthyProviderCount": None,
        "actions": ["retry"],
        "lastError": None,
    }


def _renderer_safe_runtime_capability(
    mode: GatewayMode,
    *,
    listener_operational: str | None,
    policy_configured: bool,
    proxy_configured: bool,
) -> dict[str, object] | None:
    try:
        candidate = build_runtime_capability(
            None,
            _gateway_capability_source(
                mode,
                listener_operational=listener_operational,
                policy_configured=policy_configured,
                proxy_configured=proxy_configured,
            ),
        )
        return validate_runtime_capability(candidate)
    except Exception:
        return None


def _internal_error() -> tuple[int, dict[str, object]]:
    return 500, _problem(
        "internal_error", "Request could not be completed.", True
    )


def _reject_constant(_value: str) -> None:
    raise ValueError("non-standard JSON value")


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json_object(raw: bytes) -> dict[str, Any] | None:
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(
            decoded,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        return None
    return value if type(value) is dict else None


def _bounded_text(value: object, maximum: int) -> str | None:
    if type(value) is not str or not value or len(value) > maximum:
        return None
    if value != value.strip():
        return None
    if any(unicodedata.category(character).startswith("C") for character in value):
        return None
    return value


def _finite_number(value: object, *, minimum: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        return None
    return result


def _valid_defer_until(value: object) -> str | None | object:
    if value is None:
        return None
    text = _bounded_text(value, _MAX_TIMESTAMP_LENGTH)
    if text is None or _DEFER_UNTIL_PATTERN.fullmatch(text) is None:
        return _INVALID
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return _INVALID
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return _INVALID
    return text


_INVALID = object()


def _decision_payload(value: object) -> dict[str, object] | None:
    if not isinstance(value, ShouldSendDecision) or not isinstance(
        value.decision, Decision
    ):
        return None
    confidence = _finite_number(value.confidence, minimum=0.0)
    if confidence is None or confidence > 1:
        return None
    if type(value.reason) is not str or value.reason not in _REASONS:
        return None
    defer_until: str | None = None
    if value.decision is Decision.DEFER:
        parsed_defer_until = _valid_defer_until(value.defer_until)
        if parsed_defer_until is _INVALID:
            return None
        assert parsed_defer_until is None or isinstance(parsed_defer_until, str)
        defer_until = parsed_defer_until

    optional_numbers: list[float | None] = []
    for item in (
        value.quota_remaining,
        value.burn_rate_per_minute,
        value.predicted_exhaustion_minutes,
    ):
        if item is None:
            optional_numbers.append(None)
            continue
        parsed = _finite_number(item, minimum=0.0)
        if parsed is None:
            return None
        optional_numbers.append(parsed)

    return {
        "decision": value.decision.value,
        "confidence": confidence,
        "reason": value.reason,
        "defer_until": defer_until,
        "details": {
            "quota_remaining": optional_numbers[0],
            "burn_rate_per_min": optional_numbers[1],
            "predicted_exhaustion_minutes": optional_numbers[2],
        },
    }


class GatewayRouter:
    """Dispatch the additive Gateway v1 routes without doing external I/O."""

    def __init__(
        self,
        mode: GatewayMode,
        policy: Policy | None,
        proxy: Proxy | None,
        account_pools: AccountPools | None = None,
    ) -> None:
        if not isinstance(mode, GatewayMode):
            raise ValueError("Gateway mode is invalid.")
        if policy is not None and not callable(policy):
            raise ValueError("Gateway policy is invalid.")
        if proxy is not None and not callable(proxy):
            raise ValueError("Gateway proxy is invalid.")
        if account_pools is not None and not callable(account_pools):
            raise ValueError("Gateway account pools are invalid.")
        self._mode = mode
        self._policy = policy
        self._proxy = proxy
        self._account_pools = account_pools

    def close(self) -> None:
        """Release proxy-owned resources once the listener is no longer active."""

        proxy = self._proxy
        self._proxy = None
        close = getattr(proxy, "close", None)
        if callable(close):
            close()

    def dispatch(
        self,
        method: str,
        path: str,
        body: bytes,
    ) -> tuple[int, dict[str, object]]:
        if type(path) is not str or path not in _PATHS:
            return 404, _problem("not_found", "Route not found.", False)
        if type(method) is not str or (method, path) not in _ROUTES:
            return 405, _problem(
                "method_not_allowed", "Method is not allowed.", False
            )
        if type(body) is not bytes:
            return _invalid_request()
        if method == "GET":
            if body:
                return 413, _problem(
                    "request_body_not_allowed",
                    "Request bodies are not allowed.",
                    False,
                )
            if path == "/gateway/v1/schema":
                return 200, copy.deepcopy(_SCHEMA)
            if path == "/gateway/v1/account-pools":
                if self._mode is GatewayMode.OBSERVE or self._account_pools is None:
                    return 200, {"accounts": []}
                try:
                    candidate = self._account_pools()
                    payload = validate_account_pools_public_payload(candidate)
                except Exception:
                    return _internal_error()
                return (200, payload) if payload is not None else _internal_error()
            return 200, {
                "apiVersion": "gateway.openusage/v1",
                "status": "disabled" if self._mode is GatewayMode.OBSERVE else "ok",
                "mode": self._mode.value,
                "capabilities": {
                    "shouldSend": self._mode is not GatewayMode.OBSERVE,
                    "responses": (
                        self._mode is GatewayMode.GATEWAY
                        and self._proxy is not None
                    ),
                },
            }

        if len(body) > MAX_BODY_BYTES:
            return 413, _problem("request_too_large", "Request is too large.", False)
        payload = _json_object(body)
        if payload is None:
            return _invalid_request()

        if self._mode is GatewayMode.OBSERVE:
            return 404, _problem("gateway_disabled", "Gateway is disabled.", False)

        if path == "/gateway/v1/responses":
            if self._mode is not GatewayMode.GATEWAY or self._proxy is None:
                return 403, _problem(
                    "proxy_disabled", "Gateway proxy is disabled.", False
                )
            try:
                parse_gateway_request(payload)
            except ValueError:
                return _invalid_request()
            try:
                response = self._proxy(payload)
            except Exception:
                return _versioned_internal_error()
            safe_response = validate_gateway_payload(response)
            if safe_response is None:
                return _versioned_internal_error()
            return 200, safe_response

        if set(payload) != {"provider", "model", "estimated_tokens", "window"}:
            return _invalid_request()
        provider = _bounded_text(payload["provider"], _MAX_PROVIDER_LENGTH)
        model = _bounded_text(payload["model"], _MAX_MODEL_LENGTH)
        window = _bounded_text(payload["window"], _MAX_WINDOW_LENGTH)
        estimated_tokens = payload["estimated_tokens"]
        if (
            provider is None
            or model is None
            or window is None
            or type(estimated_tokens) is not int
            or not 1 <= estimated_tokens <= 2**31 - 1
        ):
            return _invalid_request()
        if self._policy is None:
            return _internal_error()
        request = ShouldSendRequest(provider, model, estimated_tokens, window)
        try:
            decision = self._policy(request)
        except Exception:
            return _internal_error()
        try:
            serialized = _decision_payload(decision)
        except Exception:
            return _internal_error()
        return (200, serialized) if serialized is not None else _internal_error()

    def dispatch_runtime_capability(
        self,
        method: str,
        path: str,
        body: bytes,
        *,
        listener_operational: str | None = None,
    ) -> tuple[int, dict[str, object]]:
        """Return the negotiated renderer-safe health representation only."""
        status, payload = self.dispatch(method, path, body)
        if status != 200 or method != "GET" or path != "/gateway/v1/health":
            return status, payload
        runtime_capability = _renderer_safe_runtime_capability(
            self._mode,
            listener_operational=listener_operational,
            policy_configured=self._policy is not None,
            proxy_configured=self._proxy is not None,
        )
        if runtime_capability is None:
            return 500, _problem(
                "capability_invalid",
                "Gateway status could not be verified.",
                True,
            )
        return 200, runtime_capability

    def dispatch_sse(
        self,
        method: str,
        path: str,
        body: bytes,
    ) -> tuple[int, bytes]:
        status, delivery = self.dispatch_events(method, path, body)
        if type(delivery) is dict:
            return status, _encoded_json(delivery)
        try:
            encoded = b"".join(
                encode_sse_event(event) for event in delivery
            )
        finally:
            close = getattr(delivery, "close", None)
            if callable(close):
                close()
        return status, encoded

    def dispatch_events(
        self,
        method: str,
        path: str,
        body: bytes,
    ) -> tuple[int, GatewayDelivery]:
        stream_factory = None
        if self._proxy is not None:
            try:
                candidate = getattr(self._proxy, "stream_events", None)
            except Exception:
                candidate = None
            if callable(candidate):
                stream_factory = candidate
        if (
            method == "POST"
            and path == "/gateway/v1/responses"
            and stream_factory is not None
        ):
            if type(body) is not bytes:
                return _invalid_request()
            if len(body) > MAX_BODY_BYTES:
                return 413, _problem(
                    "request_too_large",
                    "Request is too large.",
                    False,
                )
            payload = _json_object(body)
            if payload is None:
                return _invalid_request()
            if self._mode is GatewayMode.OBSERVE:
                return 404, _problem(
                    "gateway_disabled",
                    "Gateway is disabled.",
                    False,
                )
            if self._mode is not GatewayMode.GATEWAY or self._proxy is None:
                return 403, _problem(
                    "proxy_disabled",
                    "Gateway proxy is disabled.",
                    False,
                )
            try:
                request = parse_gateway_request(payload)
            except ValueError:
                return _invalid_request()
            try:
                delivery = stream_factory(payload)
                validated = validated_gateway_event_stream(
                    delivery,
                    accepted_provider=request.provider_id,
                    accepted_model=request.model,
                )
            except Exception:
                status, failure = _versioned_internal_error()
                events = gateway_events(failure)
                return (status, failure) if events is None else (status, events)
            if validated is None:
                status, failure = _versioned_internal_error()
                events = gateway_events(failure)
                return (status, failure) if events is None else (status, events)
            return 200, validated

        status, payload = self.dispatch(method, path, body)
        request = _request_identity(body)
        accepted_provider, accepted_model = request or (None, None)
        events = gateway_events(
            payload,
            accepted_provider=accepted_provider,
            accepted_model=accepted_model,
        )
        if events is None:
            return status, payload
        return status, events


def _versioned_internal_error() -> tuple[int, dict[str, object]]:
    return 500, sanitized_gateway_error()


def _encoded_json(value: dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _request_identity(body: object) -> tuple[str, str] | None:
    if type(body) is not bytes or len(body) > MAX_BODY_BYTES:
        return None
    payload = _json_object(body)
    if payload is None:
        return None
    provider = payload.get("provider")
    model = payload.get("model")
    if type(provider) is str and type(model) is str:
        return provider, model
    return None


__all__ = ["GatewayRouter", "MAX_BODY_BYTES"]
