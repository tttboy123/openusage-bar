from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from openusage_bar.plugin.api import PluginRouter
from openusage_bar.plugin.clients import ResultTooLarge
from openusage_bar.plugin.store import PluginStore


API_VERSION = "plugin.openusage/v1"
NOW = datetime(2026, 8, 10, 4, 5, 6, 123456, tzinfo=timezone.utc)


class FakeFactsClient:
    def request(self, route: str, query: dict[str, object]) -> dict[str, object]:
        if route == "/v1/health":
            return {
                "schemaVersion": "1.0",
                "dataRevision": 7,
                "generatedAt": "2026-08-10T04:05:00Z",
                "health": {"ok": True, "status": "ok"},
            }
        raise AssertionError(route)


class FakeAdviceClient:
    calls = 0

    def should_send(self, request: dict[str, object]) -> dict[str, object]:
        self.calls += 1
        return {
            "decision": "yes",
            "confidence": 0.75,
            "reason": "quota_healthy",
            "defer_until": None,
            "details": {
                "quota_remaining": 1000.0,
                "burn_rate_per_min": None,
                "predicted_exhaustion_minutes": None,
            },
        }


class PluginRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = PluginStore(Path(self.temp.name) / "plugin.sqlite3", clock=lambda: NOW)
        self.advice = FakeAdviceClient()
        self.router = PluginRouter(
            store=self.store,
            facts_client=FakeFactsClient(),
            advice_client=self.advice,
            configured_principals=("loom",),
            clock=lambda: NOW,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_route_advice_is_exact_safe_and_replays_without_second_gateway_call(self) -> None:
        raw = json.dumps(
            {
                "apiVersion": API_VERSION,
                "provider": "openai",
                "model": "gpt-5",
                "estimatedTokens": 1200,
                "window": "5m",
            }
        ).encode()
        first = self.router.dispatch(
            "loom", "POST", "/plugin/v1/route-advice", raw,
            idempotency_key="idem_0123456789abcdef0123456789abcdef",
        )
        second = self.router.dispatch(
            "loom", "POST", "/plugin/v1/route-advice", raw,
            idempotency_key="idem_0123456789abcdef0123456789abcdef",
        )

        self.assertEqual(first, second)
        self.assertEqual(first[0], 200)
        self.assertEqual(self.advice.calls, 1)
        self.assertEqual(
            set(first[1]), {"apiVersion", "object", "decision"}
        )
        self.assertEqual(first[1]["apiVersion"], API_VERSION)
        self.assertEqual(first[1]["object"], "plugin.route_advice")
        decision = first[1]["decision"]
        self.assertRegex(decision["decisionId"], r"^decision_[0-9a-f]{32}$")
        self.assertEqual(decision["createdAt"], "2026-08-10T04:05:06.123456Z")
        self.assertEqual(decision["execution"], "advice_only")
        self.assertNotIn("provider", repr(first[1]).casefold())
        self.assertNotIn("gpt-5", repr(first[1]))

    def test_route_advice_rejects_unknown_fields_and_missing_idempotency(self) -> None:
        body = {
            "apiVersion": API_VERSION,
            "provider": "openai",
            "model": "gpt-5",
            "estimatedTokens": 1,
            "window": "5m",
            "prompt": "private",
        }
        status, payload = self.router.dispatch(
            "loom", "POST", "/plugin/v1/route-advice", json.dumps(body).encode()
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_idempotency_key")
        status, payload = self.router.dispatch(
            "loom", "POST", "/plugin/v1/route-advice", json.dumps(body).encode(),
            idempotency_key="idem_0123456789abcdef0123456789abcdef",
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_desktop_can_only_read_fixed_order_connections(self) -> None:
        status, payload = self.router.dispatch(
            "desktop", "GET", "/plugin/v1/connections", b""
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            set(payload), {"apiVersion", "object", "observedAt", "connections"}
        )
        self.assertEqual(
            [row["pluginId"] for row in payload["connections"]],
            ["loom", "codex", "claude_code"],
        )
        self.assertTrue(all(set(row) == {
            "pluginId", "configuration", "connection", "capabilityState",
            "capabilities", "lastSeenAt", "lastSyncOutcome", "lastSyncAt",
        } for row in payload["connections"]))

        denied, problem = self.router.dispatch(
            "desktop", "GET", "/plugin/v1/capabilities", b""
        )
        self.assertEqual(denied, 403)
        self.assertEqual(problem["error"]["code"], "insufficient_scope")

    def test_external_principal_cannot_read_connections_or_cross_principal_decision(self) -> None:
        status, problem = self.router.dispatch(
            "loom", "GET", "/plugin/v1/connections", b""
        )
        self.assertEqual(status, 403)
        self.assertEqual(problem["error"]["code"], "insufficient_scope")

    def test_get_routes_reject_query_and_body(self) -> None:
        for target, body in (
            ("/plugin/v1/schema?debug=1", b""),
            ("/plugin/v1/schema", b"{}"),
        ):
            with self.subTest(target=target, body=body):
                status, _ = self.router.dispatch("loom", "GET", target, body)
                self.assertIn(status, (400, 413))

    def test_connection_truth_starts_unconfigured_and_only_valid_requests_promote(self) -> None:
        router = PluginRouter(
            store=self.store, facts_client=FakeFactsClient(), advice_client=self.advice,
            configured_principals=(), clock=lambda: NOW,
        )
        initial = router.dispatch("desktop", "GET", "/plugin/v1/connections", b"")[1]
        self.assertTrue(all(row["configuration"] == "not_configured" for row in initial["connections"]))
        invalid = router.dispatch("codex", "POST", "/plugin/v1/health/query", b'{"apiVersion":"wrong"}')[0]
        self.assertEqual(invalid, 400)
        unchanged = router.dispatch("desktop", "GET", "/plugin/v1/connections", b"")[1]
        self.assertEqual(unchanged["connections"][1]["configuration"], "not_configured")
        status, _ = router.dispatch("codex", "GET", "/plugin/v1/capabilities", b"")
        self.assertEqual(status, 200)
        connected = router.dispatch("desktop", "GET", "/plugin/v1/connections", b"")[1]["connections"][1]
        self.assertEqual(connected["configuration"], "configured")
        self.assertEqual(connected["connection"], "connected")
        self.assertEqual(connected["capabilityState"], "negotiated")

    def test_query_result_limits_map_to_exact_422_for_usage_and_quotas(self) -> None:
        class TooLargeFacts(FakeFactsClient):
            def query_usage(self, _request: object) -> dict[str, object]:
                raise ResultTooLarge()

            def query_quotas(self, _request: object) -> dict[str, object]:
                raise ResultTooLarge()

        router = PluginRouter(
            store=self.store, facts_client=TooLargeFacts(), advice_client=self.advice,
            clock=lambda: NOW,
        )
        for route, payload in (
            ("/plugin/v1/usage/query", {"apiVersion": API_VERSION, "from": "2026-08-10", "to": "2026-08-10"}),
            ("/plugin/v1/quotas/query", {"apiVersion": API_VERSION, "limit": 128}),
        ):
            with self.subTest(route=route):
                status, response = router.dispatch("loom", "POST", route, json.dumps(payload).encode())
                self.assertEqual(status, 422)
                self.assertEqual(response["error"]["code"], "result_too_large")

    def test_first_terminal_outcome_wins_and_same_outcome_replays_original_receipt(self) -> None:
        advice_body = json.dumps({
            "apiVersion": API_VERSION, "provider": "openai", "model": "gpt-5",
            "estimatedTokens": 1, "window": "5m",
        }).encode()
        _, advice = self.router.dispatch(
            "loom", "POST", "/plugin/v1/route-advice", advice_body,
            idempotency_key="idem_11111111111111111111111111111111",
        )
        decision_id = advice["decision"]["decisionId"]
        first_payload = {
            "apiVersion": API_VERSION, "decisionId": decision_id,
            "outcome": "succeeded", "reason": None,
            "occurredAt": "2026-08-10T04:05:07.000000Z",
        }
        first = self.router.dispatch(
            "loom", "POST", "/plugin/v1/outcomes", json.dumps(first_payload).encode(),
            idempotency_key="idem_22222222222222222222222222222222",
        )
        same_payload = dict(first_payload, occurredAt="2026-08-10T04:05:08.000000Z")
        same = self.router.dispatch(
            "loom", "POST", "/plugin/v1/outcomes", json.dumps(same_payload).encode(),
            idempotency_key="idem_33333333333333333333333333333333",
        )
        conflicting_payload = dict(
            first_payload, outcome="failed", reason="unknown",
            occurredAt="2026-08-10T04:05:09.000000Z",
        )
        conflict = self.router.dispatch(
            "loom", "POST", "/plugin/v1/outcomes", json.dumps(conflicting_payload).encode(),
            idempotency_key="idem_44444444444444444444444444444444",
        )
        self.assertEqual(first[0], 200)
        self.assertEqual(same, first)
        self.assertEqual(conflict[0], 409)
        self.assertEqual(conflict[1]["error"]["code"], "decision_outcome_conflict")


    def test_dispatch_fails_closed_for_invalid_requests(self) -> None:
        self.assertEqual(self.router.dispatch("unknown", "GET", "/plugin/v1/schema", b"")[0], 401)
        self.assertEqual(self.router.dispatch("loom", 123, "/plugin/v1/schema", b"")[0], 400)
        self.assertEqual(self.router.dispatch("loom", "GET", "/plugin/v1/schema?x=1", b"")[0], 400)
        self.assertEqual(self.router.dispatch("loom", "GET", "/plugin/v1/nope", b"")[0], 404)
        self.assertEqual(self.router.dispatch("loom", "POST", "/plugin/v1/schema", b"")[0], 405)
        self.assertEqual(self.router.dispatch("desktop", "GET", "/plugin/v1/schema", b"")[0], 403)
        self.assertEqual(self.router.dispatch("loom", "GET", "/plugin/v1/connections", b"")[0], 403)
        self.assertEqual(self.router.dispatch("loom", "GET", "/plugin/v1/schema", b"x")[0], 413)
        self.assertEqual(
            self.router.dispatch(
                "loom", "GET", "/plugin/v1/schema", b"",
                idempotency_key="idem_0123456789abcdef0123456789abcdef",
            )[0],
            400,
        )
        self.assertEqual(
            self.router.dispatch(
                "loom", "POST", "/plugin/v1/route-advice", b"{}",
            )[0],
            400,
        )
        self.assertEqual(
            self.router.dispatch(
                "loom", "POST", "/plugin/v1/health/query", b"{}",
                idempotency_key="idem_0123456789abcdef0123456789abcdef",
            )[0],
            400,
        )
        self.assertEqual(
            self.router.dispatch("loom", "POST", "/plugin/v1/route-advice", b"{bad json")[0],
            400,
        )

    def test_dispatch_health_and_decision_error_paths(self) -> None:
        code, _ = self.router.dispatch("loom", "GET", "/plugin/v1/decisions/bad", b"")
        self.assertEqual(code, 404)
        code, _ = self.router.dispatch(
            "loom", "POST", "/plugin/v1/outcomes",
            json.dumps({
                "apiVersion": API_VERSION,
                "decisionId": "decision_0123456789abcdef0123456789abcdef",
                "outcome": "failed",
                "reason": "unknown",
                "occurredAt": "2026-08-10T04:05:06.123456Z",
            }).encode(),
            idempotency_key="idem_0123456789abcdef0123456789abcdef",
        )
        self.assertEqual(code, 404)

    def test_health_reports_dependency_failure_as_503(self) -> None:
        class BrokenFacts:
            def request(self, route, query):
                raise RuntimeError("facts unavailable")

        broken = PluginRouter(
            store=self.store,
            facts_client=BrokenFacts(),
            advice_client=self.advice,
            configured_principals=("loom",),
            clock=lambda: NOW,
        )
        code, _ = broken.dispatch(
            "loom", "POST", "/plugin/v1/usage/query",
            json.dumps({
                "apiVersion": API_VERSION,
                "from": "2026-08-01",
                "to": "2026-08-07",
            }).encode(),
        )
        self.assertEqual(code, 503)
        broken.close()

    def test_router_constructor_rejects_invalid_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid Plugin store"):
            PluginRouter(
                store=object(),
                facts_client=FakeFactsClient(),
                advice_client=FakeAdviceClient(),
            )
        with self.assertRaisesRegex(ValueError, "invalid Plugin principals"):
            PluginRouter(
                store=self.store,
                facts_client=FakeFactsClient(),
                advice_client=FakeAdviceClient(),
                configured_principals=("not-a-principal",),
            )
    def test_route_advice_advice_client_invalid_response_is_sanitized(self) -> None:
        class BadAdvice:
            def should_send(self, request):
                return {"decision": "maybe"}

        router = PluginRouter(
            store=self.store,
            facts_client=FakeFactsClient(),
            advice_client=BadAdvice(),
            configured_principals=("loom",),
            clock=lambda: NOW,
        )
        code, _ = router.dispatch(
            "loom", "POST", "/plugin/v1/route-advice",
            json.dumps({
                "apiVersion": API_VERSION, "provider": "openai", "model": "gpt-5",
                "estimatedTokens": 1200, "window": "5m",
            }).encode(),
            idempotency_key="idem_0123456789abcdef0123456789abcdef",
        )
        self.assertEqual(code, 503)
        router.close()

    def test_health_query_reports_observer_and_gateway_readiness(self) -> None:
        class ReadyAdvice:
            def should_send(self, request):
                raise AssertionError("should not be called")

            def health(self):
                return "disabled"

        router = PluginRouter(
            store=self.store,
            facts_client=FakeFactsClient(),
            advice_client=ReadyAdvice(),
            configured_principals=("loom",),
            clock=lambda: NOW,
        )
        code, payload = router.dispatch(
            "loom", "POST", "/plugin/v1/health/query",
            json.dumps({"apiVersion": API_VERSION}).encode(),
        )
        self.assertEqual(code, 200)
        self.assertEqual(payload["observerApi"], "ready")
        self.assertEqual(payload["gatewayApi"], "disabled")
        router.close()
if __name__ == "__main__":
    unittest.main()


