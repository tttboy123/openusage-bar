from __future__ import annotations

import json
import tempfile
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
    RoutingController,
    decode_decision_request,
)
from openusage_bar.routing_contract import RouteTarget
from openusage_bar.routing_store import RoutingStore
from openusage_bar.routing_targets import RouteTargetConfiguration


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


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


if __name__ == "__main__":
    unittest.main()
