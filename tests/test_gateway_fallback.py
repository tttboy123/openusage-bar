from __future__ import annotations

import json
from pathlib import Path
import unittest

from openusage_bar.gateway.contracts import Decision
from openusage_bar.gateway.fallback import (
    BreakerState,
    Candidate,
    CircuitBreaker,
    FailureSignal,
    FallbackAction,
    FallbackDecision,
    FallbackSelector,
    HealthEvent,
    HealthSnapshot,
    HealthStatus,
    HealthWindow,
    ReplayState,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = (
    ROOT
    / "tests"
    / "fixtures"
    / "gateway"
    / "fallback"
    / "failure_cases.json"
)

SANITIZED_ERROR_CODES = frozenset(
    {
        "fallback_exhausted",
        "policy_rejected",
        "stream_interrupted",
        "upstream_rate_limited",
        "upstream_server_error",
        "upstream_timeout",
    }
)


def candidate(
    provider_id: str,
    *,
    model: str | None = None,
    estimated_cost: float = 1.0,
    should_send: Decision = Decision.YES,
    breaker_state: BreakerState = BreakerState.CLOSED,
    health_score: float = 1.0,
) -> Candidate:
    return Candidate(
        provider_id,
        model or f"{provider_id}-model",
        estimated_cost,
        should_send,
        breaker_state=breaker_state,
        health_score=health_score,
    )


def failure_cases() -> tuple[dict[str, str], ...]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return tuple(payload["cases"])


def terminal_failure_cases() -> tuple[dict[str, str], ...]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return tuple(payload["terminal_cases"])


class CircuitBreakerTests(unittest.TestCase):
    def test_breaker_transitions_closed_open_half_open_closed(self) -> None:
        breaker = CircuitBreaker(failure_threshold=2, reset_after_seconds=30)

        breaker.record_failure(now=0)
        self.assertEqual(breaker.state(now=0), BreakerState.CLOSED)
        breaker.record_failure(now=1)
        self.assertEqual(breaker.state(now=2), BreakerState.OPEN)
        self.assertEqual(breaker.state(now=31), BreakerState.HALF_OPEN)
        self.assertTrue(breaker.allow_request(now=31))

        breaker.record_success(now=32)

        self.assertEqual(breaker.state(now=33), BreakerState.CLOSED)

    def test_half_open_allows_one_probe_and_failure_reopens(self) -> None:
        breaker = CircuitBreaker(failure_threshold=1, reset_after_seconds=30)
        breaker.record_failure(now=0)

        self.assertFalse(breaker.allow_request(now=29.999))
        self.assertTrue(breaker.allow_request(now=30))
        self.assertFalse(breaker.allow_request(now=30))

        breaker.record_failure(now=30)

        self.assertEqual(breaker.state(now=59.999), BreakerState.OPEN)
        self.assertEqual(breaker.state(now=60), BreakerState.HALF_OPEN)

    def test_success_resets_consecutive_failures(self) -> None:
        breaker = CircuitBreaker(failure_threshold=2, reset_after_seconds=30)
        breaker.record_failure(now=0)
        breaker.record_success(now=1)
        breaker.record_failure(now=2)

        self.assertEqual(breaker.state(now=3), BreakerState.CLOSED)

    def test_half_open_success_requires_an_acquired_probe(self) -> None:
        breaker = CircuitBreaker(failure_threshold=1, reset_after_seconds=30)
        breaker.record_failure(now=0)
        self.assertEqual(breaker.state(now=30), BreakerState.HALF_OPEN)

        breaker.record_success(now=30)

        self.assertEqual(breaker.state(now=30), BreakerState.HALF_OPEN)
        self.assertTrue(breaker.allow_request(now=30))
        breaker.record_success(now=30)
        self.assertEqual(breaker.state(now=30), BreakerState.CLOSED)


class HealthWindowTests(unittest.TestCase):
    def test_only_events_inside_the_five_minute_window_contribute(self) -> None:
        health = HealthWindow(window_seconds=300)
        health.record(
            HealthEvent(
                provider_id="openai",
                status=HealthStatus.SERVER_ERROR,
                latency_ms=500,
                observed_at=0,
            )
        )
        health.record(
            HealthEvent(
                provider_id="openai",
                status=HealthStatus.SUCCESS,
                latency_ms=50,
                observed_at=100,
            )
        )

        snapshot = health.snapshot("openai", now=301)

        self.assertEqual(snapshot.window_seconds, 300)
        self.assertEqual(snapshot.sample_count, 1)
        self.assertEqual(snapshot.success_rate, 1.0)
        self.assertEqual(snapshot.server_error_count, 0)
        self.assertEqual(snapshot.p50_latency_ms, 50)
        self.assertEqual(snapshot.p99_latency_ms, 50)

    def test_health_snapshot_contains_only_bounded_sanitized_aggregates(self) -> None:
        health = HealthWindow(window_seconds=300)
        events = (
            HealthEvent("openai", HealthStatus.SUCCESS, 100, 10),
            HealthEvent("openai", HealthStatus.SUCCESS, 200, 20),
            HealthEvent("openai", HealthStatus.RATE_LIMIT, 300, 30),
            HealthEvent("openai", HealthStatus.SERVER_ERROR, 400, 40),
            HealthEvent("openai", HealthStatus.TIMEOUT, None, 50),
        )
        for event in events:
            health.record(event)

        snapshot = health.snapshot("openai", now=60)
        public = snapshot.to_public_dict()

        self.assertEqual(snapshot.sample_count, 5)
        self.assertEqual(snapshot.success_rate, 0.4)
        self.assertEqual(snapshot.rate_limit_count, 1)
        self.assertEqual(snapshot.server_error_count, 1)
        self.assertEqual(snapshot.timeout_count, 1)
        self.assertIsNotNone(snapshot.p50_latency_ms)
        self.assertIsNotNone(snapshot.p99_latency_ms)
        self.assertLessEqual(snapshot.p50_latency_ms, snapshot.p99_latency_ms)
        self.assertGreaterEqual(snapshot.health_score, 0.0)
        self.assertLessEqual(snapshot.health_score, 1.0)
        self.assertEqual(
            set(public),
            {
                "providerId",
                "windowSeconds",
                "sampleCount",
                "successRate",
                "p50LatencyMs",
                "p99LatencyMs",
                "rateLimitCount",
                "serverErrorCount",
                "timeoutCount",
                "healthScore",
            },
        )
        serialized = json.dumps(public, sort_keys=True).casefold()
        for forbidden in (
            "authorization",
            "credential",
            "prompt",
            "raw",
            "request_body",
            "response",
            "secret",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_successful_provider_scores_above_identically_sized_outage(self) -> None:
        healthy = HealthWindow(window_seconds=300)
        outage = HealthWindow(window_seconds=300)
        for index in range(5):
            healthy.record(
                HealthEvent(
                    "openai",
                    HealthStatus.SUCCESS,
                    40 + index,
                    index,
                )
            )
            outage.record(
                HealthEvent(
                    "anthropic",
                    HealthStatus.SERVER_ERROR,
                    40 + index,
                    index,
                )
            )

        healthy_score = healthy.snapshot("openai", now=5).health_score
        outage_score = outage.snapshot("anthropic", now=5).health_score

        self.assertGreater(healthy_score, outage_score)

    def test_future_event_does_not_evict_valid_current_window_event(self) -> None:
        health = HealthWindow(window_seconds=300)
        health.record(
            HealthEvent("openai", HealthStatus.SUCCESS, 50, 100)
        )
        health.record(
            HealthEvent("openai", HealthStatus.SERVER_ERROR, 500, 10_000)
        )

        snapshot = health.snapshot("openai", now=151)

        self.assertEqual(snapshot.sample_count, 1)
        self.assertEqual(snapshot.success_rate, 1.0)
        self.assertEqual(snapshot.server_error_count, 0)

    def test_out_of_order_events_inside_the_window_are_all_preserved(self) -> None:
        health = HealthWindow(window_seconds=300)
        for event in (
            HealthEvent("openai", HealthStatus.SUCCESS, 40, 200),
            HealthEvent("openai", HealthStatus.RATE_LIMIT, 80, 100),
            HealthEvent("openai", HealthStatus.SUCCESS, 60, 150),
        ):
            health.record(event)

        snapshot = health.snapshot("openai", now=250)

        self.assertEqual(snapshot.sample_count, 3)
        self.assertEqual(snapshot.success_rate, 0.666667)
        self.assertEqual(snapshot.rate_limit_count, 1)


class FallbackSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selector = FallbackSelector(cost_cap_multiplier=3.0)

    def test_selector_applies_should_send_and_cost_cap(self) -> None:
        selected = self.selector.select(
            primary_cost=1.0,
            candidates=(
                candidate("anthropic", model="sonnet", estimated_cost=4.0),
                candidate("ollama", model="qwen", estimated_cost=0.0),
            ),
        )

        self.assertEqual(
            selected,
            candidate("ollama", model="qwen", estimated_cost=0.0),
        )

    def test_open_breaker_blocks_a_sticky_candidate(self) -> None:
        safe = candidate("anthropic", health_score=0.6)
        sticky_but_open = candidate(
            "openai",
            breaker_state=BreakerState.OPEN,
            health_score=0.99,
        )

        selected = self.selector.select(
            primary_cost=1.0,
            candidates=(safe, sticky_but_open),
            sticky_provider_id="openai",
        )

        self.assertEqual(selected, safe)

    def test_health_ranking_precedes_sticky_preference(self) -> None:
        safe = candidate("anthropic", health_score=0.9)
        sticky_but_unhealthy = candidate("openai", health_score=0.2)

        selected = self.selector.select(
            primary_cost=1.0,
            candidates=(safe, sticky_but_unhealthy),
            sticky_provider_id="openai",
        )

        self.assertEqual(selected, safe)

    def test_low_health_is_downranked_but_not_an_implicit_hard_failure(self) -> None:
        only_safe_choice = candidate("ollama", health_score=0.2)

        selected = self.selector.select(
            primary_cost=1.0,
            candidates=(only_safe_choice,),
        )

        self.assertEqual(selected, only_safe_choice)

    def test_should_send_no_or_defer_blocks_a_sticky_candidate(self) -> None:
        for decision in (Decision.NO, Decision.DEFER):
            safe = candidate("anthropic", health_score=0.6)
            sticky_but_rejected = candidate(
                "openai",
                should_send=decision,
                health_score=0.99,
            )
            with self.subTest(decision=decision):
                selected = self.selector.select(
                    primary_cost=1.0,
                    candidates=(safe, sticky_but_rejected),
                    sticky_provider_id="openai",
                )
                self.assertEqual(selected, safe)

    def test_cost_cap_blocks_a_sticky_candidate(self) -> None:
        safe = candidate(
            "ollama",
            estimated_cost=0.0,
            health_score=0.6,
        )
        sticky_but_too_expensive = candidate(
            "openai",
            estimated_cost=3.01,
            health_score=0.99,
        )

        selected = self.selector.select(
            primary_cost=1.0,
            candidates=(safe, sticky_but_too_expensive),
            sticky_provider_id="openai",
        )

        self.assertEqual(selected, safe)

    def test_sticky_provider_wins_only_after_all_safety_gates_pass(self) -> None:
        first = candidate("anthropic", health_score=0.9)
        sticky = candidate("openai", health_score=0.9)

        selected = self.selector.select(
            primary_cost=1.0,
            candidates=(first, sticky),
            sticky_provider_id="openai",
        )

        self.assertEqual(selected, sticky)


class FallbackDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.safe_candidate = candidate(
            "ollama",
            model="qwen",
            estimated_cost=0.0,
        )

    def test_failure_fixtures_allow_at_most_one_retry_by_default(self) -> None:
        selector = FallbackSelector(cost_cap_multiplier=3.0)

        for case in failure_cases():
            signal = FailureSignal(case["signal"])
            with self.subTest(case=case["name"], phase="first fallback"):
                decision = selector.decide(
                    failure=signal,
                    retries_used=0,
                    primary_cost=1.0,
                    candidates=(self.safe_candidate,),
                )
                self.assertEqual(decision.action, FallbackAction.RETRY)
                self.assertEqual(decision.candidate, self.safe_candidate)
                self.assertEqual(
                    decision.error_code,
                    case["sanitized_error_code"],
                )
                self.assertTrue(decision.retryable)

            with self.subTest(case=case["name"], phase="retry cap"):
                exhausted = selector.decide(
                    failure=signal,
                    retries_used=1,
                    primary_cost=1.0,
                    candidates=(self.safe_candidate,),
                )
                self.assertEqual(exhausted.action, FallbackAction.FAIL)
                self.assertIsNone(exhausted.candidate)
                self.assertEqual(exhausted.error_code, "fallback_exhausted")
                self.assertFalse(exhausted.retryable)

    def test_terminal_failure_signal_itself_forbids_retry_queue_and_degrade(self) -> None:
        for case in terminal_failure_cases():
            signal = FailureSignal(case["signal"])
            scenarios = (
                (
                    "retry",
                    FallbackSelector(cost_cap_multiplier=3.0),
                    (self.safe_candidate,),
                    None,
                ),
                (
                    "queue",
                    FallbackSelector(
                        cost_cap_multiplier=3.0,
                        on_exhausted=FallbackAction.QUEUE,
                    ),
                    (),
                    None,
                ),
                (
                    "degrade",
                    FallbackSelector(
                        cost_cap_multiplier=3.0,
                        on_exhausted=FallbackAction.DEGRADE_TO_CHEAP,
                    ),
                    (),
                    self.safe_candidate,
                ),
            )
            for scenario, selector, candidates, cheap_candidate in scenarios:
                with self.subTest(case=case["name"], scenario=scenario):
                    decision = selector.decide(
                        failure=signal,
                        retries_used=0,
                        primary_cost=1.0,
                        candidates=candidates,
                        cheap_candidate=cheap_candidate,
                    )
                    self.assertEqual(decision.action, FallbackAction.FAIL)
                    self.assertIsNone(decision.candidate)
                    self.assertEqual(
                        decision.error_code,
                        case["sanitized_error_code"],
                    )
                    self.assertFalse(decision.retryable)

    def test_tool_side_effect_and_client_abort_never_replay_or_queue(self) -> None:
        selector = FallbackSelector(
            cost_cap_multiplier=3.0,
            on_exhausted=FallbackAction.QUEUE,
        )
        cases = (
            ReplayState(tool_call_observed=True),
            ReplayState(provider_side_effect_observed=True),
            ReplayState(client_aborted=True),
        )

        for replay_state in cases:
            with self.subTest(replay_state=replay_state):
                decision = selector.decide(
                    failure=FailureSignal.TIMEOUT,
                    retries_used=0,
                    primary_cost=1.0,
                    candidates=(self.safe_candidate,),
                    replay_state=replay_state,
                )
                self.assertEqual(decision.action, FallbackAction.FAIL)
                self.assertIsNone(decision.candidate)
                self.assertFalse(decision.retryable)
                self.assertIn(decision.error_code, SANITIZED_ERROR_CODES)

    def test_all_failed_policies_have_deterministic_sanitized_outcomes(self) -> None:
        unavailable = candidate(
            "anthropic",
            breaker_state=BreakerState.OPEN,
        )
        cheap = candidate(
            "ollama",
            model="qwen-cheap",
            estimated_cost=0.0,
        )
        cases = (
            (FallbackAction.QUEUE, None),
            (FallbackAction.FAIL, None),
            (FallbackAction.DEGRADE_TO_CHEAP, cheap),
        )

        for action, expected_candidate in cases:
            selector = FallbackSelector(
                cost_cap_multiplier=3.0,
                on_exhausted=action,
            )
            kwargs = {
                "failure": FailureSignal.SERVER_ERROR,
                "retries_used": 0,
                "primary_cost": 1.0,
                "candidates": (unavailable,),
                "cheap_candidate": cheap,
            }
            with self.subTest(action=action):
                first = selector.decide(**kwargs)
                repeated = selector.decide(**kwargs)
                self.assertEqual(first, repeated)
                self.assertEqual(first.action, action)
                self.assertEqual(first.candidate, expected_candidate)
                self.assertEqual(first.error_code, "fallback_exhausted")
                self.assertIn(first.error_code, SANITIZED_ERROR_CODES)

                public = first.to_public_dict()
                serialized = json.dumps(public, sort_keys=True).casefold()
                for forbidden in (
                    "authorization",
                    "credential",
                    "prompt",
                    "raw",
                    "requestbody",
                    "response",
                    "secret",
                ):
                    self.assertNotIn(forbidden, serialized)

    def test_unsafe_cheap_candidate_cannot_bypass_replay_gates(self) -> None:
        unsafe_cheap = candidate(
            "ollama",
            model="qwen-cheap",
            estimated_cost=0.0,
            breaker_state=BreakerState.OPEN,
        )
        selector = FallbackSelector(
            cost_cap_multiplier=3.0,
            on_exhausted=FallbackAction.DEGRADE_TO_CHEAP,
        )

        decision = selector.decide(
            failure=FailureSignal.TIMEOUT,
            retries_used=0,
            primary_cost=1.0,
            candidates=(),
            cheap_candidate=unsafe_cheap,
        )

        self.assertEqual(decision.action, FallbackAction.FAIL)
        self.assertIsNone(decision.candidate)
        self.assertEqual(decision.error_code, "fallback_exhausted")

    def test_degrade_to_cheap_still_respects_the_global_retry_cap(self) -> None:
        selector = FallbackSelector(
            cost_cap_multiplier=3.0,
            on_exhausted=FallbackAction.DEGRADE_TO_CHEAP,
        )

        decision = selector.decide(
            failure=FailureSignal.TIMEOUT,
            retries_used=1,
            primary_cost=1.0,
            candidates=(),
            cheap_candidate=self.safe_candidate,
        )

        self.assertEqual(decision.action, FallbackAction.FAIL)
        self.assertIsNone(decision.candidate)
        self.assertEqual(decision.error_code, "fallback_exhausted")


class TamperSafetyTests(unittest.TestCase):
    SECRET = "sk-review-secret-material"

    def assert_safe_boundary(self, operation) -> None:
        try:
            result = operation()
        except ValueError as error:
            self.assertNotIn(self.SECRET, str(error))
            self.assertNotIn(self.SECRET, repr(error))
            return
        except Exception as error:
            self.fail(
                "tampered public boundary raised unexpected "
                f"{type(error).__name__}"
            )
        self.assertNotIn(self.SECRET, repr(result))

    def test_candidate_repr_is_safe_after_frozen_instance_tampering(self) -> None:
        value = candidate("ollama", estimated_cost=0.0)
        object.__setattr__(value, "estimated_cost", self.SECRET)

        self.assert_safe_boundary(lambda: repr(value))

    def test_health_snapshot_public_forms_are_safe_after_tampering(self) -> None:
        value = HealthSnapshot(
            provider_id="openai",
            window_seconds=300,
            sample_count=1,
            success_rate=1.0,
            p50_latency_ms=50,
            p99_latency_ms=50,
            rate_limit_count=0,
            server_error_count=0,
            timeout_count=0,
            health_score=1.0,
        )
        object.__setattr__(value, "health_score", self.SECRET)

        with self.subTest(boundary="repr"):
            self.assert_safe_boundary(lambda: repr(value))
        with self.subTest(boundary="public"):
            self.assert_safe_boundary(value.to_public_dict)

    def test_fallback_decision_public_forms_are_safe_after_tampering(self) -> None:
        value = FallbackDecision(
            FallbackAction.RETRY,
            candidate("ollama", estimated_cost=0.0),
            "upstream_timeout",
            True,
        )
        object.__setattr__(value, "error_code", self.SECRET)

        with self.subTest(boundary="repr"):
            self.assert_safe_boundary(lambda: repr(value))
        with self.subTest(boundary="public"):
            self.assert_safe_boundary(value.to_public_dict)


class NumericBoundaryTests(unittest.TestCase):
    def test_huge_numbers_fail_closed_as_value_error_at_public_boundaries(self) -> None:
        huge = 10**10_000
        selector = FallbackSelector(cost_cap_multiplier=3.0)
        operations = (
            (
                "candidate cost",
                lambda: candidate("ollama", estimated_cost=huge),
            ),
            (
                "health event clock",
                lambda: HealthEvent(
                    "openai",
                    HealthStatus.SUCCESS,
                    50,
                    huge,
                ),
            ),
            (
                "health event latency",
                lambda: HealthEvent(
                    "openai",
                    HealthStatus.SUCCESS,
                    huge,
                    0,
                ),
            ),
            (
                "breaker reset",
                lambda: CircuitBreaker(
                    failure_threshold=1,
                    reset_after_seconds=huge,
                ),
            ),
            (
                "cost multiplier",
                lambda: FallbackSelector(cost_cap_multiplier=huge),
            ),
            (
                "primary cost",
                lambda: selector.select(
                    primary_cost=huge,
                    candidates=(),
                ),
            ),
            (
                "health snapshot clock",
                lambda: HealthWindow(window_seconds=300).snapshot(
                    "openai",
                    now=huge,
                ),
            ),
        )

        for boundary, operation in operations:
            with self.subTest(boundary=boundary):
                with self.assertRaises(ValueError):
                    operation()

if __name__ == "__main__":
    unittest.main()
