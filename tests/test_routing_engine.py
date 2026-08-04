import dataclasses
import unittest
from unittest.mock import patch


def target(
    target_id: str,
    *,
    provider_id: str = "openai",
    model_id: str = "gpt-5",
    enabled: bool = True,
    adapter_available: bool = True,
    privacy_class: str = "direct_provider",
    capabilities: frozenset[str] = frozenset({"chat", "reasoning", "tools"}),
    regions: frozenset[str] = frozenset({"global"}),
    context_window_tokens: int = 400_000,
    resource_mode: str = "quota",
    runtime_scope_ref: str | None = "anon_0123456789abcdef",
    fact_account_ref: str | None = "account-1",
    balance_currency: str | None = None,
    cost_currency: str | None = "USD",
    input_cost_micros_per_million: int | None = 3_000_000,
    output_cost_micros_per_million: int | None = 3_000_000,
):
    from openusage_bar.routing_contract import RouteTarget

    return RouteTarget(
        target_id=target_id,
        provider_id=provider_id,
        account_ref="account-1",
        model_id=model_id,
        connection_ref="connection-1",
        execution_class="direct_api",
        execution_adapter_id="openai.direct",
        resource_mode=resource_mode,
        runtime_scope_ref=runtime_scope_ref,
        fact_account_ref=fact_account_ref,
        balance_currency=(
            balance_currency
            if balance_currency is not None or resource_mode != "balance"
            else "USD"
        ),
        cost_currency=cost_currency,
        input_cost_micros_per_million=input_cost_micros_per_million,
        output_cost_micros_per_million=output_cost_micros_per_million,
        enabled=enabled,
        adapter_available=adapter_available,
        regions=regions,
        privacy_class=privacy_class,
        capabilities=capabilities,
        context_window_tokens=context_window_tokens,
        quality_tier=4,
    )


def facts(
    target_id: str,
    *,
    connection_state: str = "available",
    source_state: str = "ok",
    resource_state: str = "complete",
    headroom_bp: int | None = 8_000,
    runtime_state: str = "complete",
    observation_count: int = 100,
    error_count: int = 1,
    duration_p95_ms: int | None = 1_000,
    ttft_p95_ms: int | None = 100,
    estimated_cost_micros: int | None = 100,
    estimated_cost_currency: str | None = "USD",
    resource_mode: str = "quota",
    balance_micros: int | None = None,
    balance_currency: str | None = None,
):
    from openusage_bar.routing_contract import TargetFacts

    return TargetFacts(
        target_id=target_id,
        connection_state=connection_state,
        source_state=source_state,
        resource_state=resource_state,
        resource_mode=resource_mode,
        headroom_bp=headroom_bp,
        balance_micros=balance_micros,
        balance_currency=balance_currency,
        runtime_state=runtime_state,
        observation_count=observation_count,
        error_count=error_count,
        duration_p95_ms=duration_p95_ms,
        ttft_p95_ms=ttft_p95_ms,
        estimated_cost_micros=estimated_cost_micros,
        estimated_cost_currency=(
            estimated_cost_currency
            if estimated_cost_micros is not None
            else None
        ),
    )


def request(*, policy_id: str = "reliable", **task_changes):
    from openusage_bar.routing_contract import RouteRequest, RouteTask

    values = {
        "kind": "code",
        "required_capabilities": frozenset({"chat", "reasoning", "tools"}),
        "estimated_input_tokens": 12_000,
        "max_output_tokens": 4_000,
        "minimum_context_window_tokens": 16_000,
        "privacy": "direct_provider",
        "regions": frozenset({"global"}),
    }
    values.update(task_changes)
    return RouteRequest(policy_id=policy_id, task=RouteTask(**values))


def context():
    from openusage_bar.routing_contract import DecisionContext

    return DecisionContext(
        generated_at="2026-08-02T12:00:00Z",
        expires_at="2026-08-02T12:00:30Z",
        data_revision=60_000,
        runtime_revision=120,
    )


def decide(route_request, targets, target_facts, *, policy_id: str | None = None):
    from openusage_bar.routing_engine import decide_route
    from openusage_bar.routing_policy import built_in_policy

    return decide_route(
        route_request,
        tuple(targets),
        tuple(target_facts),
        built_in_policy(policy_id or route_request.policy_id),
        context(),
    )


class RoutingContractTests(unittest.TestCase):
    def test_contract_values_are_frozen_and_collections_are_canonical(self):
        from openusage_bar.routing_contract import RouteConstraints, RouteRequest

        route_target = target(
            "openai.work.gpt-5",
            capabilities=frozenset({"tools", "chat"}),
            regions=frozenset({"us", "global"}),
        )
        self.assertEqual(route_target.capabilities, ("chat", "tools"))
        self.assertEqual(route_target.regions, ("global", "us"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            route_target.enabled = False

        constraints = RouteConstraints(
            allow_providers=("openai", "openai"),
            deny_targets=("disabled",),
        )
        canonical = RouteRequest(
            policy_id="reliable",
            task=request().task,
            constraints=constraints,
        )
        self.assertEqual(canonical.constraints.allow_providers, ("openai",))

    def test_contract_rejects_invalid_ids_enums_booleans_and_bounds(self):
        from openusage_bar.routing_contract import (
            DecisionContext,
            RouteSession,
            RouteTarget,
            RouteTask,
            TargetFacts,
        )

        cases = (
            lambda: target("openai/account/gpt"),
            lambda: RouteTarget(**{
                **dataclasses.asdict(target("valid")),
                "execution_class": "shell",
            }),
            lambda: RouteTarget(**{
                **dataclasses.asdict(target("valid")),
                "resource_mode": "unlimited",
            }),
            lambda: RouteTarget(**{
                **dataclasses.asdict(target("valid")),
                "runtime_scope_ref": "customer@example.com",
            }),
            lambda: RouteTarget(**{
                **dataclasses.asdict(target("valid")),
                "cost_currency": None,
            }),
            lambda: RouteTask(**{
                **dataclasses.asdict(request().task),
                "estimated_input_tokens": True,
            }),
            lambda: TargetFacts(**{
                **dataclasses.asdict(facts("valid")),
                "headroom_bp": 10_001,
            }),
            lambda: RouteSession(
                session_ref="customer@example.com",
                remaining_budget_micros=1,
                reserve_micros=0,
            ),
            lambda: DecisionContext(
                generated_at="not-utc",
                expires_at="2026-08-02T12:00:30Z",
                data_revision=1,
                runtime_revision=None,
            ),
        )
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    case()

    def test_decision_outputs_are_deeply_frozen_bounded_and_canonical(self):
        from openusage_bar.routing_contract import (
            RejectedTarget,
            RouteDecision,
            ScoreComponents,
            ScoredTarget,
        )

        components = ScoreComponents(
            reliability=9_000,
            headroom=8_000,
            latency=7_000,
            cost=6_000,
        )
        selected = ScoredTarget(
            target_id="openai.work.gpt-5",
            provider_id="openai",
            account_ref="account-1",
            model_id="gpt-5",
            score=8_500,
            components=components,
            reasons=("healthy_source", "quota_headroom"),
        )
        alternative = ScoredTarget(
            target_id="openai.work.gpt-5-mini",
            provider_id="openai",
            account_ref="account-1",
            model_id="gpt-5-mini",
            score=7_500,
            components=components,
            reasons=("healthy_source",),
        )
        rejected = RejectedTarget(
            target_id="minimax.work.m2",
            reason_codes=("fact_stale",),
        )
        decision = RouteDecision(
            generated_at="2026-08-02T12:00:00Z",
            expires_at="2026-08-02T12:00:30Z",
            policy_id="reliable",
            policy_revision=1,
            data_revision=60_000,
            runtime_revision=120,
            selected=selected,
            alternatives=(alternative,),
            rejected=(rejected,),
        )

        self.assertEqual(decision.alternatives, (alternative,))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            decision.policy_revision = 2

        invalid_cases = (
            lambda: ScoreComponents(True, 0, 0, 0),
            lambda: ScoreComponents(10_001, 0, 0, 0),
            lambda: dataclasses.replace(selected, reasons=["healthy_source"]),
            lambda: dataclasses.replace(selected, reasons=("customer@example.com",)),
            lambda: dataclasses.replace(selected, score=10_001),
            lambda: dataclasses.replace(rejected, reason_codes=()),
            lambda: dataclasses.replace(decision, alternatives=(selected,)),
            lambda: dataclasses.replace(decision, selected=None, alternatives=(alternative,)),
            lambda: dataclasses.replace(decision, alternatives=(alternative,) * 17),
            lambda: dataclasses.replace(decision, rejected=(rejected,) * 129),
        )
        for case in invalid_cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                case()


class RoutingEngineTests(unittest.TestCase):
    def test_complete_fact_map_does_not_construct_missing_fact_fallbacks(self):
        route_target = target("openai.work.gpt-5")
        target_facts = facts(route_target.target_id)

        with patch(
            "openusage_bar.routing_engine._missing_facts",
            side_effect=AssertionError("unexpected missing fallback"),
        ):
            decision = decide(request(), [route_target], [target_facts])

        self.assertEqual(decision.selected.target_id, route_target.target_id)

    def test_reliability_first_prefers_healthier_target_over_cheaper_target(self):
        healthy = facts(
            "openai.work.gpt-5",
            error_count=0,
            duration_p95_ms=900,
            estimated_cost_micros=800,
        )
        cheap_unreliable = facts(
            "minimax.work.m3",
            error_count=18,
            duration_p95_ms=300,
            estimated_cost_micros=10,
        )

        decision = decide(
            request(),
            [target("openai.work.gpt-5"), target("minimax.work.m3", provider_id="minimax", model_id="minimax-m3")],
            [healthy, cheap_unreliable],
        )

        self.assertEqual(decision.selected.target_id, "openai.work.gpt-5")
        self.assertEqual(decision.alternatives[0].target_id, "minimax.work.m3")
        self.assertGreater(decision.selected.score, decision.alternatives[0].score)
        self.assertIn("healthy_source", decision.selected.reasons)
        self.assertEqual(decision.data_revision, 60_000)
        self.assertEqual(decision.runtime_revision, 120)

    def test_hard_filters_run_before_scoring_and_keep_stable_reason_order(self):
        route_target = target(
            "bad",
            enabled=False,
            adapter_available=False,
            capabilities=frozenset({"chat"}),
            context_window_tokens=8_000,
            privacy_class="proxy",
            regions=frozenset({"eu"}),
        )
        route_facts = facts(
            "bad",
            connection_state="unavailable",
            source_state="authentication_failed",
            resource_state="stale",
            headroom_bp=100,
            error_count=100,
        )

        decision = decide(request(), [route_target], [route_facts])

        self.assertIsNone(decision.selected)
        self.assertEqual(
            decision.rejected[0].reason_codes,
            (
                "target_disabled",
                "adapter_unavailable",
                "capability_missing",
                "context_too_small",
                "privacy_incompatible",
                "region_incompatible",
                "connection_unavailable",
                "source_unhealthy",
                "fact_stale",
                "quota_reserve_exceeded",
                "error_rate_exceeded",
            ),
        )

    def test_missing_partial_and_stale_resource_facts_never_become_zero(self):
        for state, expected in (
            ("missing", "fact_missing"),
            ("partial", "coverage_partial"),
            ("stale", "fact_stale"),
        ):
            with self.subTest(state=state):
                decision = decide(
                    request(),
                    [target("candidate")],
                    [facts("candidate", resource_state=state, headroom_bp=None)],
                )
                self.assertIsNone(decision.selected)
                self.assertIn(expected, decision.rejected[0].reason_codes)
                self.assertNotIn("quota_reserve_exceeded", decision.rejected[0].reason_codes)

    def test_missing_target_facts_return_no_route_instead_of_raising(self):
        decision = decide(request(), [target("candidate")], [])

        self.assertIsNone(decision.selected)
        self.assertEqual(
            decision.rejected[0].reason_codes,
            ("connection_unavailable", "source_unhealthy", "fact_missing"),
        )

    def test_ties_are_resolved_by_canonical_target_id(self):
        decision = decide(
            request(),
            [target("z-target"), target("a-target")],
            [facts("z-target"), facts("a-target")],
        )

        self.assertEqual(decision.selected.target_id, "a-target")
        self.assertEqual(decision.alternatives[0].target_id, "z-target")

    def test_request_allow_and_deny_lists_are_hard_constraints(self):
        from openusage_bar.routing_contract import RouteConstraints, RouteRequest

        base = request()
        constrained = RouteRequest(
            policy_id=base.policy_id,
            task=base.task,
            constraints=RouteConstraints(
                allow_providers=("openai",),
                deny_targets=("openai.denied",),
            ),
        )
        decision = decide(
            constrained,
            [
                target("openai.allowed"),
                target("openai.denied"),
                target("minimax.denied", provider_id="minimax"),
            ],
            [facts("openai.allowed"), facts("openai.denied"), facts("minimax.denied")],
        )

        self.assertEqual(decision.selected.target_id, "openai.allowed")
        self.assertEqual(
            {item.target_id: item.reason_codes for item in decision.rejected},
            {
                "minimax.denied": ("not_allowed",),
                "openai.denied": ("not_allowed",),
            },
        )

    def test_quota_cost_and_session_reserves_fail_closed(self):
        from openusage_bar.routing_contract import (
            RouteConstraints,
            RouteRequest,
            RouteSession,
        )

        base = request()
        route_request = RouteRequest(
            policy_id=base.policy_id,
            task=base.task,
            constraints=RouteConstraints(
                maximum_estimated_cost_micros=300,
                cost_currency="USD",
            ),
            session=RouteSession(
                session_ref="anon_0123456789abcdef",
                remaining_budget_micros=500,
                reserve_micros=250,
                budget_currency="USD",
            ),
        )
        decision = decide(
            route_request,
            [target("low-quota"), target("too-expensive"), target("unknown-cost")],
            [
                facts("low-quota", headroom_bp=100, estimated_cost_micros=100),
                facts("too-expensive", estimated_cost_micros=400),
                facts("unknown-cost", estimated_cost_micros=None),
            ],
        )

        self.assertIsNone(decision.selected)
        reasons = {item.target_id: item.reason_codes for item in decision.rejected}
        self.assertIn("quota_reserve_exceeded", reasons["low-quota"])
        self.assertIn("cost_limit_exceeded", reasons["too-expensive"])
        self.assertIn("session_budget_exceeded", reasons["too-expensive"])
        self.assertIn("cost_unknown", reasons["unknown-cost"])

    def test_balance_targets_require_post_request_reserve_and_matching_currency(self):
        safe = facts(
            "safe",
            resource_mode="balance",
            headroom_bp=None,
            balance_micros=2_000_000,
            balance_currency="USD",
            estimated_cost_micros=500_000,
            estimated_cost_currency="USD",
        )
        below_reserve = facts(
            "below-reserve",
            resource_mode="balance",
            headroom_bp=None,
            balance_micros=1_400_000,
            balance_currency="USD",
            estimated_cost_micros=500_000,
            estimated_cost_currency="USD",
        )
        unknown_cost = facts(
            "unknown-cost",
            resource_mode="balance",
            headroom_bp=None,
            balance_micros=5_000_000,
            balance_currency="CNY",
            estimated_cost_micros=None,
            estimated_cost_currency=None,
        )

        decision = decide(
            request(),
            [
                target("safe", resource_mode="balance"),
                target("below-reserve", resource_mode="balance"),
                target(
                    "unknown-cost",
                    resource_mode="balance",
                    balance_currency="CNY",
                    cost_currency="CNY",
                ),
            ],
            [safe, below_reserve, unknown_cost],
        )

        self.assertEqual(decision.selected.target_id, "safe")
        rejected = {value.target_id: value.reason_codes for value in decision.rejected}
        self.assertIn("balance_reserve_exceeded", rejected["below-reserve"])
        self.assertIn("cost_unknown", rejected["unknown-cost"])

    def test_cost_limit_and_session_budget_reject_currency_mismatch(self):
        from openusage_bar.routing_contract import RouteConstraints, RouteRequest, RouteSession

        base = request()
        route_request = RouteRequest(
            policy_id=base.policy_id,
            task=base.task,
            constraints=RouteConstraints(
                maximum_estimated_cost_micros=1_000_000,
                cost_currency="USD",
            ),
            session=RouteSession(
                session_ref="anon_0123456789abcdef",
                remaining_budget_micros=2_000_000,
                reserve_micros=1_000_000,
                budget_currency="USD",
            ),
        )
        decision = decide(
            route_request,
            [target("cny", cost_currency="CNY")],
            [facts("cny", estimated_cost_currency="CNY")],
        )

        self.assertIsNone(decision.selected)
        self.assertIn("cost_unknown", decision.rejected[0].reason_codes)

    def test_runtime_error_threshold_applies_only_after_minimum_samples(self):
        decision = decide(
            request(),
            [target("few-samples"), target("enough-samples")],
            [
                facts("few-samples", observation_count=4, error_count=4),
                facts("enough-samples", observation_count=5, error_count=2),
            ],
        )

        self.assertEqual(decision.selected.target_id, "few-samples")
        self.assertEqual(decision.rejected[0].target_id, "enough-samples")
        self.assertEqual(decision.rejected[0].reason_codes, ("error_rate_exceeded",))

    def test_economy_policy_requires_known_cost_and_can_choose_cheaper_target(self):
        reliable_decision = decide(
            request(),
            [target("reliable"), target("cheap")],
            [
                facts("reliable", error_count=0, estimated_cost_micros=900),
                facts("cheap", error_count=18, estimated_cost_micros=10),
            ],
        )
        economy_request = request(policy_id="economy")
        economy_decision = decide(
            economy_request,
            [target("reliable"), target("cheap"), target("unknown")],
            [
                facts("reliable", error_count=0, estimated_cost_micros=900),
                facts("cheap", error_count=18, estimated_cost_micros=10),
                facts("unknown", estimated_cost_micros=None),
            ],
        )

        self.assertEqual(reliable_decision.selected.target_id, "reliable")
        self.assertEqual(economy_decision.selected.target_id, "cheap")
        self.assertEqual(economy_decision.rejected[0].target_id, "unknown")
        self.assertEqual(economy_decision.rejected[0].reason_codes, ("cost_unknown",))

    def test_duplicate_target_or_fact_ids_are_rejected_as_programmer_errors(self):
        with self.assertRaisesRegex(ValueError, "duplicate route target"):
            decide(
                request(),
                [target("same"), target("same")],
                [facts("same")],
            )
        with self.assertRaisesRegex(ValueError, "duplicate target facts"):
            decide(
                request(),
                [target("same")],
                [facts("same"), facts("same")],
            )


if __name__ == "__main__":
    unittest.main()
