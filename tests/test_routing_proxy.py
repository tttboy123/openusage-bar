from __future__ import annotations

import io
import json
import hashlib
import http.client
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from openusage_bar.routing_contract import RouteTarget
from openusage_bar.routing_execution import (
    ExecutionAuthenticationError,
    ExecutionConnection,
    ExecutionTransientError,
)
from openusage_bar.routing_proxy import (
    DEFAULT_PROXY_PORT,
    ProxyConfigurationError,
    ProxyProblem,
    ProxyStreamInterrupted,
    RoutingProxyConfiguration,
    RoutingProxyConfigurationStore,
    RoutingProxyController,
    RoutingProxySupervisor,
    create_routing_proxy_server,
    run_proxy_command,
)
from openusage_bar.routing_targets import RouteTargetConfiguration


NOW = datetime(2026, 8, 3, 1, 0, tzinfo=timezone.utc)
CONTENT_SENTINEL = "prompt-private-sentinel-907f2d"


def target(identifier: str, *, model: str, connection: str) -> RouteTarget:
    return RouteTarget(
        target_id=identifier,
        provider_id="openai",
        account_ref=connection,
        model_id=model,
        connection_ref=connection,
        execution_class="openai_compatible",
        execution_adapter_id="openai_compatible.direct",
        resource_mode="quota",
        fact_account_ref=connection,
        runtime_scope_ref=None,
        balance_currency=None,
        cost_currency="USD",
        input_cost_micros_per_million=1_000_000,
        output_cost_micros_per_million=2_000_000,
        enabled=True,
        adapter_available=True,
        regions=("global",),
        privacy_class="direct_provider",
        capabilities=("chat", "reasoning", "tools"),
        context_window_tokens=128_000,
        quality_tier=3,
    )


def connection(value: RouteTarget) -> ExecutionConnection:
    return ExecutionConnection(
        connection_ref=value.connection_ref,
        provider_id=value.provider_id,
        account_ref=value.account_ref,
        execution_class=value.execution_class,
        execution_adapter_id=value.execution_adapter_id,
        base_url="https://api.example.invalid/v1",
        enabled=True,
        models=(value.model_id,),
    )


class FakeDecisionController:
    def __init__(self, selected: RouteTarget, alternatives: tuple[RouteTarget, ...]):
        self.selected = selected
        self.alternatives = alternatives
        self.requests: list[dict[str, object]] = []
        self.expires_at = "2026-08-03T01:00:30Z"

    def decide(self, payload: object, *, simulated: bool) -> dict[str, object]:
        assert isinstance(payload, dict)
        self.requests.append(payload)
        return {
            "schemaVersion": "1.0",
            "decisionId": "route_0123456789abcdef0123456789abcdef",
            "generatedAt": "2026-08-03T01:00:00Z",
            "expiresAt": self.expires_at,
            "selected": {
                "targetId": self.selected.target_id,
                "providerId": self.selected.provider_id,
                "accountRef": self.selected.account_ref,
                "modelId": self.selected.model_id,
                "score": 9000,
                "components": {},
                "reasons": [],
            },
            "alternatives": [
                {
                    "targetId": item.target_id,
                    "providerId": item.provider_id,
                    "accountRef": item.account_ref,
                    "modelId": item.model_id,
                    "score": 8000,
                    "components": {},
                    "reasons": [],
                }
                for item in self.alternatives
            ],
            "rejected": [],
            "warnings": [],
            "evidenceStored": True,
            "simulated": simulated,
        }


class FakeAdapter:
    adapter_id = "openai_compatible.direct"
    execution_class = "openai_compatible"

    def __init__(self, outcomes: list[object]):
        self.outcomes = outcomes
        self.requests: list[dict[str, object]] = []

    def execute_chat(
        self,
        connection: ExecutionConnection,
        model_id: str,
        body: dict[str, object],
    ) -> dict[str, object]:
        self.requests.append(body)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, dict)
        return outcome


class FakeStreamingAdapter(FakeAdapter):
    def __init__(self, outcomes: list[object]):
        super().__init__([])
        self.stream_outcomes = outcomes

    def execute_chat_stream(
        self,
        connection: ExecutionConnection,
        model_id: str,
        body: dict[str, object],
    ):
        self.requests.append(body)
        outcome = self.stream_outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return iter(outcome)


class FakeRegistry:
    def __init__(self, values: dict[str, tuple[FakeAdapter, ExecutionConnection]]):
        self.values = values

    def resolve(self, value: RouteTarget):
        return self.values[value.target_id]


class FakeEvidenceStore:
    def __init__(self):
        self.attempts = []

    def record_attempt(self, evidence):
        self.attempts.append(evidence)
        return object()


class RoutingProxyConfigurationTests(unittest.TestCase):
    def test_absent_configuration_is_disabled_with_a_stable_loopback_port(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RoutingProxyConfigurationStore(Path(directory) / "routing-proxy.json")

            value = store.load()

            self.assertEqual(
                value,
                RoutingProxyConfiguration(
                    schema_version=1,
                    revision=0,
                    enabled=False,
                    port=DEFAULT_PROXY_PORT,
                ),
            )

    def test_configuration_save_is_private_monotonic_and_rejects_duplicate_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routing-proxy.json"
            store = RoutingProxyConfigurationStore(path)
            enabled = RoutingProxyConfiguration(
                1, 1, True, 64124, hashlib.sha256(b"token").hexdigest()
            )

            store.save(enabled)

            self.assertEqual(store.load(), enabled)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "invalid proxy configuration"):
                store.save(RoutingProxyConfiguration(1, 1, False, 64124))
            self.assertEqual(path.read_bytes(), before)

            path.write_text(
                '{"schemaVersion":1,"revision":2,"enabled":true,'
                '"enabled":false,"port":64124}',
                encoding="utf-8",
            )
            path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "invalid proxy configuration"):
                store.load()

    def test_failed_config_save_never_emits_or_persists_raw_token(self):
        class FailingStore:
            def __init__(self, current: RoutingProxyConfiguration):
                self.current = current

            def load(self) -> RoutingProxyConfiguration:
                return self.current

            def save(self, value: RoutingProxyConfiguration) -> None:
                raise ProxyConfigurationError("invalid proxy configuration")

        cases = (
            ("enable", RoutingProxyConfiguration(1, 0, False, 64123)),
            (
                "rotate",
                RoutingProxyConfiguration(
                    1, 5, True, 64123, hashlib.sha256(b"old").hexdigest()
                ),
            ),
        )
        for command, current in cases:
            with self.subTest(command=command):
                stdout = io.StringIO()
                stderr = io.StringIO()
                code = run_proxy_command(
                    SimpleNamespace(proxy_command=command, port=64123),
                    stdout=stdout,
                    stderr=stderr,
                    config_store=FailingStore(current),
                    token_factory=lambda: "replacement-private-token-0123456789",
                )
                self.assertNotEqual(code, 0)
                self.assertEqual(stdout.getvalue(), "")
                self.assertNotIn("replacement-private-token", stderr.getvalue())


class RoutingProxyControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.first = target("openai.primary.gpt-5", model="gpt-5", connection="primary")
        self.second = target("openai.backup.gpt-5-mini", model="gpt-5-mini", connection="backup")
        self.configuration = RouteTargetConfiguration(1, 1, (self.first, self.second))
        self.evidence = FakeEvidenceStore()

    def proxy(self, first_adapter: FakeAdapter, second_adapter: FakeAdapter):
        decision = FakeDecisionController(self.first, (self.second,))
        registry = FakeRegistry({
            self.first.target_id: (first_adapter, connection(self.first)),
            self.second.target_id: (second_adapter, connection(self.second)),
        })
        return RoutingProxyController(
            decision_controller=decision,
            target_loader=lambda: self.configuration,
            registry_loader=lambda: registry,
            evidence_store=self.evidence,
            clock=lambda: NOW,
        ), decision

    def test_virtual_model_uses_content_free_decision_and_retries_429_once(self):
        first_adapter = FakeAdapter([
            ExecutionTransientError("http_429", "provider_rate_limited")
        ])
        second_adapter = FakeAdapter([{
            "id": "chatcmpl-public",
            "object": "chat.completion",
            "model": "upstream-private-name",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        }])
        proxy, decision = self.proxy(first_adapter, second_adapter)

        result = proxy.complete({
            "model": "openusage/reliable",
            "messages": [{"role": "user", "content": CONTENT_SENTINEL}],
            "tools": [{"type": "function", "function": {"name": "read_file"}}],
            "max_completion_tokens": 128,
            "stream": False,
        })

        self.assertEqual(result.status, 200)
        self.assertEqual(result.headers["x-openusage-route-id"], "route_0123456789abcdef0123456789abcdef")
        self.assertEqual(result.body["model"], "gpt-5-mini")
        self.assertEqual(len(first_adapter.requests), 1)
        self.assertEqual(len(second_adapter.requests), 1)
        self.assertIn(CONTENT_SENTINEL, json.dumps(second_adapter.requests[0]))
        self.assertNotIn(CONTENT_SENTINEL, json.dumps(decision.requests[0]))
        self.assertEqual(decision.requests[0]["policyId"], "reliable")
        self.assertEqual(
            decision.requests[0]["task"]["requiredCapabilities"],
            ["chat", "tools"],
        )
        self.assertEqual(
            [(value.ordinal, value.outcome, value.status_class) for value in self.evidence.attempts],
            [(1, "transient_failure", "http_429"), (2, "succeeded", "success")],
        )
        self.assertNotIn(CONTENT_SENTINEL, repr(self.evidence.attempts))

    def test_upstream_authentication_failure_is_sanitized_and_never_retried(self):
        first_adapter = FakeAdapter([ExecutionAuthenticationError(CONTENT_SENTINEL)])
        second_adapter = FakeAdapter([{"id": "must-not-run"}])
        proxy, _ = self.proxy(first_adapter, second_adapter)

        with self.assertRaises(ProxyProblem) as raised:
            proxy.complete({
                "model": "openusage/reliable",
                "messages": [{"role": "user", "content": CONTENT_SENTINEL}],
                "stream": False,
            })

        self.assertEqual(raised.exception.status, 502)
        self.assertEqual(raised.exception.code, "upstream_authentication_failed")
        self.assertNotIn(CONTENT_SENTINEL, str(raised.exception))
        self.assertEqual(len(first_adapter.requests), 1)
        self.assertEqual(second_adapter.requests, [])
        self.assertEqual(
            [(value.outcome, value.status_class) for value in self.evidence.attempts],
            [("permanent_failure", "authentication")],
        )

    def test_auto_virtual_model_uses_the_configured_default_policy(self):
        adapter = FakeAdapter([{
            "id": "chatcmpl-auto",
            "object": "chat.completion",
            "choices": [],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }])
        decision = FakeDecisionController(self.first, ())
        proxy = RoutingProxyController(
            decision_controller=decision,
            target_loader=lambda: self.configuration,
            registry_loader=lambda: FakeRegistry({
                self.first.target_id: (adapter, connection(self.first)),
            }),
            evidence_store=self.evidence,
            default_policy_loader=lambda: "economy",
            clock=lambda: NOW,
        )

        proxy.complete({
            "model": "openusage/auto",
            "messages": [{"role": "user", "content": CONTENT_SENTINEL}],
            "stream": False,
        })

        self.assertEqual(decision.requests[0]["policyId"], "economy")
        self.assertNotIn(CONTENT_SENTINEL, json.dumps(decision.requests[0]))

    def test_expired_phase_a_decision_is_rejected_before_provider_attempt(self):
        first_adapter = FakeAdapter([{"id": "must-not-run"}])
        second_adapter = FakeAdapter([{"id": "must-not-run"}])
        proxy, decision = self.proxy(first_adapter, second_adapter)
        decision.expires_at = "2026-08-03T00:59:59Z"

        with self.assertRaises(ProxyProblem) as raised:
            proxy.complete({
                "model": "openusage/reliable",
                "messages": [{"role": "user", "content": CONTENT_SENTINEL}],
                "stream": False,
            })

        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(raised.exception.code, "decision_expired")
        self.assertEqual(first_adapter.requests, [])
        self.assertEqual(second_adapter.requests, [])
        self.assertEqual(self.evidence.attempts, [])

    def test_stream_retries_before_first_byte_then_locks_selected_target(self):
        first_adapter = FakeStreamingAdapter([
            ExecutionTransientError("http_5xx", "provider_unavailable")
        ])
        second_adapter = FakeStreamingAdapter([[
            b'data: {"id":"chunk-1"}\n\n',
            b'data: [DONE]\n\n',
        ]])
        proxy, _ = self.proxy(first_adapter, second_adapter)

        stream = proxy.prepare_stream({
            "model": "openusage/reliable",
            "messages": [{"role": "user", "content": CONTENT_SENTINEL}],
            "stream": True,
        })

        self.assertEqual(stream.status, 200)
        self.assertEqual(stream.model_id, "gpt-5-mini")
        self.assertEqual(
            b"".join(stream.chunks),
            b'data: {"id":"chunk-1"}\n\ndata: [DONE]\n\n',
        )
        self.assertEqual(len(first_adapter.requests), 1)
        self.assertEqual(len(second_adapter.requests), 1)
        self.assertEqual(
            [(value.ordinal, value.outcome, value.reason_code) for value in self.evidence.attempts],
            [
                (1, "transient_failure", "provider_unavailable"),
                (2, "succeeded", "provider_completed"),
            ],
        )

    def test_stream_never_switches_target_after_first_emitted_byte(self):
        def interrupted():
            yield b'data: {"id":"already-visible"}\n\n'
            raise ExecutionTransientError("transport", "transport_error")

        first_adapter = FakeStreamingAdapter([interrupted()])
        second_adapter = FakeStreamingAdapter([[b"data: must-not-run\n\n"]])
        proxy, _ = self.proxy(first_adapter, second_adapter)

        stream = proxy.prepare_stream({
            "model": "openusage/reliable",
            "messages": [{"role": "user", "content": CONTENT_SENTINEL}],
            "stream": True,
        })
        iterator = iter(stream.chunks)
        self.assertEqual(next(iterator), b'data: {"id":"already-visible"}\n\n')
        with self.assertRaises(ProxyStreamInterrupted):
            next(iterator)

        self.assertEqual(len(first_adapter.requests), 1)
        self.assertEqual(second_adapter.requests, [])
        self.assertEqual(
            [(value.ordinal, value.outcome, value.reason_code) for value in self.evidence.attempts],
            [(1, "transient_failure", "stream_started")],
        )


class RoutingProxyServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.route_target = target(
            "openai.primary.gpt-5", model="gpt-5", connection="primary"
        )
        self.adapter = FakeAdapter([{
            "id": "chatcmpl-loopback",
            "object": "chat.completion",
            "model": "provider-model",
            "choices": [],
        }])
        self.registry = FakeRegistry({
            self.route_target.target_id: (
                self.adapter, connection(self.route_target)
            ),
        })
        self.controller = RoutingProxyController(
            decision_controller=FakeDecisionController(self.route_target, ()),
            target_loader=lambda: RouteTargetConfiguration(
                1, 1, (self.route_target,)
            ),
            registry_loader=lambda: self.registry,
            evidence_store=FakeEvidenceStore(),
            clock=lambda: NOW,
        )
        self.token = "proxy-test-token-0123456789abcdef"
        self.server = create_routing_proxy_server(
            self.controller, bearer_token=self.token, port=0
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: object | None = None,
    ) -> tuple[int, dict[str, str], dict[str, object]]:
        connection_value = http.client.HTTPConnection(
            self.server.server_address[0], self.server.server_address[1], timeout=2
        )
        headers: dict[str, str] = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        encoded = None
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection_value.request(method, path, body=encoded, headers=headers)
        response = connection_value.getresponse()
        payload = json.loads(response.read())
        response_headers = {key.casefold(): value for key, value in response.getheaders()}
        connection_value.close()
        return response.status, response_headers, payload

    def raw_request(self, framed: bytes) -> bytes:
        peer = socket.create_connection(self.server.server_address, timeout=2)
        peer.settimeout(2)
        peer.sendall(framed)
        try:
            peer.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        response = bytearray()
        while True:
            chunk = peer.recv(65_536)
            if not chunk:
                break
            response.extend(chunk)
        peer.close()
        return bytes(response)

    def test_binds_ipv4_loopback_and_requires_bearer_for_every_route(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

        status, headers, payload = self.request("GET", "/v1/models")
        self.assertEqual(status, 401)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(payload["error"]["code"], "unauthorized")

        status, _, payload = self.request(
            "GET", "/v1/models", token="wrong-token-0123456789abcdef"
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

    def test_models_and_nonstreaming_chat_use_openai_compatible_shapes(self) -> None:
        status, _, models = self.request(
            "GET", "/v1/models", token=self.token
        )
        self.assertEqual(status, 200)
        self.assertEqual(models["object"], "list")
        self.assertEqual(
            [value["id"] for value in models["data"]],
            ["openai.primary.gpt-5", "openusage/auto", "openusage/reliable"],
        )

        status, headers, response = self.request(
            "POST",
            "/v1/chat/completions",
            token=self.token,
            body={
                "model": "openusage/reliable",
                "messages": [{"role": "user", "content": CONTENT_SENTINEL}],
                "stream": False,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["model"], "gpt-5")
        self.assertEqual(
            headers["x-openusage-route-id"],
            "route_0123456789abcdef0123456789abcdef",
        )
        self.assertEqual(headers["cache-control"], "no-store")

    def test_streaming_chat_forwards_sse_with_decision_headers(self) -> None:
        streaming = FakeStreamingAdapter([[
            b'data: {"id":"chunk-1"}\n\n',
            b'data: [DONE]\n\n',
        ]])
        self.registry.values[self.route_target.target_id] = (
            streaming, connection(self.route_target)
        )
        client = http.client.HTTPConnection(
            self.server.server_address[0], self.server.server_address[1], timeout=2
        )
        body = json.dumps({
            "model": "openusage/reliable",
            "messages": [{"role": "user", "content": CONTENT_SENTINEL}],
            "stream": True,
        }).encode("utf-8")
        client.request(
            "POST",
            "/v1/chat/completions",
            body=body,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        response = client.getresponse()
        payload = response.read()
        headers = {key.casefold(): value for key, value in response.getheaders()}
        client.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(headers["content-type"], "text/event-stream")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-openusage-model"], "gpt-5")
        self.assertEqual(
            headers["x-openusage-route-id"],
            "route_0123456789abcdef0123456789abcdef",
        )
        self.assertNotIn("content-length", headers)
        self.assertEqual(
            payload,
            b'data: {"id":"chunk-1"}\n\ndata: [DONE]\n\n',
        )

    def test_rejects_smuggling_legacy_http_absolute_paths_and_folded_headers(self) -> None:
        host = f"127.0.0.1:{self.server.server_address[1]}".encode("ascii")
        authorization = ("Bearer " + self.token).encode("ascii")
        invalid = (
            b"GET /v1/models HTTP/1.0\r\nHost: " + host
            + b"\r\nAuthorization: " + authorization + b"\r\n\r\n",
            b"GET http://127.0.0.1/v1/models HTTP/1.1\r\nHost: " + host
            + b"\r\nAuthorization: " + authorization + b"\r\n\r\n",
            b"GET /v1/models HTTP/1.1\r\nHost: " + host
            + b"\r\nHost: " + host + b"\r\nAuthorization: " + authorization
            + b"\r\n\r\n",
            b"GET /v1/models HTTP/1.1\r\nHost: " + host
            + b"\r\nAuthorization: " + authorization
            + b"\r\n folded-value\r\n\r\n",
        )
        for framed in invalid:
            with self.subTest(framed=framed.split(b"\r\n", 1)[0]):
                response = self.raw_request(framed)
                self.assertIn(b"HTTP/1.1 400", response)
                self.assertIn(b'"code":"invalid_request"', response)
                self.assertNotIn(CONTENT_SENTINEL.encode(), response)

        body = b'{"model":"openusage/reliable","messages":[],"stream":false}'
        duplicate_length = (
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: " + host
            + b"\r\nAuthorization: " + authorization
            + b"\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\nContent-Length: " + str(len(body)).encode("ascii")
            + b"\r\n\r\n" + body
        )
        response = self.raw_request(duplicate_length)
        self.assertIn(b"HTTP/1.1 400", response)
        self.assertIn(b'"code":"invalid_request"', response)

        chunked = (
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: " + host
            + b"\r\nAuthorization: " + authorization
            + b"\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked"
            + b"\r\n\r\n0\r\n\r\n"
        )
        response = self.raw_request(chunked)
        self.assertIn(b"HTTP/1.1 400", response)
        self.assertIn(b'"code":"invalid_request"', response)

    def test_absolute_deadline_evicts_slow_header_and_releases_only_slot(self) -> None:
        server = create_routing_proxy_server(
            self.controller,
            bearer_token=self.token,
            port=0,
            max_threads=1,
            client_timeout=1,
            request_deadline=0.12,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        expired = threading.Event()
        original_expire = server._expire_request

        def mark_expired(request: socket.socket) -> None:
            try:
                original_expire(request)
            finally:
                expired.set()

        server._expire_request = mark_expired
        slow = socket.create_connection(server.server_address, timeout=1)

        def drip() -> None:
            request = (
                b"GET /v1/models HTTP/1.1\r\nHost: 127.0.0.1:"
                + str(server.server_address[1]).encode("ascii")
                + b"\r\nAuthorization: Bearer "
                + self.token.encode("ascii")
                + b"\r\n\r\n"
            )
            for byte in request:
                try:
                    slow.send(bytes([byte]))
                except OSError:
                    return
                time.sleep(0.04)

        dripper = threading.Thread(target=drip)
        dripper.start()
        try:
            self.assertTrue(expired.wait(2))
            deadline = time.monotonic() + 2
            while server.active_deadline_count and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(server.active_deadline_count, 0)

            client = http.client.HTTPConnection(*server.server_address, timeout=2)
            client.request(
                "GET",
                "/v1/models",
                headers={"Authorization": "Bearer " + self.token},
            )
            response = client.getresponse()
            response.read()
            client.close()
            self.assertEqual(response.status, 200)
        finally:
            slow.close()
            dripper.join(1)
            server.shutdown()
            server.server_close()
            thread.join(2)
            self.assertEqual(server.active_deadline_count, 0)

    def test_parser_failures_use_bounded_stable_json_errors(self) -> None:
        oversized = (
            b"GET /" + (b"a" * 8_300) + b" HTTP/1.1\r\nHost: localhost\r\n\r\n"
        )
        response = self.raw_request(oversized)
        self.assertIn(b"HTTP/1.1 413", response)
        self.assertIn(b'"code":"request_too_large"', response)
        self.assertNotIn(b"<!DOCTYPE HTML>", response)

        malformed = b"GET /v1/models WHAT/1.1\r\nHost: localhost\r\n\r\n"
        response = self.raw_request(malformed)
        self.assertIn(b"HTTP/1.1 400", response)
        self.assertIn(b'"code":"invalid_request"', response)
        self.assertNotIn(b"<!DOCTYPE HTML>", response)

    def test_live_verifier_rotation_keeps_listener_and_invalidates_old_token(self) -> None:
        replacement = "replacement-proxy-token-0123456789abcdef"
        self.server.replace_bearer_token_digest(
            hashlib.sha256(replacement.encode()).hexdigest()
        )

        old = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        old.request("GET", "/v1/models", headers={
            "Authorization": "Bearer " + self.token,
        })
        old_response = old.getresponse()
        old_response.read()
        old.close()

        new = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        new.request("GET", "/v1/models", headers={
            "Authorization": "Bearer " + replacement,
        })
        new_response = new.getresponse()
        new_response.read()
        new.close()

        self.assertEqual(old_response.status, 401)
        self.assertEqual(new_response.status, 200)

    def test_supervisor_restores_previous_runtime_when_same_port_rotation_fails(self) -> None:
        class FakeServer:
            def __init__(self) -> None:
                self.stop = threading.Event()
                self.closed = False

            def serve_forever(self) -> None:
                self.stop.wait(3)

            def shutdown(self) -> None:
                self.stop.set()

            def server_close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as directory:
            store = RoutingProxyConfigurationStore(
                Path(directory) / "routing-proxy.json"
            )
            original_digest = hashlib.sha256(b"original").hexdigest()
            replacement_digest = hashlib.sha256(b"replacement").hexdigest()
            store.save(RoutingProxyConfiguration(
                1, 1, True, 64123, original_digest
            ))
            calls = 0
            servers: list[FakeServer] = []

            def factory(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic bind failure")
                server = FakeServer()
                servers.append(server)
                return server

            failures: list[bool] = []
            supervisor = RoutingProxySupervisor(
                configuration_store=store,
                controller=self.controller,
                server_factory=factory,
                on_error=lambda: failures.append(True),
            )
            supervisor.reconcile()
            self.assertEqual(len(servers), 1)

            store.save(RoutingProxyConfiguration(
                1, 2, True, 64123, replacement_digest
            ))
            supervisor.reconcile()

            self.assertEqual(calls, 3)
            self.assertEqual(failures, [True])
            self.assertTrue(servers[0].closed)
            self.assertFalse(servers[1].closed)
            supervisor.close()
            self.assertTrue(servers[1].closed)

    def test_supervisor_rejects_legacy_enabled_config_without_verifier(self) -> None:
        class FakeServer:
            def __init__(self) -> None:
                self.stop = threading.Event()

            def serve_forever(self) -> None:
                self.stop.wait(3)

            def shutdown(self) -> None:
                self.stop.set()

            def server_close(self) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            store = RoutingProxyConfigurationStore(
                Path(directory) / "routing-proxy.json"
            )
            store.save(RoutingProxyConfiguration(1, 1, True, 64123))
            servers: list[FakeServer] = []
            failures: list[bool] = []

            def factory(*args, **kwargs):
                server = FakeServer()
                servers.append(server)
                return server

            supervisor = RoutingProxySupervisor(
                configuration_store=store,
                controller=self.controller,
                server_factory=factory,
                on_error=lambda: failures.append(True),
            )
            supervisor.reconcile()
            self.assertEqual(len(servers), 0)
            self.assertEqual(failures, [True])
            supervisor.close()


if __name__ == "__main__":
    unittest.main()
