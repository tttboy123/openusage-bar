from __future__ import annotations

import http.client
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import openusage_bar.gateway.server as gateway_server_module
from openusage_bar.gateway.api import GatewayRouter
from openusage_bar.gateway.config import GatewayMode
from openusage_bar.gateway.contracts import (
    Decision,
    ShouldSendDecision,
    ShouldSendRequest,
)
from openusage_bar.gateway.server import create_gateway_server


ROOT = Path(__file__).resolve().parents[1]
GATEWAY_TOKEN = "g" * 48
VALID_REQUEST = {
    "provider": "openai",
    "model": "gpt-4o",
    "estimated_tokens": 8_000,
    "window": "5m",
}
DECISION = ShouldSendDecision(
    decision=Decision.DEFER,
    confidence=0.8,
    reason="quota_low",
    defer_until=None,
    quota_remaining=0.12,
    burn_rate_per_minute=10.0,
    predicted_exhaustion_minutes=120.0,
)
ACCOUNT_POOLS = {
    "accounts": [
        {
            "alias": "Work",
            "displayId": "acct_0123456789ab",
            "providerId": "openai",
            "status": "unknown",
            "quota": {
                "state": "unknown",
                "remaining": None,
                "limit": None,
                "resetAt": None,
            },
            "cooldown": {"state": "unknown", "until": None},
            "pools": [
                {"poolId": "daily-coding", "priority": 10, "weight": 1}
            ],
            "priority": 10,
            "weight": 1,
        }
    ],
    "pools": [
        {
            "poolId": "daily-coding",
            "revision": 3,
            "strategy": "fixed-first",
            "members": [
                {
                    "displayId": "acct_0123456789ab",
                    "priority": 10,
                    "weight": 1,
                }
            ],
            "crossProviderFallback": False,
            "crossModelFallback": False,
            "crossRegionFallback": False,
        }
    ],
}


def body(payload: object = VALID_REQUEST) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def policy(_request: ShouldSendRequest) -> ShouldSendDecision:
    return DECISION


def proxy(_payload: dict[str, object]) -> dict[str, object]:
    return {
        "apiVersion": "gateway.openusage/v1",
        "object": "gateway.response",
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "stream": False,
        "status": "complete",
        "outputText": "",
        "usage": None,
        "tool": {"observed": False},
        "cache": {"outcome": "disabled"},
        "fallback": {
            "attempted": False,
            "attemptCount": 1,
            "finalAction": "none",
        },
    }


def assert_problem(
    test: unittest.TestCase,
    payload: dict[str, object],
    code: str,
    retryable: bool,
) -> None:
    test.assertEqual(set(payload), {"error"})
    problem = payload["error"]
    test.assertIsInstance(problem, dict)
    assert isinstance(problem, dict)
    test.assertEqual(set(problem), {"code", "message", "retryable"})
    test.assertEqual(problem["code"], code)
    test.assertIsInstance(problem["message"], str)
    test.assertTrue(problem["message"])
    test.assertIs(problem["retryable"], retryable)


def start(server: object) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def request(
    port: int,
    token: str,
    target: str = "/gateway/v1/health",
    *,
    method: str = "GET",
    payload: bytes | None = None,
    host: str | None = None,
    authorize: bool = True,
) -> tuple[int, dict[str, str], dict[str, object]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if authorize:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
    connection.request(method, target, body=payload, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    result = (
        response.status,
        {name.lower(): value for name, value in response.getheaders()},
        json.loads(raw),
    )
    connection.close()
    return result


class GatewayRouterTests(unittest.TestCase):
    def test_internal_egress_counters_require_bearer_and_never_expand_public_routes(
        self,
    ) -> None:
        from openusage_bar.gateway.egress import gateway_egress_attempt_counters

        target = "/_internal/v1/gateway-egress-attempts"
        router = GatewayRouter(mode=GatewayMode.OBSERVE, policy=None, proxy=None)
        direct_status, direct_payload = router.dispatch("GET", target, b"")
        self.assertEqual(direct_status, 404)
        assert_problem(self, direct_payload, "not_found", False)
        expected = gateway_egress_attempt_counters()

        with tempfile.TemporaryDirectory() as directory:
            server = create_gateway_server(
                router,
                port=0,
                bearer_token=GATEWAY_TOKEN,
                token_path=Path(directory) / "gateway.token",
            )
            thread = start(server)
            port = server.server_address[1]
            try:
                status, _, unauthorized = request(
                    port,
                    GATEWAY_TOKEN,
                    target,
                    authorize=False,
                )
                self.assertEqual(status, 401)
                assert_problem(
                    self, unauthorized, "authentication_required", False
                )
                status, headers, payload = request(port, GATEWAY_TOKEN, target)
                wrong_method, _, wrong_method_payload = request(
                    port,
                    GATEWAY_TOKEN,
                    target,
                    method="POST",
                    payload=b"{}",
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

        self.assertEqual(status, 200)
        self.assertEqual(wrong_method, 405)
        assert_problem(
            self,
            wrong_method_payload,
            "method_not_allowed",
            False,
        )
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(
            payload,
            {
                "apiVersion": "gateway-internal-diagnostics/v1",
                "object": "gateway.egressAttempts",
                "providerNetworkAttempts": expected.provider_network_attempts,
                "providerCredentialReadAttempts": (
                    expected.provider_credential_read_attempts
                ),
            },
        )
        self.assertEqual(
            set(payload),
            {
                "apiVersion",
                "object",
                "providerNetworkAttempts",
                "providerCredentialReadAttempts",
            },
        )
        schema = json.loads(
            (ROOT / "openusage_bar/resources/gateway-api-v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertNotIn(target, json.dumps(schema))

    def test_health_exposes_only_mode_accurate_public_capabilities(self) -> None:
        cases = (
            (GatewayMode.OBSERVE, policy, proxy, "disabled", False, False),
            (GatewayMode.ADVISE, policy, proxy, "ok", True, False),
            (GatewayMode.GATEWAY, policy, None, "ok", True, False),
            (GatewayMode.GATEWAY, policy, proxy, "ok", True, True),
        )
        actual: list[tuple[int, dict[str, object]]] = []
        expected: list[tuple[int, dict[str, object]]] = []
        for mode, policy_value, proxy_value, status_value, should_send, responses in cases:
            router = GatewayRouter(
                mode=mode,
                policy=policy_value,
                proxy=proxy_value,
            )
            result = router.dispatch("GET", "/gateway/v1/health", b"")
            actual.append(result)
            expected.append(
                (
                    200,
                    {
                        "apiVersion": "gateway.openusage/v1",
                        "status": status_value,
                        "mode": mode.value,
                        "capabilities": {
                            "shouldSend": should_send,
                            "responses": responses,
                        },
                    },
                )
            )

        serialized = json.dumps(actual, sort_keys=True).lower()
        self.assertNotIn("token", serialized)
        self.assertNotIn("credential", serialized)
        self.assertNotIn("path", serialized)
        self.assertEqual(actual, expected)

    def test_modes_fail_closed_before_policy_or_proxy_dispatch(self) -> None:
        calls: list[str] = []

        def called_policy(_request: ShouldSendRequest) -> ShouldSendDecision:
            calls.append("policy")
            return DECISION

        def called_proxy(_payload: dict[str, object]) -> dict[str, object]:
            calls.append("proxy")
            return {}

        observe = GatewayRouter(
            mode=GatewayMode.OBSERVE,
            policy=called_policy,
            proxy=called_proxy,
        )
        status, payload = observe.dispatch(
            "POST", "/gateway/v1/should-send", body()
        )
        self.assertEqual(status, 404)
        assert_problem(self, payload, "gateway_disabled", False)

        advise = GatewayRouter(
            mode=GatewayMode.ADVISE,
            policy=called_policy,
            proxy=called_proxy,
        )
        status, payload = advise.dispatch(
            "POST", "/gateway/v1/responses", b"{}"
        )
        self.assertEqual(status, 403)
        assert_problem(self, payload, "proxy_disabled", False)
        self.assertEqual(calls, [])

    def test_schema_is_the_committed_gateway_only_manifest(self) -> None:
        router = GatewayRouter(mode=GatewayMode.ADVISE, policy=policy, proxy=None)
        status, payload = router.dispatch("GET", "/gateway/v1/schema", b"")
        expected = json.loads(
            (ROOT / "openusage_bar/resources/gateway-api-v1.schema.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload, expected)
        self.assertTrue(
            all(route.split(" ", 1)[1].startswith("/gateway/v1/") for route in payload["routes"])
        )

    def test_only_the_six_additive_method_and_path_pairs_dispatch(self) -> None:
        router = GatewayRouter(
            mode=GatewayMode.GATEWAY,
            policy=policy,
            proxy=proxy,
            account_pools=lambda: ACCOUNT_POOLS,
        )
        accepted = (
            ("GET", "/gateway/v1/account-pools", b""),
            ("GET", "/gateway/v1/decision-traces", b""),
            ("GET", "/gateway/v1/health", b""),
            ("GET", "/gateway/v1/schema", b""),
            ("POST", "/gateway/v1/should-send", body()),
            (
                "POST",
                "/gateway/v1/responses",
                body(
                    {
                        "provider": "openai",
                        "model": "gpt-4.1-mini",
                        "request": {
                            "model": "gpt-4.1-mini",
                            "input": "hello",
                            "stream": False,
                        },
                    }
                ),
            ),
        )
        for method, path, raw in accepted:
            with self.subTest(method=method, path=path):
                self.assertEqual(router.dispatch(method, path, raw)[0], 200)

        status, payload = router.dispatch("HEAD", "/gateway/v1/health", b"")
        self.assertEqual(status, 405)
        assert_problem(self, payload, "method_not_allowed", False)
        for path in ("/v1/health", "/gateway/v1/schema.json", "/gateway/v1/health/"):
            with self.subTest(path=path):
                status, payload = router.dispatch("GET", path, b"")
                self.assertEqual(status, 404)
                assert_problem(self, payload, "not_found", False)

    def test_account_pool_projection_is_optional_closed_and_renderer_safe(self) -> None:
        router = GatewayRouter(
            mode=GatewayMode.ADVISE,
            policy=policy,
            proxy=None,
            account_pools=lambda: ACCOUNT_POOLS,
        )

        status, payload = router.dispatch(
            "GET", "/gateway/v1/account-pools", b""
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload, ACCOUNT_POOLS)
        serialized = json.dumps(payload, sort_keys=True).casefold()
        for forbidden in ("credential", "account_id", "token", "path", "header"):
            self.assertNotIn(forbidden, serialized)

        unavailable = GatewayRouter(
            mode=GatewayMode.ADVISE,
            policy=policy,
            proxy=None,
        )
        self.assertEqual(
            unavailable.dispatch("GET", "/gateway/v1/account-pools", b""),
            (200, {"accounts": [], "pools": []}),
        )

        hostile = GatewayRouter(
            mode=GatewayMode.ADVISE,
            policy=policy,
            proxy=None,
            account_pools=lambda: {
                "accounts": [],
                "credential": "CANARY",
            },
        )
        status, payload = hostile.dispatch(
            "GET", "/gateway/v1/account-pools", b""
        )
        self.assertEqual(status, 500)
        assert_problem(self, payload, "internal_error", True)

    def test_post_requires_strict_utf8_json_object(self) -> None:
        router = GatewayRouter(mode=GatewayMode.ADVISE, policy=policy, proxy=None)
        for raw in (b"[1]", b"null", b"{", b'"text"', b"\xff"):
            with self.subTest(raw=raw):
                status, payload = router.dispatch(
                    "POST", "/gateway/v1/should-send", raw
                )
                self.assertEqual(status, 400)
                assert_problem(self, payload, "invalid_request", False)

    def test_should_send_rejects_nonstandard_or_ambiguous_json_without_policy(self) -> None:
        calls: list[ShouldSendRequest] = []

        def capture(value: ShouldSendRequest) -> ShouldSendDecision:
            calls.append(value)
            return DECISION

        router = GatewayRouter(mode=GatewayMode.ADVISE, policy=capture, proxy=None)
        invalid = (
            b'{"provider":"openai","model":"gpt-4o","estimated_tokens":NaN,"window":"5m"}',
            b'{"provider":"openai","model":"gpt-4o","estimated_tokens":Infinity,"window":"5m"}',
            b'{"provider":"openai","provider":"anthropic","model":"gpt-4o","estimated_tokens":1,"window":"5m"}',
            b'{"provider":"openai\\u0000","model":"gpt-4o","estimated_tokens":1,"window":"5m"}',
            b'{"provider":"   ","model":"gpt-4o","estimated_tokens":1,"window":"5m"}',
        )
        for raw in invalid:
            with self.subTest(raw=raw):
                status, payload = router.dispatch(
                    "POST", "/gateway/v1/should-send", raw
                )
                self.assertEqual(status, 400)
                assert_problem(self, payload, "invalid_request", False)
        self.assertEqual(calls, [])

    def test_gateway_mode_without_proxy_reports_proxy_disabled(self) -> None:
        router = GatewayRouter(
            mode=GatewayMode.GATEWAY,
            policy=policy,
            proxy=None,
        )
        status, payload = router.dispatch(
            "POST", "/gateway/v1/responses", b"{}"
        )

        self.assertEqual(status, 403)
        assert_problem(self, payload, "proxy_disabled", False)

    def test_should_send_shape_and_integer_bounds_are_strict(self) -> None:
        captured: list[ShouldSendRequest] = []

        def capture(value: ShouldSendRequest) -> ShouldSendDecision:
            captured.append(value)
            return DECISION

        router = GatewayRouter(mode=GatewayMode.ADVISE, policy=capture, proxy=None)
        invalid = (
            {},
            {key: value for key, value in VALID_REQUEST.items() if key != "window"},
            {**VALID_REQUEST, "unexpected": True},
            {**VALID_REQUEST, "estimated_tokens": True},
            {**VALID_REQUEST, "estimated_tokens": 0},
            {**VALID_REQUEST, "estimated_tokens": 2**31},
        )
        for payload_value in invalid:
            with self.subTest(payload=payload_value):
                status, payload = router.dispatch(
                    "POST", "/gateway/v1/should-send", body(payload_value)
                )
                self.assertEqual(status, 400)
                assert_problem(self, payload, "invalid_request", False)
        self.assertEqual(captured, [])

        for estimated_tokens in (1, 2**31 - 1):
            status, _ = router.dispatch(
                "POST",
                "/gateway/v1/should-send",
                body({**VALID_REQUEST, "estimated_tokens": estimated_tokens}),
            )
            self.assertEqual(status, 200)
        self.assertEqual(
            [item.estimated_tokens for item in captured],
            [1, 2**31 - 1],
        )

    def test_router_rejects_more_than_four_mib_before_policy_dispatch(self) -> None:
        calls: list[ShouldSendRequest] = []

        def capture(value: ShouldSendRequest) -> ShouldSendDecision:
            calls.append(value)
            return DECISION

        router = GatewayRouter(mode=GatewayMode.ADVISE, policy=capture, proxy=None)
        status, payload = router.dispatch(
            "POST", "/gateway/v1/should-send", b"{" + b"x" * (4 * 1024 * 1024)
        )

        self.assertEqual(status, 413)
        assert_problem(self, payload, "request_too_large", False)
        self.assertEqual(calls, [])

    def test_policy_result_uses_the_frozen_snake_case_wire_shape(self) -> None:
        router = GatewayRouter(mode=GatewayMode.ADVISE, policy=policy, proxy=None)
        status, payload = router.dispatch(
            "POST", "/gateway/v1/should-send", body()
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {
                "decision": "defer",
                "confidence": 0.8,
                "reason": "quota_low",
                "defer_until": None,
                "details": {
                    "quota_remaining": 0.12,
                    "burn_rate_per_min": 10.0,
                    "predicted_exhaustion_minutes": 120.0,
                },
            },
        )

    def test_defer_until_uses_the_closed_rfc3339_timestamp_subset(self) -> None:
        valid = (
            "2026-08-09T10:15Z",
            "2026-08-09T10:15+08:00",
            "2026-08-09T10:15:30Z",
            "2026-08-09T10:15:30.1Z",
            "2026-08-09T10:15:30.123456-05:30",
        )
        invalid = (
            "2026-08-09 10:15:00+00:00",
            "20260809T101500+0000",
            "2026-02-29T10:15:00Z",
            "2026-08-09T24:00:00Z",
            "2026-08-09T10:15:00+24:00",
            "2026-08-09T10:15:00",
            "2026-08-09T10:15:30.1234567Z",
            "2026-08-09T10:15:30,1Z",
            "2026-08-09T10:15:30+08:00:01",
        )

        def dispatch(defer_until: str) -> tuple[int, dict[str, object]]:
            decision = ShouldSendDecision(
                decision=Decision.DEFER,
                confidence=0.8,
                reason="quota_low",
                defer_until=defer_until,
                quota_remaining=None,
                burn_rate_per_minute=None,
                predicted_exhaustion_minutes=None,
            )
            router = GatewayRouter(
                mode=GatewayMode.ADVISE,
                policy=lambda _request: decision,
                proxy=None,
            )
            return router.dispatch("POST", "/gateway/v1/should-send", body())

        for timestamp in valid:
            with self.subTest(valid=timestamp):
                status, payload = dispatch(timestamp)
                self.assertEqual(status, 200)
                self.assertEqual(payload["defer_until"], timestamp)

        for timestamp in invalid:
            with self.subTest(invalid=timestamp):
                status, payload = dispatch(timestamp)
                self.assertEqual(status, 500)
                assert_problem(self, payload, "internal_error", True)

    def test_only_defer_can_publish_a_defer_until_timestamp(self) -> None:
        timestamp = "2026-08-09T10:15:30Z"
        cases = (
            (Decision.YES, "quota_healthy"),
            (Decision.NO, "approaching_limit"),
        )
        for decision_value, reason in cases:
            with self.subTest(decision=decision_value.value):
                decision = ShouldSendDecision(
                    decision=decision_value,
                    confidence=0.8,
                    reason=reason,
                    defer_until=timestamp,
                    quota_remaining=None,
                    burn_rate_per_minute=None,
                    predicted_exhaustion_minutes=None,
                )
                router = GatewayRouter(
                    mode=GatewayMode.ADVISE,
                    policy=lambda _request: decision,
                    proxy=None,
                )

                status, payload = router.dispatch(
                    "POST", "/gateway/v1/should-send", body()
                )

                self.assertEqual(status, 200)
                self.assertIsNone(payload["defer_until"])

    def test_get_body_and_internal_exceptions_use_sanitized_errors(self) -> None:
        secret = "sk-secret /Users/private prompt-body"

        def failing_policy(_request: ShouldSendRequest) -> ShouldSendDecision:
            raise RuntimeError(secret)

        router = GatewayRouter(
            mode=GatewayMode.ADVISE,
            policy=failing_policy,
            proxy=None,
        )
        status, payload = router.dispatch("GET", "/gateway/v1/schema", b"x")
        self.assertEqual(status, 413)
        assert_problem(self, payload, "request_body_not_allowed", False)

        status, payload = router.dispatch(
            "POST", "/gateway/v1/should-send", body()
        )
        self.assertEqual(status, 500)
        assert_problem(self, payload, "internal_error", True)
        self.assertNotIn(secret, json.dumps(payload))


class GatewayTokenPublicationTests(unittest.TestCase):
    def test_windows_security_seam_hardens_existing_file_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "gateway.token"
            token_path.write_text(GATEWAY_TOKEN, encoding="ascii")
            if os.name != "nt":
                token_path.chmod(0o600)
            calls: list[tuple[str, object]] = []
            real_read = os.read

            class FakeWindowsFileSecurity:
                def harden_directory(self, parent: Path) -> None:
                    calls.append(("directory", parent))

                def harden_file(self, descriptor: int) -> None:
                    calls.append(("file", descriptor))

            def checked_read(descriptor: int, count: int) -> bytes:
                self.assertEqual(
                    calls,
                    [
                        ("directory", token_path.parent),
                        ("file", descriptor),
                    ],
                )
                return real_read(descriptor, count)

            with (
                patch.object(
                    gateway_server_module,
                    "_WINDOWS_FILE_SECURITY",
                    FakeWindowsFileSecurity(),
                ),
                patch.object(gateway_server_module.os, "read", checked_read),
            ):
                loaded = gateway_server_module._load_or_create_token(
                    token_path,
                    None,
                )

            self.assertEqual(loaded, GATEWAY_TOKEN)
            self.assertEqual(calls[0], ("directory", token_path.parent))
            self.assertEqual(calls[1][0], "file")
            self.assertEqual(len(calls), 2)

    def test_windows_file_hardening_failure_prevents_new_token_write(self) -> None:
        secret_detail = "security-detail-must-not-escape"
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "gateway.token"
            calls: list[tuple[str, object]] = []

            class FailingWindowsFileSecurity:
                def harden_directory(self, parent: Path) -> None:
                    calls.append(("directory", parent))

                def harden_file(self, descriptor: int) -> None:
                    calls.append(("file", descriptor))
                    raise OSError(f"{secret_detail}: {token_path}")

            def forbidden_write(_descriptor: int, _content: object) -> int:
                raise AssertionError("token bytes were written before ACL hardening")

            with (
                patch.object(
                    gateway_server_module,
                    "_WINDOWS_FILE_SECURITY",
                    FailingWindowsFileSecurity(),
                ),
                patch.object(gateway_server_module.os, "write", forbidden_write),
                self.assertRaises(OSError) as caught,
            ):
                gateway_server_module._load_or_create_token(
                    token_path,
                    GATEWAY_TOKEN,
                )

            self.assertEqual(
                str(caught.exception),
                "temporary Gateway token file is unsafe",
            )
            self.assertNotIn(secret_detail, str(caught.exception))
            self.assertNotIn(str(token_path), str(caught.exception))
            self.assertFalse(token_path.exists())
            self.assertFalse(any(token_path.parent.iterdir()))
            self.assertEqual(calls[0], ("directory", token_path.parent))
            self.assertEqual(calls[1][0], "file")
            self.assertEqual(len(calls), 2)

    def test_token_path_is_published_only_after_complete_private_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "gateway.token"
            write_started = threading.Event()
            allow_write = threading.Event()
            created: list[bool] = []
            errors: list[Exception] = []
            real_write = os.write

            def blocking_write(descriptor, content):
                write_started.set()
                if not allow_write.wait(2):
                    raise TimeoutError("test write was not released")
                return real_write(descriptor, content)

            def create() -> None:
                try:
                    created.append(
                        gateway_server_module._create_token(token_path, GATEWAY_TOKEN)
                    )
                except Exception as error:
                    errors.append(error)

            with patch.object(gateway_server_module.os, "write", blocking_write):
                worker = threading.Thread(target=create)
                worker.start()
                self.assertTrue(write_started.wait(2))
                try:
                    self.assertFalse(token_path.exists())
                finally:
                    allow_write.set()
                    worker.join(2)

            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(created, [True])
            self.assertEqual(token_path.read_text(encoding="ascii"), GATEWAY_TOKEN)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)

    def test_failed_partial_write_leaves_no_final_token_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "gateway.token"
            real_write = os.write
            calls = 0

            def partial_then_fail(descriptor, content):
                nonlocal calls
                calls += 1
                if calls == 1:
                    prefix = max(1, len(content) // 2)
                    return real_write(descriptor, content[:prefix])
                raise OSError("synthetic Gateway token write failure")

            with patch.object(
                gateway_server_module.os,
                "write",
                partial_then_fail,
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "synthetic Gateway token write failure",
                ):
                    gateway_server_module._create_token(token_path, GATEWAY_TOKEN)

            self.assertFalse(token_path.exists())

    def test_initial_metadata_failure_leaves_only_private_empty_orphan(self) -> None:
        failure = "synthetic Gateway token metadata failure"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_path = root / "gateway.token"

            with patch.object(
                gateway_server_module.os,
                "fstat",
                side_effect=OSError(failure),
            ):
                with self.assertRaises(OSError) as caught:
                    gateway_server_module._create_token(
                        token_path,
                        GATEWAY_TOKEN,
                    )

            self.assertFalse(token_path.exists())
            residuals = list(root.iterdir())
            self.assertLessEqual(
                len(residuals),
                1,
                "more than one temporary Gateway token was left behind",
            )
            for residual in residuals:
                metadata = residual.lstat()
                self.assertTrue(
                    stat.S_ISREG(metadata.st_mode),
                    "residual temporary Gateway token is not regular",
                )
                self.assertEqual(
                    metadata.st_size,
                    0,
                    "residual temporary Gateway token is not empty",
                )
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
            self.assertEqual(str(caught.exception), failure)

    @unittest.skipIf(os.name == "nt", "Windows locks the open publication node")
    def test_initial_metadata_failure_preserves_replacement_node(self) -> None:
        failure = "synthetic Gateway token metadata failure"
        replacement = b"replacement-node"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_path = root / "gateway.token"
            candidates: list[Path] = []
            replacement_identity: tuple[int, int] | None = None
            fstat_calls = 0
            real_open = os.open
            real_fstat = os.fstat

            def capture_candidate(target, flags, *args, **kwargs):
                descriptor = real_open(target, flags, *args, **kwargs)
                candidate = Path(target)
                if (
                    not candidates
                    and candidate.parent == root
                    and ".tmp-" in candidate.name
                ):
                    candidates.append(candidate)
                return descriptor

            def replace_then_fail(descriptor):
                nonlocal fstat_calls, replacement_identity
                fstat_calls += 1
                if fstat_calls == 1:
                    if len(candidates) != 1:
                        raise AssertionError("temporary candidate was not captured")
                    candidate = candidates[0]
                    candidate.unlink()
                    candidate.write_bytes(replacement)
                    if os.name != "nt":
                        candidate.chmod(0o600)
                    metadata = candidate.stat()
                    replacement_identity = (metadata.st_dev, metadata.st_ino)
                    raise OSError(failure)
                return real_fstat(descriptor)

            with (
                patch.object(gateway_server_module.os, "open", capture_candidate),
                patch.object(gateway_server_module.os, "fstat", replace_then_fail),
            ):
                with self.assertRaises(OSError) as caught:
                    gateway_server_module._create_token(
                        token_path,
                        GATEWAY_TOKEN,
                    )

            self.assertEqual(str(caught.exception), failure)
            self.assertFalse(token_path.exists())
            self.assertEqual(
                len(candidates),
                1,
                "temporary Gateway candidate was not captured exactly once",
            )
            candidate = candidates[0]
            self.assertTrue(candidate.exists(), "replacement node was deleted")
            self.assertEqual(candidate.read_bytes(), replacement)
            metadata = candidate.stat()
            self.assertEqual(
                (metadata.st_dev, metadata.st_ino),
                replacement_identity,
            )

    def test_primary_write_failure_wins_over_secondary_close_failure(self) -> None:
        primary = "primary write failure"
        secondary = "secondary close failure"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_path = root / "gateway.token"
            real_close = os.close
            close_calls = 0

            def fail_write(_descriptor, _content):
                raise OSError(primary)

            def close_then_fail(descriptor):
                nonlocal close_calls
                close_calls += 1
                real_close(descriptor)
                if close_calls == 1:
                    raise OSError(secondary)

            with (
                patch.object(gateway_server_module.os, "write", fail_write),
                patch.object(gateway_server_module.os, "close", close_then_fail),
            ):
                with self.assertRaises(OSError) as caught:
                    gateway_server_module._create_token(
                        token_path,
                        GATEWAY_TOKEN,
                    )

            self.assertFalse(token_path.exists())
            self.assertFalse(any(root.iterdir()), "owned token node was not cleaned")
            self.assertEqual(str(caught.exception), primary)

    @unittest.skipIf(os.name == "nt", "directory fsync is unavailable on Windows")
    def test_parent_fsync_failure_wins_over_parent_close_failure(self) -> None:
        primary = "primary parent fsync failure"
        secondary = "secondary parent close failure"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_path = root / "gateway.token"
            parent_descriptor: int | None = None
            real_fstat = os.fstat
            real_fsync = os.fsync
            real_close = os.close

            def fail_parent_fsync(descriptor):
                nonlocal parent_descriptor
                if stat.S_ISDIR(real_fstat(descriptor).st_mode):
                    parent_descriptor = descriptor
                    raise OSError(primary)
                return real_fsync(descriptor)

            def close_parent_then_fail(descriptor):
                real_close(descriptor)
                if descriptor == parent_descriptor:
                    raise OSError(secondary)

            with (
                patch.object(gateway_server_module.os, "fsync", fail_parent_fsync),
                patch.object(gateway_server_module.os, "close", close_parent_then_fail),
            ):
                with self.assertRaises(OSError) as caught:
                    gateway_server_module._create_token(
                        token_path,
                        GATEWAY_TOKEN,
                    )

            self.assertIsNotNone(parent_descriptor, "parent fsync probe did not run")
            self.assertFalse(token_path.exists())
            self.assertFalse(any(root.iterdir()), "owned token nodes were not cleaned")
            self.assertEqual(str(caught.exception), primary)

    def test_read_token_and_server_reject_inode_swap(self) -> None:
        unsafe = "existing Gateway token file is unsafe"
        replacement_token = "r" * 48
        router = GatewayRouter(mode=GatewayMode.ADVISE, policy=policy, proxy=None)

        for through_server in (False, True):
            with self.subTest(through_server=through_server):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    token_path = root / "gateway.token"
                    source_path = root / "source.token"
                    token_path.write_text(GATEWAY_TOKEN, encoding="ascii")
                    if os.name != "nt":
                        token_path.chmod(0o600)
                    original = token_path.stat()
                    replacement_identity: tuple[int, int] | None = None
                    swapped = False
                    real_open = os.open
                    server = None
                    caught: OSError | None = None

                    def swap_then_open(target, flags, *args, **kwargs):
                        nonlocal replacement_identity, swapped
                        opened_path = Path(target)
                        if not swapped and opened_path == token_path:
                            swapped = True
                            opened_path.rename(source_path)
                            opened_path.write_text(
                                replacement_token,
                                encoding="ascii",
                            )
                            if os.name != "nt":
                                opened_path.chmod(0o600)
                            metadata = opened_path.stat()
                            replacement_identity = (
                                metadata.st_dev,
                                metadata.st_ino,
                            )
                        return real_open(target, flags, *args, **kwargs)

                    try:
                        with patch.object(
                            gateway_server_module.os,
                            "open",
                            swap_then_open,
                        ):
                            try:
                                if through_server:
                                    server = create_gateway_server(
                                        router,
                                        port=0,
                                        token_path=token_path,
                                    )
                                else:
                                    gateway_server_module._read_token(token_path)
                            except OSError as error:
                                caught = error
                    finally:
                        if server is not None:
                            server.server_close()

                    self.assertTrue(swapped, "token inode swap did not run")
                    source = source_path.stat()
                    replacement = token_path.stat()
                    self.assertEqual(
                        (source.st_dev, source.st_ino),
                        (original.st_dev, original.st_ino),
                    )
                    self.assertEqual(
                        source_path.read_text(encoding="ascii"),
                        GATEWAY_TOKEN,
                    )
                    self.assertEqual(
                        (replacement.st_dev, replacement.st_ino),
                        replacement_identity,
                    )
                    self.assertEqual(
                        token_path.read_text(encoding="ascii"),
                        replacement_token,
                    )
                    self.assertIsNotNone(caught, "inode swap was accepted")
                    assert caught is not None
                    self.assertEqual(str(caught), unsafe)

    def test_close_failure_after_complete_write_cleans_every_token_path(self) -> None:
        failure = "synthetic Gateway token close failure"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_path = root / "gateway.token"
            real_close = os.close
            calls = 0

            def close_then_fail(descriptor):
                nonlocal calls
                calls += 1
                if calls == 1:
                    real_close(descriptor)
                    raise OSError(failure)
                return real_close(descriptor)

            with patch.object(gateway_server_module.os, "close", close_then_fail):
                with self.assertRaises(OSError) as caught:
                    gateway_server_module._create_token(
                        token_path,
                        GATEWAY_TOKEN,
                    )

            self.assertFalse(
                any(root.iterdir()),
                "temporary Gateway token was not cleaned",
            )
            self.assertEqual(str(caught.exception), failure)

    def test_existing_token_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "gateway.token"
            existing = "e" * 48
            token_path.write_text(existing, encoding="ascii")
            if os.name != "nt":
                token_path.chmod(0o600)

            self.assertFalse(
                gateway_server_module._create_token(token_path, GATEWAY_TOKEN)
            )
            self.assertEqual(token_path.read_text(encoding="ascii"), existing)


class GatewayHTTPTests(unittest.TestCase):
    def test_server_close_releases_a_closable_router_proxy_once(self) -> None:
        class ClosableProxy:
            def __init__(self) -> None:
                self.close_calls = 0

            def __call__(self, payload):
                return proxy(payload)

            def close(self) -> None:
                self.close_calls += 1

        closable = ClosableProxy()
        router = GatewayRouter(
            mode=GatewayMode.GATEWAY,
            policy=policy,
            proxy=closable,
        )
        with tempfile.TemporaryDirectory() as directory:
            server = create_gateway_server(
                router,
                port=0,
                token_path=Path(directory) / "gateway.token",
            )

            server.server_close()
            server.server_close()

        self.assertEqual(closable.close_calls, 1)

    def test_token_path_and_existing_aliases_fail_closed(self) -> None:
        router = GatewayRouter(mode=GatewayMode.ADVISE, policy=policy, proxy=None)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relative = Path("relative.token")
            with self.assertRaises(ValueError):
                create_gateway_server(router, port=0, token_path=relative)
            self.assertFalse((root / "relative.token").exists())

            target = root / "target.token"
            target.write_text(GATEWAY_TOKEN, encoding="ascii")
            if os.name != "nt":
                target.chmod(0o600)

            hardlink = root / "hardlink.token"
            os.link(target, hardlink)
            with self.assertRaises(OSError):
                create_gateway_server(router, port=0, token_path=hardlink)
            hardlink.unlink()

            symlink = root / "symlink.token"
            symlink.symlink_to(target)
            with self.assertRaises(OSError):
                create_gateway_server(router, port=0, token_path=symlink)

            mismatch = root / "mismatch.token"
            mismatch.write_text("x" * 48, encoding="ascii")
            if os.name != "nt":
                mismatch.chmod(0o600)
            with self.assertRaises(OSError):
                create_gateway_server(
                    router,
                    port=0,
                    bearer_token=GATEWAY_TOKEN,
                    token_path=mismatch,
                )

    def test_thread_and_timeout_boundaries_validate_before_token_creation(self) -> None:
        router = GatewayRouter(mode=GatewayMode.ADVISE, policy=policy, proxy=None)
        invalid = (
            {"max_threads": 0},
            {"max_threads": 257},
            {"client_timeout": 0.09},
            {"client_timeout": 60.01},
            {"request_deadline": 0.04},
            {"request_deadline": 300.01},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, options in enumerate(invalid):
                token_path = root / f"invalid-{index}" / "gateway.token"
                with self.subTest(options=options), self.assertRaises(ValueError):
                    create_gateway_server(
                        router,
                        port=0,
                        token_path=token_path,
                        **options,
                    )
                self.assertFalse(token_path.exists())

            accepted = (
                {"max_threads": 1},
                {"max_threads": 256},
                {"client_timeout": 0.1},
                {"client_timeout": 60},
                {"request_deadline": 0.05},
                {"request_deadline": 300},
            )
            for index, options in enumerate(accepted):
                with self.subTest(options=options):
                    server = create_gateway_server(
                        router,
                        port=0,
                        token_path=root / f"valid-{index}" / "gateway.token",
                        **options,
                    )
                    server.server_close()

    def test_slow_connection_is_closed_and_deadline_state_is_cleaned(self) -> None:
        router = GatewayRouter(mode=GatewayMode.ADVISE, policy=policy, proxy=None)
        with tempfile.TemporaryDirectory() as directory:
            server = create_gateway_server(
                router,
                port=0,
                token_path=Path(directory) / "gateway.token",
                max_threads=1,
                client_timeout=1,
                request_deadline=0.15,
            )
            thread = start(server)
            peer = socket.create_connection(server.server_address, timeout=1)
            started = time.monotonic()
            try:
                peer.sendall(b"GET /gateway/v1/health HTTP/1.1\r\n")
                while server.active_deadline_count == 0 and time.monotonic() - started < 1:
                    time.sleep(0.005)
                self.assertEqual(server.active_deadline_count, 1)
                while server.active_deadline_count and time.monotonic() - started < 1:
                    time.sleep(0.005)
                self.assertEqual(server.active_deadline_count, 0)
                peer.settimeout(1)
                self.assertEqual(peer.recv(1), b"")

                recovery_deadline = time.monotonic() + 1
                recovered_status: int | None = None
                while time.monotonic() < recovery_deadline:
                    try:
                        recovered_status, _, _ = request(
                            server.server_address[1],
                            server.bearer_token,
                        )
                        break
                    except (OSError, http.client.HTTPException):
                        time.sleep(0.005)
                self.assertEqual(recovered_status, 200)
            finally:
                peer.close()
                server.shutdown()
                server.server_close()
                thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(server.active_deadline_count, 0)
            self.assertLess(time.monotonic() - started, 2)

    def test_accepted_socket_timeout_never_exceeds_the_absolute_deadline(
        self,
    ) -> None:
        router = GatewayRouter(mode=GatewayMode.ADVISE, policy=policy, proxy=None)
        with tempfile.TemporaryDirectory() as directory:
            server = create_gateway_server(
                router,
                port=0,
                token_path=Path(directory) / "gateway.token",
                client_timeout=1,
                request_deadline=0.15,
            )
            peer = socket.create_connection(server.server_address, timeout=1)
            accepted: socket.socket | None = None
            try:
                accepted, _ = server.get_request()
                self.assertEqual(accepted.gettimeout(), 0.15)
            finally:
                peer.close()
                if accepted is not None:
                    accepted.close()
                server.server_close()

    def test_expired_deadline_remains_active_until_worker_releases_its_slot(
        self,
    ) -> None:
        router = GatewayRouter(mode=GatewayMode.ADVISE, policy=policy, proxy=None)
        finish_entered = threading.Event()
        allow_finish = threading.Event()
        original_finish = gateway_server_module._GatewayHandler.finish

        def blocked_finish(handler: object) -> None:
            finish_entered.set()
            allow_finish.wait(2)
            original_finish(handler)

        with tempfile.TemporaryDirectory() as directory, patch.object(
            gateway_server_module._GatewayHandler,
            "finish",
            blocked_finish,
        ):
            server = create_gateway_server(
                router,
                port=0,
                token_path=Path(directory) / "gateway.token",
                max_threads=1,
                client_timeout=1,
                request_deadline=0.05,
            )
            thread = start(server)
            peer = socket.create_connection(server.server_address, timeout=1)
            try:
                peer.sendall(b"GET /gateway/v1/health HTTP/1.1\r\n")
                self.assertTrue(finish_entered.wait(1))
                self.assertEqual(server.active_deadline_count, 1)
                self.assertFalse(server._thread_slots.acquire(blocking=False))

                allow_finish.set()
                deadline = time.monotonic() + 1
                while server.active_deadline_count and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(server.active_deadline_count, 0)
                status, _, _ = request(
                    server.server_address[1],
                    server.bearer_token,
                )
                self.assertEqual(status, 200)
            finally:
                allow_finish.set()
                peer.close()
                server.shutdown()
                server.server_close()
                thread.join(1)
            self.assertFalse(thread.is_alive())

    def test_fast_requests_share_server_watchdog_without_per_request_timers(
        self,
    ) -> None:
        router = GatewayRouter(mode=GatewayMode.ADVISE, policy=policy, proxy=None)
        with tempfile.TemporaryDirectory() as directory:
            server = create_gateway_server(
                router,
                port=0,
                token_path=Path(directory) / "gateway.token",
            )
            thread = start(server)
            timer_starts: list[threading.Timer] = []
            original_start = gateway_server_module.threading.Timer.start

            def record_start(timer: threading.Timer) -> None:
                timer_starts.append(timer)
                original_start(timer)

            try:
                with patch.object(
                    gateway_server_module.threading.Timer,
                    "start",
                    record_start,
                ):
                    for _ in range(3):
                        status, _, _ = request(
                            server.server_address[1],
                            server.bearer_token,
                        )
                        self.assertEqual(status, 200)
                self.assertEqual(timer_starts, [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(server.active_deadline_count, 0)

    def test_server_close_interrupts_active_reader_and_stops_watchdog(self) -> None:
        router = GatewayRouter(mode=GatewayMode.ADVISE, policy=policy, proxy=None)
        with tempfile.TemporaryDirectory() as directory:
            server = create_gateway_server(
                router,
                port=0,
                token_path=Path(directory) / "gateway.token",
                max_threads=1,
                client_timeout=30,
                request_deadline=30,
            )
            thread = start(server)
            peer = socket.create_connection(server.server_address, timeout=1)
            try:
                peer.sendall(b"GET /gateway/v1/health HTTP/1.1\r\n")
                deadline = time.monotonic() + 1
                while server.active_deadline_count == 0 and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.assertEqual(server.active_deadline_count, 1)

                server.shutdown()
                server.server_close()
                thread.join(1)

                peer.settimeout(1)
                self.assertEqual(peer.recv(1), b"")
                self.assertEqual(server.active_deadline_count, 0)
                self.assertFalse(server._deadline_watchdog.is_alive())
            finally:
                peer.close()
                if thread.is_alive():
                    server.shutdown()
                server.server_close()
                thread.join(1)
            self.assertFalse(thread.is_alive())

    def test_loopback_host_auth_and_body_bound_precede_dispatch(self) -> None:
        calls: list[ShouldSendRequest] = []

        def capture(value: ShouldSendRequest) -> ShouldSendDecision:
            calls.append(value)
            return DECISION

        router = GatewayRouter(mode=GatewayMode.ADVISE, policy=capture, proxy=None)
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "gateway.token"
            server = create_gateway_server(router, port=0, token_path=token_path)
            thread = start(server)
            try:
                host, port = server.server_address
                self.assertEqual(host, "127.0.0.1")
                self.assertEqual(server.address_family, socket.AF_INET)
                self.assertGreaterEqual(len(server.bearer_token), 43)
                self.assertEqual(token_path.read_text(encoding="ascii"), server.bearer_token)
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)

                status, _, payload = request(
                    port,
                    server.bearer_token,
                    host=f"localhost:{port}",
                )
                self.assertEqual(status, 403)
                assert_problem(self, payload, "forbidden_host", False)

                status, _, payload = request(
                    port,
                    server.bearer_token,
                    "/gateway/v1/should-send",
                    method="POST",
                    payload=b"{secret-body",
                    authorize=False,
                )
                self.assertEqual(status, 401)
                assert_problem(self, payload, "authentication_required", False)
                self.assertNotIn("secret-body", json.dumps(payload))
                self.assertEqual(calls, [])

                peer = socket.create_connection(("127.0.0.1", port), timeout=2)
                peer.settimeout(2)
                try:
                    peer.sendall(
                        (
                            "POST /gateway/v1/should-send HTTP/1.1\r\n"
                            f"Host: 127.0.0.1:{port}\r\n"
                            f"Authorization: Bearer {server.bearer_token}\r\n"
                            "Content-Type: application/json\r\n"
                            f"Content-Length: {4 * 1024 * 1024 + 1}\r\n\r\n"
                        ).encode("ascii")
                    )
                    response = http.client.HTTPResponse(peer)
                    response.begin()
                    oversized = json.loads(response.read())
                    self.assertEqual(response.status, 413)
                    assert_problem(self, oversized, "request_too_large", False)
                    self.assertEqual(calls, [])
                finally:
                    peer.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

    def test_admission_rate_limit_refills_and_server_shuts_down(self) -> None:
        now = [100.0]
        router = GatewayRouter(mode=GatewayMode.ADVISE, policy=policy, proxy=None)
        with tempfile.TemporaryDirectory() as directory:
            server = create_gateway_server(
                router,
                port=0,
                bearer_token=GATEWAY_TOKEN,
                token_path=Path(directory) / "gateway.token",
                rate_limit_capacity=2,
                rate_limit_refill_per_second=1.0,
                monotonic=lambda: now[0],
            )
            thread = start(server)
            port = server.server_address[1]
            try:
                statuses = [
                    request(
                        port,
                        GATEWAY_TOKEN,
                        "/gateway/v1/should-send",
                        method="POST",
                        payload=body(),
                    )[0]
                    for _ in range(2)
                ]
                self.assertEqual(statuses, [200, 200])
                status, headers, payload = request(
                    port,
                    GATEWAY_TOKEN,
                    "/gateway/v1/should-send",
                    method="POST",
                    payload=body(),
                )
                self.assertEqual(status, 429)
                self.assertEqual(headers["retry-after"], "1")
                assert_problem(self, payload, "rate_limited", True)

                now[0] += 1.0
                self.assertEqual(
                    request(
                        port,
                        GATEWAY_TOKEN,
                        "/gateway/v1/should-send",
                        method="POST",
                        payload=body(),
                    )[0],
                    200,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

    def test_responses_can_emit_gateway_native_sse_without_provider_headers(self) -> None:
        def streaming_proxy(_payload: dict[str, object]) -> dict[str, object]:
            return {
                "apiVersion": "gateway.openusage/v1",
                "object": "gateway.response",
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "stream": True,
                "status": "complete",
                "outputText": "hello over gateway sse",
                "usage": {"inputTokens": 1, "outputTokens": 2},
                "tool": {"observed": False},
                "cache": {"outcome": "disabled"},
                "fallback": {
                    "attempted": False,
                    "attemptCount": 1,
                    "finalAction": "none",
                },
            }

        router = GatewayRouter(
            mode=GatewayMode.GATEWAY,
            policy=policy,
            proxy=streaming_proxy,
        )
        payload = body(
            {
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "request": {
                    "model": "gpt-4.1-mini",
                    "input": "hello",
                    "stream": True,
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            server = create_gateway_server(
                router,
                port=0,
                bearer_token=GATEWAY_TOKEN,
                token_path=Path(directory) / "gateway.token",
            )
            thread = start(server)
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_address[1], timeout=2
                )
                connection.request(
                    "POST",
                    "/gateway/v1/responses",
                    body=payload,
                    headers={
                        "Host": f"127.0.0.1:{server.server_address[1]}",
                        "Authorization": f"Bearer {GATEWAY_TOKEN}",
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    },
                )
                response = connection.getresponse()
                raw = response.read()
                headers = {
                    name.lower(): value for name, value in response.getheaders()
                }
                connection.close()

                self.assertEqual(response.status, 200)
                self.assertEqual(
                    headers["content-type"],
                    "text/event-stream; charset=utf-8",
                )
                with self.subTest(gate="unbuffered transport"):
                    self.assertNotIn(
                        "content-length",
                        headers,
                        "Gateway SSE must not be materialized as one buffered body",
                    )
                self.assertNotIn("x-request-id", headers)
                events: list[tuple[str, dict[str, object]]] = []
                for block in raw.split(b"\n\n"):
                    if not block:
                        continue
                    lines = block.splitlines()
                    self.assertEqual(len(lines), 2)
                    self.assertTrue(lines[0].startswith(b"event: "))
                    self.assertTrue(lines[1].startswith(b"data: "))
                    wire_name = lines[0].removeprefix(b"event: ").decode("ascii")
                    data = json.loads(
                        lines[1].removeprefix(b"data: ").decode("utf-8")
                    )
                    self.assertIs(type(data), dict)
                    events.append((wire_name, data))

                failures: list[str] = []
                names = [name for name, _ in events]
                allowed_names = {
                    "gateway.response.started",
                    "gateway.cache.event",
                    "gateway.output_text.delta",
                    "gateway.response.completed",
                }
                if (
                    not names
                    or names[0] != "gateway.response.started"
                    or names[-1] != "gateway.response.completed"
                    or names.count("gateway.output_text.delta") != 1
                    or any(name not in allowed_names for name in names)
                ):
                    failures.append("SSE event order is outside the frozen v1 union")
                for sequence, (wire_name, data) in enumerate(events):
                    if wire_name != data.get("type"):
                        failures.append(f"event {sequence} wire name != data.type")
                    if not wire_name.startswith("gateway."):
                        failures.append(f"event {sequence} is not Gateway-native")
                    if data.get("apiVersion") != "gateway.openusage/v1":
                        failures.append(f"event {sequence} lacks apiVersion")
                    if data.get("sequence") != sequence:
                        failures.append(f"event {sequence} lacks monotonic sequence")

                if events:
                    started = events[0][1]
                    if (
                        started.get("provider") != "openai"
                        or started.get("model") != "gpt-4.1-mini"
                        or started.get("stream") is not True
                    ):
                        failures.append("started event lacks accepted provider/model")
                    deltas = [
                        data
                        for _, data in events
                        if data.get("type") == "gateway.output_text.delta"
                    ]
                    if (
                        len(deltas) != 1
                        or deltas[0].get("text") != "hello over gateway sse"
                    ):
                        failures.append("delta text differs from JSON outputText")
                    terminal = events[-1][1]
                    expected_terminal = {
                        "provider": "openai",
                        "model": "gpt-4.1-mini",
                        "status": "complete",
                        "usage": {"inputTokens": 1, "outputTokens": 2},
                        "tool": {"observed": False},
                        "cache": {"outcome": "disabled"},
                        "fallback": {
                            "attempted": False,
                            "attemptCount": 1,
                            "finalAction": "none",
                        },
                    }
                    for name, value in expected_terminal.items():
                        if terminal.get(name) != value:
                            failures.append(f"terminal {name} is not authoritative")
                with self.subTest(gate="Gateway-native SSE v1"):
                    self.assertEqual(failures, [])
                self.assertNotIn(b"response.output_text.delta", raw)
                self.assertNotIn(b"providerHeaders", raw)
                self.assertNotIn(b"providerRequestId", raw)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_invalid_response_requests_ignore_sse_accept_and_keep_problem_json(
        self,
    ) -> None:
        from openusage_bar.gateway.runtime import GatewayRuntime

        provider_calls: list[str] = []

        def egress(provider_id: str, _request_body: bytes, **_kwargs):
            provider_calls.append(provider_id)
            raise AssertionError("invalid requests must not reach Provider egress")

        router = GatewayRouter(
            mode=GatewayMode.GATEWAY,
            policy=policy,
            proxy=GatewayRuntime(egress=egress),
        )
        cases = {
            "invalid JSON": b"{",
            "invalid Gateway envelope": b"{}",
        }
        with tempfile.TemporaryDirectory() as directory:
            server = create_gateway_server(
                router,
                port=0,
                bearer_token=GATEWAY_TOKEN,
                token_path=Path(directory) / "gateway.token",
            )
            thread = start(server)
            try:
                for name, raw_request in cases.items():
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", server.server_address[1], timeout=2
                    )
                    connection.request(
                        "POST",
                        "/gateway/v1/responses",
                        body=raw_request,
                        headers={
                            "Host": f"127.0.0.1:{server.server_address[1]}",
                            "Authorization": f"Bearer {GATEWAY_TOKEN}",
                            "Content-Type": "application/json",
                            "Accept": "text/event-stream",
                        },
                    )
                    response = connection.getresponse()
                    raw_response = response.read()
                    headers = {
                        key.lower(): value for key, value in response.getheaders()
                    }
                    connection.close()

                    failures: list[str] = []
                    if response.status != 400:
                        failures.append(f"status was {response.status}, expected 400")
                    if headers.get("content-type") != "application/json; charset=utf-8":
                        failures.append("content type was not generic problem JSON")
                    if b"event:" in raw_response or b"data:" in raw_response:
                        failures.append("pre-runtime error was incorrectly encoded as SSE")
                    try:
                        payload = json.loads(raw_response.decode("utf-8"))
                    except (UnicodeError, json.JSONDecodeError):
                        failures.append("response body was not one JSON object")
                    else:
                        expected = {
                            "error": {
                                "code": "invalid_request",
                                "message": "Invalid request.",
                                "retryable": False,
                            }
                        }
                        if payload != expected:
                            failures.append(
                                f"problem payload changed: {payload!r}"
                            )
                    with self.subTest(case=name):
                        self.assertEqual(failures, [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(provider_calls, [])


if __name__ == "__main__":
    unittest.main()
