"""Deterministic, offline composition probe for the frozen Gateway runtime.

This module is imported only by the Collector's hidden release-gate command.
It deliberately exercises production routing, policy, runtime, egress, Provider,
and Observer boundaries while replacing only the final network and credential
edges with in-memory test doubles.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from email.message import Message
from typing import Any

from ..activity_store import ActivityStore
from ..local_api import LocalAPIRouter
from ..query import QueryService
from .api import GatewayRouter
from .contracts import GatewayMode, ShouldSendDecision, ShouldSendRequest
from .policy import PolicySnapshot, ShouldSendPolicy
from .providers import ProviderResult, default_gateway_providers
from .runtime import GatewayRuntime


_SCHEMA_VERSION = "gateway-self-test/v1"
_OBJECT_TYPE = "gateway.self_test"
_MODEL = "gpt-4.1-mini"
_CREDENTIAL_ACCOUNT = "openai.gateway-api-key"
_SYNTHETIC_CREDENTIAL = "offline-self-test-key"
_FIXED_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _SelfTestFailure(RuntimeError):
    """Content-free internal signal that can never disclose a failed value."""


def _require(condition: bool) -> None:
    if condition is not True:
        raise _SelfTestFailure


def _json_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class _InMemoryHealthHandler:
    """Small adapter that executes ``LocalAPIRouter`` without opening a socket."""

    def __init__(self) -> None:
        self.path = "/v1/health"
        self.headers = Message()
        self.headers["Host"] = "localhost"
        self.status: int | None = None
        self.body = b""

    def _send(
        self,
        status: object,
        body: bytes,
        _headers: object,
        *,
        include_body: bool,
        include_length: bool = True,
    ) -> None:
        del include_length
        self.status = int(status)  # type: ignore[arg-type]
        self.body = body if include_body else b""

    def _problem(self, problem: Any, *, include_body: bool) -> None:
        del include_body
        self.status = int(problem.status)
        self.body = b""


def _observer_is_healthy() -> bool:
    store: ActivityStore | None = None
    healthy = False
    try:
        store = ActivityStore(":memory:")
        clock = lambda: _FIXED_NOW
        query = QueryService(store, clock=clock)
        router = LocalAPIRouter(query, clock=clock)
        handler = _InMemoryHealthHandler()
        router.handle(handler, include_body=True)  # type: ignore[arg-type]
        if handler.status == 200 and handler.body:
            payload = json.loads(handler.body)
            healthy = (
                type(payload) is dict
                and payload.get("health") == {"ok": True, "status": "ok"}
            )
    except Exception:
        healthy = False
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                healthy = False
    return healthy


class _MemoryKeychain:
    def __init__(self) -> None:
        self.reads = 0

    def get(self, account: str) -> str:
        self.reads += 1
        _require(account == _CREDENTIAL_ACCOUNT)
        return _SYNTHETIC_CREDENTIAL


class _UnavailableKeychain:
    def __init__(self) -> None:
        self.reads = 0

    def get(self, account: str) -> str:
        self.reads += 1
        _require(account == _CREDENTIAL_ACCOUNT)
        raise RuntimeError


class _OfflineResolver:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, host: str) -> list[str]:
        self.calls += 1
        _require(host == "api.openai.com")
        # A syntactically public address lets the real endpoint validator run;
        # the injected transport below guarantees no connection is attempted.
        return ["8.8.8.8"]


class _OfflineTransport:
    def __init__(self, *, calls_allowed: bool) -> None:
        self.calls_allowed = calls_allowed
        self.calls = 0

    def send(
        self,
        *,
        method: str,
        endpoint: str,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
        max_response_header_count: int,
        max_response_header_bytes: int,
        max_response_bytes: int,
        max_response_chunks: int,
        allow_redirects: bool,
    ) -> ProviderResult:
        del (
            timeout_seconds,
            max_response_header_count,
            max_response_header_bytes,
            max_response_bytes,
            max_response_chunks,
        )
        self.calls += 1
        _require(self.calls_allowed)
        _require(method == "POST")
        _require(endpoint == "https://api.openai.com/v1/responses")
        _require(headers.get("Authorization") == f"Bearer {_SYNTHETIC_CREDENTIAL}")
        _require(headers.get("Content-Type") == "application/json")
        _require(body == _json_bytes({"input": "offline self-test", "model": _MODEL}))
        _require(allow_redirects is False)
        return ProviderResult(
            status_code=200,
            headers=(("Content-Type", "application/json"),),
            body_chunks=(b'{"object":"response","output":[]}',),
        )


def _gateway_request_body() -> bytes:
    return _json_bytes(
        {
            "provider": "openai",
            "model": _MODEL,
            "request": {
                "input": "offline self-test",
                "model": _MODEL,
            },
        }
    )


def _passed_check(name: str) -> dict[str, object]:
    if name == "observe":
        return {
            "ok": True,
            "observer": "healthy",
            "gateway": "disabled",
            "credentialReads": 0,
            "providerCalls": 0,
        }
    if name == "advise":
        return {
            "ok": True,
            "decision": "yes",
            "reason": "quota_healthy",
            "credentialReads": 0,
            "providerCalls": 0,
        }
    if name == "gateway":
        return {
            "ok": True,
            "status": "complete",
            "credentialReads": 1,
            "providerCalls": 1,
        }
    return {
        "ok": True,
        "errorCode": "credential_unavailable",
        "retryable": False,
        "providerCalls": 0,
        "observer": "healthy",
    }


def _observe_check() -> dict[str, object]:
    _require(_observer_is_healthy())
    router = GatewayRouter(GatewayMode.OBSERVE, policy=None, proxy=None)
    status, payload = router.dispatch("GET", "/gateway/v1/health", b"")
    _require(status == 200)
    _require(payload.get("status") == "disabled")
    _require(payload.get("mode") == "observe")
    return _passed_check("observe")


def _advise_check() -> dict[str, object]:
    policy = ShouldSendPolicy()
    snapshot = PolicySnapshot(
        remaining_ratio=0.8,
        quota_remaining=None,
        burn_rate_per_minute=None,
        unknown=False,
    )
    policy_calls = 0

    def should_send(request: ShouldSendRequest) -> ShouldSendDecision:
        nonlocal policy_calls
        policy_calls += 1
        return policy.decide(request, snapshot)

    router = GatewayRouter(GatewayMode.ADVISE, policy=should_send, proxy=None)
    status, payload = router.dispatch(
        "POST",
        "/gateway/v1/should-send",
        _json_bytes(
            {
                "provider": "openai",
                "model": _MODEL,
                "estimated_tokens": 512,
                "window": "5m",
            }
        ),
    )
    _require(status == 200)
    _require(policy_calls == 1)
    _require(payload.get("decision") == "yes")
    _require(payload.get("reason") == "quota_healthy")
    return _passed_check("advise")


def _gateway_check() -> dict[str, object]:
    keychain = _MemoryKeychain()
    resolver = _OfflineResolver()
    transport = _OfflineTransport(calls_allowed=True)
    providers = default_gateway_providers(transport=transport, resolver=resolver)
    # Omit ``egress`` intentionally: the runtime must use execute_provider_call.
    runtime = GatewayRuntime(providers=providers, keychain=keychain)
    router = GatewayRouter(GatewayMode.GATEWAY, policy=None, proxy=runtime)
    status, payload = router.dispatch(
        "POST", "/gateway/v1/responses", _gateway_request_body()
    )
    _require(status == 200)
    _require(payload.get("object") == "gateway.response")
    _require(payload.get("status") == "complete")
    _require(keychain.reads == 1)
    _require(resolver.calls == 1)
    _require(transport.calls == 1)
    return _passed_check("gateway")


def _credential_failure_check() -> dict[str, object]:
    keychain = _UnavailableKeychain()
    resolver = _OfflineResolver()
    transport = _OfflineTransport(calls_allowed=False)
    providers = default_gateway_providers(transport=transport, resolver=resolver)
    # The credential boundary must fail inside the same default egress path.
    runtime = GatewayRuntime(providers=providers, keychain=keychain)
    router = GatewayRouter(GatewayMode.GATEWAY, policy=None, proxy=runtime)
    status, payload = router.dispatch(
        "POST", "/gateway/v1/responses", _gateway_request_body()
    )
    error = payload.get("error")
    _require(status == 200)
    _require(payload.get("object") == "gateway.error")
    _require(type(error) is dict)
    _require(error.get("code") == "credential_unavailable")
    _require(error.get("retryable") is False)
    _require(keychain.reads == 1)
    _require(resolver.calls == 0)
    _require(transport.calls == 0)
    _require(_observer_is_healthy())
    return _passed_check("credentialFailure")


def _failed_check(name: str) -> dict[str, object]:
    if name == "observe":
        return {
            "ok": False,
            "observer": "unavailable",
            "gateway": "disabled",
            "credentialReads": 0,
            "providerCalls": 0,
        }
    if name == "advise":
        return {
            "ok": False,
            "decision": "defer",
            "reason": "self_test_failed",
            "credentialReads": 0,
            "providerCalls": 0,
        }
    if name == "gateway":
        return {
            "ok": False,
            "status": "failed",
            "credentialReads": 0,
            "providerCalls": 0,
        }
    return {
        "ok": False,
        "errorCode": "self_test_failed",
        "retryable": False,
        "providerCalls": 0,
        "observer": "unavailable",
    }


def run_gateway_self_test() -> dict[str, object]:
    """Exercise the frozen production composition with no external side effects."""

    runners = {
        "observe": _observe_check,
        "advise": _advise_check,
        "gateway": _gateway_check,
        "credentialFailure": _credential_failure_check,
    }
    checks: dict[str, dict[str, object]] = {}
    for name, runner in runners.items():
        try:
            checks[name] = runner()
        except Exception:
            checks[name] = _failed_check(name)
    return {
        "schemaVersion": _SCHEMA_VERSION,
        "object": _OBJECT_TYPE,
        "ok": all(check.get("ok") is True for check in checks.values()),
        "checks": checks,
    }


def is_gateway_self_test_report(value: object) -> bool:
    """Accept only the closed, content-free self-test success/failure shapes."""

    try:
        if type(value) is not dict or set(value) != {
            "schemaVersion",
            "object",
            "ok",
            "checks",
        }:
            return False
        if (
            value.get("schemaVersion") != _SCHEMA_VERSION
            or value.get("object") != _OBJECT_TYPE
            or type(value.get("ok")) is not bool
        ):
            return False
        checks = value.get("checks")
        names = ("observe", "advise", "gateway", "credentialFailure")
        if type(checks) is not dict or set(checks) != set(names):
            return False
        for name in names:
            candidate = checks.get(name)
            if type(candidate) is not dict:
                return False
            encoded = _json_bytes(candidate)
            if encoded not in {
                _json_bytes(_passed_check(name)),
                _json_bytes(_failed_check(name)),
            }:
                return False
        expected_ok = all(checks[name].get("ok") is True for name in names)
        return value.get("ok") is expected_ok
    except (TypeError, ValueError, OverflowError, UnicodeError):
        return False


__all__ = ["is_gateway_self_test_report", "run_gateway_self_test"]
