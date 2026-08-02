from __future__ import annotations

import json
import socket
import stat
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from openusage_bar.query import (
    CapacityProvider,
    ProviderInstanceItem,
    QuotaAppliesTo,
    ResourceSnapshotResult,
    SnapshotSummary,
    SourceStatusItem,
)
from openusage_bar.routing_api import (
    RoutingAPIProblem,
    ROUTING_API_SCHEMA,
    RoutingController,
    create_routing_unix_server,
    decode_decision_request,
)
from openusage_bar.routing_contract import RouteTarget
from openusage_bar.routing_policy import RoutePolicy
from openusage_bar.routing_store import RoutingStore
from openusage_bar.routing_targets import RouteTargetConfiguration


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def custom_policy() -> RoutePolicy:
    return RoutePolicy(
        policy_id="custom_coding", revision=1,
        reliability_weight=45, headroom_weight=25,
        latency_weight=20, cost_weight=10,
        min_headroom_bp=1_000, max_error_rate_bp=1_500,
        min_runtime_samples=3, latency_reference_ms=8_000,
        cost_reference_micros=2_000, cost_currency="USD",
        min_balance_micros=2_000_000,
        balance_reference_micros=25_000_000,
        unknown_penalty=3_000, require_cost=False, require_runtime=False,
        allowed_execution_classes=frozenset({"direct_api", "openai_compatible"}),
        allowed_privacy_classes=frozenset({"direct_provider"}),
    )


def request_payload() -> dict[str, object]:
    return {
        "schemaVersion": "1.0",
        "clientRequestRef": "req_0123456789abcdef",
        "policyId": "reliable",
        "task": {
            "kind": "code",
            "requiredCapabilities": ["chat", "reasoning"],
            "estimatedInputTokens": 12_000,
            "maxOutputTokens": 4_000,
            "minimumContextWindowTokens": 16_000,
            "privacy": "direct_provider",
            "regions": ["global"],
        },
        "constraints": {
            "allowProviders": [],
            "denyProviders": [],
            "allowTargets": [],
            "denyTargets": [],
            "maximumEstimatedCostMicrounits": None,
            "costCurrency": None,
        },
        "session": {
            "sessionRef": "anon_0123456789abcdef",
            "remainingBudgetMicrounits": None,
            "reserveMicrounits": None,
            "budgetCurrency": None,
        },
    }


def target() -> RouteTarget:
    return RouteTarget(
        target_id="openai.work.gpt-5",
        provider_id="openai",
        account_ref="account-1",
        model_id="gpt-5",
        connection_ref="connection-1",
        execution_class="direct_api",
        execution_adapter_id="openai.direct",
        resource_mode="quota",
        fact_account_ref="account-1",
        runtime_scope_ref=None,
        balance_currency=None,
        cost_currency="USD",
        input_cost_micros_per_million=3_000_000,
        output_cost_micros_per_million=3_000_000,
        enabled=True,
        adapter_available=True,
        regions=("global",),
        privacy_class="direct_provider",
        capabilities=("chat", "reasoning", "tools"),
        context_window_tokens=400_000,
        quality_tier=4,
    )


def snapshot() -> ResourceSnapshotResult:
    quota = CapacityProvider(
        record_id="quota-1",
        provider_id="openai",
        account_ref="account-1",
        quota_name="weekly",
        unit="percent",
        used="20",
        quota_limit="100",
        remaining="80",
        remaining_ratio=0.8,
        resets_at="2026-08-03T12:00:00Z",
        period_start="2026-07-27T00:00:00Z",
        period_end="2026-08-03T00:00:00Z",
        observed_at="2026-08-02T11:59:55Z",
        freshness_seconds=60,
        state="ok",
        quality="official",
        stale=False,
        revision=11,
        source_id="openai.quota",
        quota_window="weekly",
        applies_to=QuotaAppliesTo("account", ()),
    )
    return ResourceSnapshotResult(
        schema_version="1.0",
        data_revision=11,
        generated_at="2026-08-02T12:00:00Z",
        local_day="2026-08-02",
        summary=SnapshotSummary(None, 0, 0),
        balances=(),
        quota_windows=(quota,),
        providers=(ProviderInstanceItem(
            provider_id="openai",
            family_id="openai",
            display_name="OpenAI",
            category="api",
            credential_source="keychain",
            source_kind="official",
            observed_at="2026-08-02T11:59:55Z",
            revision=11,
        ),),
        sources=(SourceStatusItem(
            provider_id="openai",
            source_id="openai.quota",
            state="ok",
            last_attempt_at="2026-08-02T11:59:55Z",
            last_success_at="2026-08-02T11:59:55Z",
            stale_at="2026-08-02T12:00:55Z",
            error_code=None,
        ),),
        catalog_revision="test",
    )


class FakeQuery:
    def resource_snapshot(self, today):
        self.today = today
        return snapshot()


class RoutingRequestContractTests(unittest.TestCase):
    def test_tracked_schema_matches_the_runtime_contract(self) -> None:
        path = (
            Path(__file__).parents[1]
            / "openusage_bar"
            / "resources"
            / "routing-api-v1.schema.json"
        )
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), ROUTING_API_SCHEMA)
        self.assertFalse(ROUTING_API_SCHEMA["additionalProperties"])

    def test_decodes_exact_content_free_request(self) -> None:
        envelope = decode_decision_request(request_payload())
        self.assertEqual(envelope.client_request_ref, "req_0123456789abcdef")
        self.assertEqual(envelope.request.policy_id, "reliable")
        self.assertEqual(envelope.request.task.required_capabilities, ("chat", "reasoning"))
        self.assertEqual(envelope.request.session.session_ref, "anon_0123456789abcdef")

    def test_rejects_unknown_content_identity_and_boolean_numbers(self) -> None:
        for key in ("prompt", "messages", "headers", "email", "apiKey"):
            value = request_payload()
            value[key] = "sentinel"
            with self.subTest(key=key), self.assertRaises(ValueError):
                decode_decision_request(value)
        value = request_payload()
        value["task"]["estimatedInputTokens"] = True
        with self.assertRaises(ValueError):
            decode_decision_request(value)

    def test_rejects_duplicate_json_keys_and_trailing_values(self) -> None:
        encoded = json.dumps(request_payload(), separators=(",", ":"))
        duplicate = encoded.replace(
            '"policyId":"reliable"',
            '"policyId":"reliable","policyId":"fast"',
        )
        for raw in (duplicate, encoded + "{}"):
            with self.subTest(raw=raw[-12:]), self.assertRaises(ValueError):
                decode_decision_request(raw)


class RoutingControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RoutingStore(
            Path(self.temp.name) / "routing.sqlite3", clock=lambda: NOW
        )
        self.addCleanup(self.store.close)
        self.controller = RoutingController(
            query=FakeQuery(),
            target_loader=lambda: RouteTargetConfiguration(1, 7, (target(),)),
            evidence_store=self.store,
            runtime_reader=lambda _start, _end: None,
            available_connections=lambda: ("connection-1",),
            clock=lambda: NOW,
        )

    def test_decide_returns_explainable_selection_and_persists_content_free_evidence(self) -> None:
        response = self.controller.decide(request_payload(), simulated=False)
        self.assertEqual(response["schemaVersion"], "1.0")
        self.assertRegex(response["decisionId"], r"^route_[0-9a-f]{32}$")
        self.assertEqual(response["selected"]["targetId"], "openai.work.gpt-5")
        self.assertEqual(response["facts"], {"dataRevision": 11, "runtimeRevision": None})
        self.assertTrue(response["evidenceStored"])
        self.assertFalse(response["simulated"])
        stored = self.store.get_decision(response["decisionId"])
        self.assertEqual(stored.selected_target_id, "openai.work.gpt-5")
        material = Path(self.store.path).read_bytes()
        for forbidden in (b"providerId", b"accountRef", b"modelId", b"OpenAI"):
            self.assertNotIn(forbidden, material)

    def test_simulation_uses_same_engine_without_writing_evidence(self) -> None:
        response = self.controller.decide(request_payload(), simulated=True)
        self.assertTrue(response["simulated"])
        self.assertFalse(response["evidenceStored"])
        self.assertEqual(self.store.decision_count(), 0)

    def test_custom_policy_loader_drives_listing_and_decisions(self) -> None:
        controller = RoutingController(
            query=FakeQuery(),
            target_loader=lambda: RouteTargetConfiguration(1, 7, (target(),)),
            evidence_store=self.store,
            runtime_reader=lambda _start, _end: None,
            available_connections=lambda: ("connection-1",),
            policy_loader=lambda: (custom_policy(),),
            clock=lambda: NOW,
        )
        payload = request_payload()
        payload["policyId"] = "custom_coding"

        response = controller.decide(payload, simulated=True)
        listed = controller.policies()["policies"]

        self.assertEqual(response["policy"], {
            "policyId": "custom_coding", "policyRevision": 1,
        })
        self.assertIn("custom_coding", [value["policyId"] for value in listed])

    def test_invalid_custom_policy_configuration_fails_closed(self) -> None:
        def invalid_loader():
            raise OSError("private path")

        controller = RoutingController(
            query=FakeQuery(),
            target_loader=lambda: RouteTargetConfiguration(1, 7, (target(),)),
            evidence_store=self.store,
            runtime_reader=lambda _start, _end: None,
            available_connections=lambda: ("connection-1",),
            policy_loader=invalid_loader,
            clock=lambda: NOW,
        )

        with self.assertRaises(RoutingAPIProblem) as raised:
            controller.decide(request_payload(), simulated=True)
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(raised.exception.code, "facts_unavailable")
        self.assertNotIn("private", raised.exception.message)

    def test_no_route_is_a_409_with_bounded_rejections(self) -> None:
        payload = request_payload()
        payload["task"]["requiredCapabilities"] = ["image"]
        with self.assertRaises(RoutingAPIProblem) as raised:
            self.controller.decide(payload, simulated=False)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.code, "no_route")
        self.assertEqual(
            raised.exception.details["rejected"][0]["reasonCodes"],
            ["capability_missing"],
        )
        self.assertEqual(self.store.decision_count(), 1)

    def test_history_never_rehydrates_provider_account_or_model_identity(self) -> None:
        decision = self.controller.decide(request_payload(), simulated=False)
        page = self.controller.list_decisions(before=None, limit=10)
        self.assertEqual(page["decisions"][0]["decisionId"], decision["decisionId"])
        encoded = json.dumps(page, sort_keys=True)
        for forbidden in ("providerId", "accountRef", "modelId", "connectionRef"):
            self.assertNotIn(forbidden, encoded)


def raw_unix_request(path: Path, request: bytes) -> tuple[int, dict[str, str], bytes]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    client.connect(str(path))
    try:
        try:
            client.sendall(request)
        except BrokenPipeError:
            pass
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        client.close()
    head, body = b"".join(chunks).split(b"\r\n\r\n", 1)
    lines = head.decode("ascii").split("\r\n")
    status = int(lines[0].split(" ", 2)[1])
    headers = {
        name.lower(): value.strip()
        for name, value in (line.split(":", 1) for line in lines[1:])
    }
    return status, headers, body


class RoutingUnixAPITests(RoutingControllerTests):
    def setUp(self) -> None:
        super().setUp()
        self.socket_path = Path(self.temp.name) / "router.sock"
        self.server = create_routing_unix_server(
            self.socket_path,
            self.controller,
            max_threads=4,
            client_timeout=1,
            request_deadline=2,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.running = True
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        if not self.running:
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.running = False

    def request(self, request: bytes) -> tuple[int, dict[str, str], object]:
        status, headers, body = raw_unix_request(self.socket_path, request)
        return status, headers, json.loads(body)

    def post(self, route: str, payload: object) -> tuple[int, dict[str, str], object]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return self.request(
            f"POST {route} HTTP/1.1\r\nHost: localhost\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode()
            + body
        )

    def test_socket_is_private_and_health_schema_policies_targets_are_bounded(self) -> None:
        self.assertEqual(stat.S_IMODE(self.socket_path.stat().st_mode), 0o600)
        for route in ("/v1/health", "/v1/schema.json", "/v1/policies", "/v1/targets"):
            with self.subTest(route=route):
                status, headers, body = self.request(
                    f"GET {route} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode()
                )
                self.assertEqual(status, 200)
                self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
                self.assertLessEqual(len(json.dumps(body).encode()), 256 * 1024)

    def test_decide_simulate_history_and_detail_share_one_contract(self) -> None:
        status, _, decided = self.post("/v1/decisions", request_payload())
        self.assertEqual(status, 200)
        status, _, simulated = self.post("/v1/simulations", request_payload())
        self.assertEqual(status, 200)
        self.assertTrue(simulated["simulated"])
        decision_id = decided["decisionId"]
        for route in ("/v1/decisions?limit=10", f"/v1/decisions/{decision_id}"):
            with self.subTest(route=route):
                status, _, body = self.request(
                    f"GET {route} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode()
                )
                self.assertEqual(status, 200)
                encoded = json.dumps(body)
                self.assertNotIn("accountRef", encoded)
                self.assertNotIn("modelId", encoded)

    def test_duplicate_trailing_oversized_chunked_and_wrong_http_fail_closed(self) -> None:
        encoded = json.dumps(request_payload(), separators=(",", ":"))
        duplicate = encoded.replace(
            '"policyId":"reliable"',
            '"policyId":"reliable","policyId":"fast"',
        ).encode()
        cases = (
            (
                b"POST /v1/decisions HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(duplicate)).encode() + b"\r\n\r\n" + duplicate,
                400,
            ),
            (
                b"POST /v1/decisions HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 4\r\n\r\n{}{}",
                400,
            ),
            (
                b"POST /v1/decisions HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
                400,
            ),
            (
                b"POST /v1/decisions HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 65537\r\n\r\n",
                413,
            ),
            (b"GET /v1/health HTTP/1.0\r\nHost: localhost\r\n\r\n", 400),
        )
        for request, expected in cases:
            with self.subTest(expected=expected, prefix=request[:30]):
                status, _, body = self.request(request)
                self.assertEqual(status, expected)
                self.assertIn("error", body)
                self.assertNotIn(str(self.socket_path), json.dumps(body))

    def test_server_close_removes_only_its_socket(self) -> None:
        self._stop_server()
        self.assertFalse(self.socket_path.exists())

    def test_bounded_concurrency_returns_router_busy(self) -> None:
        self._stop_server()
        self.server = create_routing_unix_server(
            self.socket_path,
            self.controller,
            max_threads=1,
            client_timeout=2,
            request_deadline=3,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.running = True
        held = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        held.settimeout(2)
        held.connect(str(self.socket_path))
        held.sendall(b"GET /v1/health HTTP/1.1\r\n")
        deadline = time.monotonic() + 1
        while self.server.active_deadline_count != 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(self.server.active_deadline_count, 1)
        try:
            status, _, body = self.request(
                b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\n\r\n"
            )
        finally:
            held.close()
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "router_busy")


if __name__ == "__main__":
    unittest.main()
