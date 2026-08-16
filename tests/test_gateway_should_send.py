import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

from openusage_bar.gateway.contracts import Decision, ShouldSendRequest
from openusage_bar.gateway.policy import (
    PolicySnapshot,
    ShouldSendPolicy,
    SnapshottingShouldSendEvaluator,
)
from openusage_bar.query import (
    CapacityProvider,
    CapacityResult,
    QuotaAppliesTo,
)


class ShouldSendPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ShouldSendPolicy(no_threshold=0.05, defer_threshold=0.15)
        self.request = ShouldSendRequest("openai", "gpt-4o", 8000, "5m")

    def test_unknown_quota_defers(self) -> None:
        result = self.policy.decide(
            self.request,
            PolicySnapshot(None, None, None, True),
        )

        self.assertEqual(result.decision, Decision.DEFER)
        self.assertEqual(result.reason, "quota_unknown")
        self.assertEqual(result.confidence, 0.5)

    def test_exhausted_quota_rejects(self) -> None:
        result = self.policy.decide(
            self.request,
            PolicySnapshot(0.02, 9000.0, 1.0, False),
        )

        self.assertEqual(result.decision, Decision.NO)
        self.assertEqual(result.reason, "approaching_limit")
        self.assertEqual(result.confidence, 0.95)

    def test_low_quota_with_slow_burn_defers_for_quota_not_burn_rate(self) -> None:
        result = self.policy.decide(
            self.request,
            PolicySnapshot(0.12, 9000.0, 12.0, False),
        )

        self.assertEqual(result.decision, Decision.DEFER)
        self.assertEqual(result.reason, "quota_low")
        self.assertEqual(result.confidence, 0.8)

    def test_predicted_exhaustion_inside_fifteen_minutes_defers(self) -> None:
        result = self.policy.decide(
            self.request,
            PolicySnapshot(0.74, 850.0, 90.0, False),
        )

        self.assertEqual(result.decision, Decision.DEFER)
        self.assertEqual(result.reason, "burn_rate_too_high")
        self.assertEqual(result.confidence, 0.8)
        self.assertAlmostEqual(result.predicted_exhaustion_minutes, 850.0 / 90.0)

    def test_predicted_exhaustion_at_fifteen_minutes_is_not_fast_burn(self) -> None:
        result = self.policy.decide(
            self.request,
            PolicySnapshot(0.74, 150.0, 10.0, False),
        )

        self.assertEqual(result.decision, Decision.YES)
        self.assertEqual(result.reason, "quota_healthy")
        self.assertEqual(result.predicted_exhaustion_minutes, 15.0)

    def test_healthy_quota_allows(self) -> None:
        result = self.policy.decide(
            self.request,
            PolicySnapshot(0.74, 850.0, 10.0, False),
        )

        self.assertEqual(result.decision, Decision.YES)
        self.assertEqual(result.reason, "quota_healthy")
        self.assertEqual(result.confidence, 0.92)

    def test_snapshotting_evaluator_reuses_facts_for_ten_seconds(self) -> None:
        now = [100.0]
        calls = {"capacity": 0, "burn": 0}

        def capacity():
            calls["capacity"] += 1
            return self._capacity_result()

        def burn_rate(_provider_id: str, _model_id: str):
            calls["burn"] += 1
            return 10.0

        evaluator = SnapshottingShouldSendEvaluator(
            capacity=capacity,
            burn_rate=burn_rate,
            ttl_seconds=10.0,
            monotonic=lambda: now[0],
        )

        first = evaluator(self.request)
        repeated = evaluator(self.request)
        now[0] += 9.999
        still_fresh = evaluator(self.request)
        now[0] += 0.001
        refreshed = evaluator(self.request)

        self.assertEqual(first.decision, Decision.YES)
        self.assertEqual(first, repeated)
        self.assertEqual(first, still_fresh)
        self.assertEqual(first, refreshed)
        self.assertEqual(calls, {"capacity": 2, "burn": 2})

    def test_snapshotting_evaluator_single_flights_concurrent_refresh(self) -> None:
        entered = Event()
        release = Event()
        calls = 0
        calls_lock = Lock()

        def capacity():
            nonlocal calls
            with calls_lock:
                calls += 1
            entered.set()
            self.assertTrue(release.wait(2))
            return self._capacity_result()

        evaluator = SnapshottingShouldSendEvaluator(
            capacity=capacity,
            ttl_seconds=10.0,
            monotonic=lambda: 100.0,
        )
        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = [executor.submit(evaluator, self.request) for _ in range(16)]
            self.assertTrue(entered.wait(2))
            release.set()
            decisions = [future.result(2) for future in futures]

        self.assertTrue(all(item.decision is Decision.YES for item in decisions))
        self.assertEqual(calls, 1)

    def test_snapshotting_evaluator_degrades_burn_rate_failure_without_masking_capacity(self) -> None:
        calls = {"capacity": 0, "burn": 0}

        def capacity():
            calls["capacity"] += 1
            return self._capacity_result()

        def burn_rate(_provider_id: str, _model_id: str):
            calls["burn"] += 1
            raise RuntimeError("fixture telemetry unavailable")

        evaluator = SnapshottingShouldSendEvaluator(
            capacity=capacity,
            burn_rate=burn_rate,
            ttl_seconds=10.0,
            monotonic=lambda: 100.0,
        )

        first = evaluator(self.request)
        repeated = evaluator(self.request)

        self.assertEqual(first.decision, Decision.YES)
        self.assertIsNone(first.burn_rate_per_minute)
        self.assertEqual(first, repeated)
        self.assertEqual(calls, {"capacity": 1, "burn": 1})

    def test_snapshotting_evaluator_refreshes_after_clock_regression(self) -> None:
        now = [100.0]
        capacity_calls = 0

        def capacity():
            nonlocal capacity_calls
            capacity_calls += 1
            return self._capacity_result()

        evaluator = SnapshottingShouldSendEvaluator(
            capacity=capacity,
            ttl_seconds=10.0,
            monotonic=lambda: now[0],
        )

        evaluator(self.request)
        now[0] = 99.0
        evaluator(self.request)

        self.assertEqual(capacity_calls, 2)

    def test_non_finite_and_out_of_range_ratios_fail_closed(self) -> None:
        for remaining_ratio in (
            float("nan"),
            float("inf"),
            float("-inf"),
            -0.01,
            1.01,
        ):
            with self.subTest(remaining_ratio=remaining_ratio):
                result = self.policy.decide(
                    self.request,
                    PolicySnapshot(remaining_ratio, 850.0, 10.0, False),
                )

                self.assertEqual(result.decision, Decision.DEFER)
                self.assertEqual(result.reason, "quota_unknown")
                self.assertEqual(result.confidence, 0.5)

    def test_unknown_marker_overrides_apparently_healthy_values(self) -> None:
        result = self.policy.decide(
            self.request,
            PolicySnapshot(0.74, 850.0, 10.0, True),
        )

        self.assertEqual(result.decision, Decision.DEFER)
        self.assertEqual(result.reason, "quota_unknown")
        self.assertEqual(result.confidence, 0.5)

    def test_decision_metadata_is_deterministic_and_never_echoes_request(self) -> None:
        snapshot = PolicySnapshot(0.74, 850.0, 10.0, False)
        sensitive_request = ShouldSendRequest(
            "provider-sk-sensitive",
            "model-with-private-prompt",
            1234,
            "window-with-secret",
        )

        first = self.policy.decide(sensitive_request, snapshot)
        repeated = self.policy.decide(sensitive_request, snapshot)
        ordinary = self.policy.decide(self.request, snapshot)

        self.assertEqual(first, repeated)
        self.assertEqual(
            (first.decision, first.confidence, first.reason),
            (ordinary.decision, ordinary.confidence, ordinary.reason),
        )
        self.assertNotIn(sensitive_request.provider, first.reason)
        self.assertNotIn(sensitive_request.model, first.reason)
        self.assertNotIn(sensitive_request.window, first.reason)

    def test_capacity_result_builds_snapshot_without_live_adapter_calls(self) -> None:
        capacity = CapacityResult(
            "1.0",
            7,
            "2026-08-08T10:00:00Z",
            (
                CapacityProvider(
                    record_id="openai.daily",
                    provider_id="openai",
                    account_ref=None,
                    quota_name="Daily",
                    unit="tokens",
                    used="26000",
                    quota_limit="100000",
                    remaining="74000",
                    remaining_ratio=0.74,
                    resets_at=None,
                    period_start=None,
                    period_end=None,
                    observed_at="2026-08-08T09:59:00Z",
                    freshness_seconds=60,
                    state="ok",
                    quality="direct",
                    stale=False,
                    revision=7,
                    source_id="openai.quota",
                    quota_window="daily",
                    applies_to=QuotaAppliesTo("account", ()),
                ),
            ),
        )

        snapshot = PolicySnapshot.from_capacity_result(
            capacity,
            provider_id="openai",
        )

        self.assertEqual(
            snapshot,
            PolicySnapshot(0.74, 74000.0, None, False),
        )

    def test_token_capacity_and_token_burn_enable_production_path_prediction(self) -> None:
        snapshot = PolicySnapshot.from_capacity_result(
            self._capacity_result(remaining="74000", unit="tokens"),
            provider_id=self.request.provider,
            model_id=self.request.model,
            burn_rate_per_minute=10_000.0,
        )

        result = self.policy.decide(self.request, snapshot)

        self.assertEqual(snapshot.quota_remaining, 74_000.0)
        self.assertEqual(result.decision, Decision.DEFER)
        self.assertEqual(result.reason, "burn_rate_too_high")
        self.assertEqual(result.predicted_exhaustion_minutes, 7.4)

    def test_incompatible_capacity_unit_does_not_fabricate_prediction(self) -> None:
        snapshot = PolicySnapshot.from_capacity_result(
            self._capacity_result(remaining="74000", unit="requests"),
            provider_id=self.request.provider,
            model_id=self.request.model,
            burn_rate_per_minute=10_000.0,
        )

        result = self.policy.decide(self.request, snapshot)

        self.assertIsNone(snapshot.quota_remaining)
        self.assertEqual(result.decision, Decision.YES)
        self.assertIsNone(result.predicted_exhaustion_minutes)

    def test_missing_provider_snapshot_is_unknown(self) -> None:
        capacity = CapacityResult("1.0", 7, "2026-08-08T10:00:00Z", ())

        snapshot = PolicySnapshot.from_capacity_result(
            capacity,
            provider_id="openai",
        )

        self.assertEqual(snapshot, PolicySnapshot(None, None, None, True))

    def test_stale_capacity_snapshot_is_unknown_and_defers(self) -> None:
        capacity = self._capacity_result(stale=True)

        snapshot = PolicySnapshot.from_capacity_result(
            capacity,
            provider_id=self.request.provider,
            model_id=self.request.model,
        )
        result = self.policy.decide(self.request, snapshot)

        self.assertTrue(snapshot.unknown)
        self.assertEqual(result.decision, Decision.DEFER)
        self.assertEqual(result.reason, "quota_unknown")

    def test_model_scoped_capacity_does_not_apply_to_another_model(self) -> None:
        capacity = self._capacity_result(model_ids=("gpt-4.1",))

        snapshot = PolicySnapshot.from_capacity_result(
            capacity,
            provider_id=self.request.provider,
            model_id=self.request.model,
        )

        self.assertEqual(snapshot, PolicySnapshot(None, None, None, True))

    def test_model_scoped_capacity_applies_to_the_requested_model(self) -> None:
        capacity = self._capacity_result(model_ids=("gpt-4o",))

        snapshot = PolicySnapshot.from_capacity_result(
            capacity,
            provider_id=self.request.provider,
            model_id=self.request.model,
        )

        self.assertEqual(snapshot, PolicySnapshot(0.74, 74000.0, None, False))

    @staticmethod
    def _capacity_result(
        *,
        stale: bool = False,
        model_ids: tuple[str, ...] = (),
        remaining: str = "74000",
        unit: str = "tokens",
    ) -> CapacityResult:
        return CapacityResult(
            "1.0",
            7,
            "2026-08-08T10:00:00Z",
            (
                CapacityProvider(
                    record_id="openai.daily",
                    provider_id="openai",
                    account_ref=None,
                    quota_name="Daily",
                    unit=unit,
                    used="26000",
                    quota_limit="100000",
                    remaining=remaining,
                    remaining_ratio=0.74,
                    resets_at=None,
                    period_start=None,
                    period_end=None,
                    observed_at="2026-08-08T09:59:00Z",
                    freshness_seconds=60,
                    state="ok",
                    quality="direct",
                    stale=stale,
                    revision=7,
                    source_id="openai.quota",
                    quota_window="daily",
                    applies_to=QuotaAppliesTo("models", model_ids),
                ),
            ),
        )


if __name__ == "__main__":
    unittest.main()
