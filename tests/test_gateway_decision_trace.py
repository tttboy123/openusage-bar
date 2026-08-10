from __future__ import annotations

import json
import re
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - optional development dependency
    Draft202012Validator = None

from openusage_bar.gateway.accounts import ProviderAccountRef
from openusage_bar.gateway.api import GatewayRouter
from openusage_bar.gateway.contracts import (
    Decision,
    GatewayMode,
    ShouldSendDecision,
)
from openusage_bar.gateway.decision_trace import (
    DECISION_TRACE_API_VERSION,
    DecisionTraceRecorder,
    validate_decision_trace,
    validate_decision_traces_payload,
)
from openusage_bar.gateway.pools import (
    AccountCandidate,
    AccountPool,
    PoolMember,
    PoolSelector,
    PoolStrategy,
)
from openusage_bar.gateway.config import GatewayConfig
from openusage_bar.query import CapacityProvider, CapacityResult, QuotaAppliesTo


_TRACE_KEYS = {
    "traceId",
    "occurredAt",
    "kind",
    "execution",
    "outcome",
    "reason",
    "pool",
    "selected",
    "exclusions",
    "fallback",
    "factsWindow",
}
_TRACE_ID = re.compile(r"^trace_[0-9a-f]{32}$")
_ROOT = Path(__file__).resolve().parents[1]


def _decision(value: Decision = Decision.YES) -> ShouldSendDecision:
    return ShouldSendDecision(
        decision=value,
        confidence=0.9,
        reason="quota_healthy" if value is Decision.YES else "quota_low",
        defer_until=None,
        quota_remaining=500.0,
        burn_rate_per_minute=2.0,
        predicted_exhaustion_minutes=250.0,
    )


def _gateway_payload(*, status: str = "complete") -> dict[str, object]:
    return {
        "apiVersion": "gateway.openusage/v1",
        "object": "gateway.response",
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "stream": False,
        "status": status,
        "outputText": "private response body",
        "usage": None,
        "tool": {"observed": False},
        "cache": {"outcome": "disabled"},
        "fallback": {
            "attempted": False,
            "attemptCount": 1,
            "finalAction": "none",
        },
    }


class DecisionTraceContractTests(unittest.TestCase):
    @unittest.skipIf(
        Draft202012Validator is None,
        "jsonschema is unavailable for Decision Trace schema validation",
    )
    def test_committed_schema_matches_the_live_exact_projection(self) -> None:
        schema = json.loads(
            (
                _ROOT
                / "openusage_bar/resources/gateway-decision-trace-v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        Draft202012Validator.check_schema(schema)
        recorder = DecisionTraceRecorder()
        recorder.record_route_advice(
            outcome="yes",
            reason="quota_healthy",
            facts_window_duration_seconds=300,
        )
        Draft202012Validator(schema).validate(recorder.snapshot())

    def test_recorder_is_bounded_thread_safe_and_newest_first(self) -> None:
        recorder = DecisionTraceRecorder(max_traces=128)

        def record(index: int) -> None:
            self.assertTrue(
                recorder.record_route_advice(
                    outcome="yes",
                    reason="quota_healthy",
                    facts_window_duration_seconds=300,
                    occurred_at=datetime(
                        2026, 8, 10, 0, 0, index % 60, index, tzinfo=timezone.utc
                    ),
                )
            )

        with ThreadPoolExecutor(max_workers=16) as executor:
            list(executor.map(record, range(512)))

        payload = recorder.snapshot()
        self.assertEqual(set(payload), {"apiVersion", "traces"})
        self.assertEqual(payload["apiVersion"], DECISION_TRACE_API_VERSION)
        self.assertEqual(len(payload["traces"]), 128)
        self.assertIsNotNone(validate_decision_traces_payload(payload))
        occurred = [item["occurredAt"] for item in payload["traces"]]
        self.assertEqual(occurred, sorted(occurred, reverse=True))
        for trace in payload["traces"]:
            self.assertEqual(set(trace), _TRACE_KEYS)
            self.assertRegex(trace["traceId"], _TRACE_ID)

    def test_exact_validator_rejects_private_or_ambiguous_forms(self) -> None:
        recorder = DecisionTraceRecorder()
        self.assertTrue(
            recorder.record_gateway_execution(
                provider_id="openai",
                outcome="succeeded",
                fallback={
                    "attempted": False,
                    "attemptCount": 1,
                    "finalAction": "none",
                },
            )
        )
        valid = recorder.snapshot()["traces"][0]
        self.assertEqual(
            valid["selected"],
            {"providerId": "openai", "accountDisplayId": None},
        )
        self.assertIsNotNone(validate_decision_trace(valid))

        hostile = (
            {**valid, "prompt": "secret prompt"},
            {**valid, "selected": {"providerId": "custom", "accountDisplayId": None}},
            {**valid, "selected": {"providerId": "openai", "accountId": "private"}},
            {**valid, "reason": "raw upstream error: sk-secret"},
            {**valid, "traceId": "trace_" + "A" * 32},
            {**valid, "occurredAt": "2026-08-10T00:00:00+00:00"},
            {**valid, "fallback": {**valid["fallback"], "endpoint": "/private"}},
            {
                **valid,
                "exclusions": [
                    {
                        "accountDisplayId": "acct_0123456789ab",
                        "reason": "secret",
                    }
                ],
            },
        )
        for candidate in hostile:
            with self.subTest(candidate=candidate):
                self.assertIsNone(validate_decision_trace(candidate))

        root = recorder.snapshot()
        self.assertIsNone(
            validate_decision_traces_payload({**root, "credential": "sk-secret"})
        )

    def test_process_lifetime_snapshot_is_a_detached_public_projection(self) -> None:
        recorder = DecisionTraceRecorder()
        recorder.record_route_advice(
            outcome="defer",
            reason="quota_low",
            facts_window_duration_seconds=None,
        )
        first = recorder.snapshot()
        first["traces"][0]["reason"] = "tampered"
        second = recorder.snapshot()
        self.assertEqual(second["traces"][0]["reason"], "quota_low")

    def test_cross_platform_numeric_and_collection_bounds_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            DecisionTraceRecorder(max_traces=129)

        recorder = DecisionTraceRecorder()
        self.assertFalse(
            recorder.record_route_advice(
                outcome="yes",
                reason="quota_healthy",
                facts_window_duration_seconds=2_678_401,
            )
        )
        self.assertFalse(
            recorder.record_pool_selection(
                pool_id="daily-coding",
                revision=2**53,
                strategy="fixed-first",
                selected_provider_id=None,
                selected_account_display_id=None,
                exclusions=[],
            )
        )
        self.assertFalse(
            recorder.record_gateway_execution(
                provider_id="openai",
                outcome="failed",
                fallback={
                    "attempted": False,
                    "attemptCount": 2,
                    "finalAction": "none",
                },
            )
        )
        self.assertEqual(recorder.snapshot()["traces"], [])


class DecisionTraceIntegrationTests(unittest.TestCase):
    def test_read_only_route_records_should_send_without_request_identity(self) -> None:
        recorder = DecisionTraceRecorder()
        router = GatewayRouter(
            mode=GatewayMode.ADVISE,
            policy=lambda _request: _decision(),
            proxy=None,
            decision_traces=recorder,
        )

        status, advice = router.dispatch(
            "POST",
            "/gateway/v1/should-send",
            json.dumps(
                {
                    "provider": "hostile-private-provider",
                    "model": "private-model-name",
                    "estimated_tokens": 10,
                    "window": "5m",
                }
            ).encode(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(advice["decision"], "yes")

        status, payload = router.dispatch(
            "GET", "/gateway/v1/decision-traces", b""
        )
        self.assertEqual(status, 200)
        self.assertEqual(set(payload), {"apiVersion", "traces"})
        trace = payload["traces"][0]
        self.assertEqual(
            trace,
            {
                **trace,
                "kind": "route_advice",
                "execution": "advice_only",
                "outcome": "yes",
                "reason": "quota_healthy",
                "pool": None,
                "selected": None,
                "exclusions": [],
                "fallback": None,
                "factsWindow": {"durationSeconds": 300},
            },
        )
        rendered = json.dumps(payload, sort_keys=True)
        self.assertNotIn("hostile-private-provider", rendered)
        self.assertNotIn("private-model-name", rendered)

    def test_observe_mode_always_returns_exact_empty_trace_root(self) -> None:
        recorder = DecisionTraceRecorder()
        recorder.record_route_advice(
            outcome="yes",
            reason="quota_healthy",
            facts_window_duration_seconds=300,
        )
        router = GatewayRouter(
            mode=GatewayMode.OBSERVE,
            policy=None,
            proxy=None,
            decision_traces=recorder,
        )
        self.assertEqual(
            router.dispatch("GET", "/gateway/v1/decision-traces", b""),
            (
                200,
                {"apiVersion": DECISION_TRACE_API_VERSION, "traces": []},
            ),
        )

    def test_non_stream_gateway_trace_uses_only_sanitized_result_and_fallback(
        self,
    ) -> None:
        recorder = DecisionTraceRecorder()

        def proxy(_payload: dict[str, object]) -> dict[str, object]:
            return _gateway_payload()

        router = GatewayRouter(
            mode=GatewayMode.GATEWAY,
            policy=lambda _request: _decision(),
            proxy=proxy,
            decision_traces=recorder,
        )
        request = {
            "provider": "openai",
            "model": "gpt-private-model",
            "request": {
                "model": "gpt-private-model",
                "input": "sk-prompt-private",
                "stream": False,
            },
        }
        status, _response = router.dispatch(
            "POST", "/gateway/v1/responses", json.dumps(request).encode()
        )
        self.assertEqual(status, 200)

        trace = recorder.snapshot()["traces"][0]
        self.assertEqual(trace["kind"], "gateway_execution")
        self.assertEqual(trace["execution"], "executed")
        self.assertEqual(trace["outcome"], "succeeded")
        self.assertEqual(
            trace["selected"],
            {"providerId": "openai", "accountDisplayId": None},
        )
        self.assertEqual(
            trace["fallback"],
            {"attempted": False, "attemptCount": 1, "finalAction": "none"},
        )
        rendered = json.dumps(trace, sort_keys=True)
        for forbidden in (
            "gpt-private-model",
            "sk-prompt-private",
            "private response body",
            "outputText",
            "usage",
            "request",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_failed_execution_projects_only_exact_public_fallback_fields(self) -> None:
        recorder = DecisionTraceRecorder()

        def proxy(_payload: dict[str, object]) -> dict[str, object]:
            return {
                "apiVersion": "gateway.openusage/v1",
                "object": "gateway.error",
                "error": {
                    "code": "fallback_exhausted",
                    "message": "sanitized",
                    "retryable": False,
                },
                "fallback": {
                    "attempted": True,
                    "attemptCount": 2,
                    "finalAction": "fail",
                    "errorCode": "fallback_exhausted",
                    "retryable": False,
                },
            }

        router = GatewayRouter(
            mode=GatewayMode.GATEWAY,
            policy=None,
            proxy=proxy,
            decision_traces=recorder,
        )
        status, _response = router.dispatch(
            "POST",
            "/gateway/v1/responses",
            json.dumps(
                {
                    "provider": "openai",
                    "model": "private-model",
                    "request": {
                        "model": "private-model",
                        "input": "private prompt",
                        "stream": False,
                    },
                }
            ).encode(),
        )
        self.assertEqual(status, 200)
        trace = recorder.snapshot()["traces"][0]
        self.assertEqual(trace["outcome"], "failed")
        self.assertEqual(
            trace["fallback"],
            {"attempted": True, "attemptCount": 2, "finalAction": "fail"},
        )
        self.assertNotIn("errorCode", trace["fallback"])
        self.assertNotIn("private", json.dumps(trace, sort_keys=True))

    def test_pool_selector_records_actual_public_selection_only_when_provided(
        self,
    ) -> None:
        recorder = DecisionTraceRecorder()
        account = ProviderAccountRef(
            provider_id="openai",
            account_id="account-private",
            alias="Private alias",
            credential_account="openai.account-private.gateway-api-key",
        )
        pool = AccountPool(
            pool_id="daily-coding",
            revision=7,
            strategy=PoolStrategy.FIXED_FIRST,
            members=(PoolMember("account-private"),),
        )
        candidate = AccountCandidate(
            account=account,
            health=1.0,
            credential_backend_available=True,
        )
        selection = PoolSelector(decision_traces=recorder).select(
            pool,
            candidates=(candidate,),
            requested_provider_id="openai",
        )
        self.assertEqual(selection.selected_account_id, "account-private")

        trace = recorder.snapshot()["traces"][0]
        self.assertEqual(trace["kind"], "pool_selection")
        self.assertEqual(trace["outcome"], "selected")
        self.assertEqual(
            trace["pool"],
            {"poolId": "daily-coding", "revision": 7, "strategy": "fixed-first"},
        )
        self.assertEqual(
            trace["selected"],
            {"providerId": "openai", "accountDisplayId": account.display_id},
        )
        rendered = json.dumps(trace, sort_keys=True)
        self.assertNotIn("account-private", rendered)
        self.assertNotIn("Private alias", rendered)
        self.assertNotIn("gateway-api-key", rendered)

        PoolSelector().select(
            pool,
            candidates=(candidate,),
            requested_provider_id="openai",
        )
        self.assertEqual(len(recorder.snapshot()["traces"]), 1)

    def test_pool_unavailable_trace_contains_only_bounded_public_exclusions(
        self,
    ) -> None:
        recorder = DecisionTraceRecorder()
        account = ProviderAccountRef(
            provider_id="openai",
            account_id="account-private",
            alias="Private alias",
            credential_account="openai.account-private.gateway-api-key",
        )
        pool = AccountPool(
            pool_id="daily-coding",
            revision=7,
            strategy=PoolStrategy.FIXED_FIRST,
            members=(PoolMember("account-private"),),
        )
        selection = PoolSelector(decision_traces=recorder).select(
            pool,
            candidates=(AccountCandidate(account=account, health=None),),
            requested_provider_id="openai",
        )
        self.assertEqual(selection.status, "unavailable")
        trace = recorder.snapshot()["traces"][0]
        self.assertEqual(trace["outcome"], "unavailable")
        self.assertIsNone(trace["selected"])
        self.assertEqual(
            trace["exclusions"],
            [{"accountDisplayId": account.display_id, "reason": "health_unknown"}],
        )
        self.assertNotIn("account-private", json.dumps(trace, sort_keys=True))

    def test_default_server_composition_shares_one_process_lifetime_recorder(
        self,
    ) -> None:
        import openusage_bar.collector_cli as collector_cli

        class Query:
            @staticmethod
            def capacity() -> CapacityResult:
                return CapacityResult(
                    "1.0",
                    1,
                    "2026-08-10T00:00:00Z",
                    (
                        CapacityProvider(
                            record_id="openai.daily",
                            provider_id="openai",
                            account_ref=None,
                            quota_name="Daily",
                            unit="tokens",
                            used="10",
                            quota_limit="1000",
                            remaining="990",
                            remaining_ratio=0.99,
                            resets_at=None,
                            period_start=None,
                            period_end=None,
                            observed_at="2026-08-10T00:00:00Z",
                            freshness_seconds=60,
                            state="ok",
                            quality="direct",
                            stale=False,
                            revision=1,
                            source_id="openai.quota",
                            quota_window="daily",
                            applies_to=QuotaAppliesTo("model", ("gpt-4.1",)),
                        ),
                    ),
                )

        captured: list[GatewayRouter] = []

        def capture(router: GatewayRouter, **_kwargs: object) -> object:
            captured.append(router)
            return object()

        with (
            patch(
                "openusage_bar.gateway.server.create_gateway_server",
                side_effect=capture,
            ),
            patch(
                "openusage_bar.gateway.telemetry.GatewayTelemetryStore",
                side_effect=RuntimeError("telemetry unavailable"),
            ),
        ):
            collector_cli._build_default_gateway_server(
                config=GatewayConfig(enabled=True, mode=GatewayMode.ADVISE),
                token_path=Path("/not-opened/gateway.token"),
                query=Query(),
            )

        self.assertEqual(len(captured), 1)
        router = captured[0]
        status, _advice = router.dispatch(
            "POST",
            "/gateway/v1/should-send",
            json.dumps(
                {
                    "provider": "openai",
                    "model": "gpt-4.1",
                    "estimated_tokens": 10,
                    "window": "5m",
                }
            ).encode(),
        )
        self.assertEqual(status, 200)
        status, root = router.dispatch("GET", "/gateway/v1/decision-traces", b"")
        self.assertEqual(status, 200)
        self.assertEqual(len(root["traces"]), 1)
        self.assertEqual(root["traces"][0]["kind"], "route_advice")


if __name__ == "__main__":
    unittest.main()
