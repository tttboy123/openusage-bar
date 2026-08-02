import dataclasses
import unittest

from tests.test_routing_engine import context, facts, request, target


class RoutingEvaluationTests(unittest.TestCase):
    def test_shadow_comparison_reports_agreement_and_counterfactual_deltas(self):
        from openusage_bar.routing_engine import decide_route
        from openusage_bar.routing_evaluation import compare_shadow
        from openusage_bar.routing_policy import built_in_policy

        targets = (
            target("openai.fast", input_cost_micros_per_million=1_000_000,
                   output_cost_micros_per_million=1_000_000),
            target("openai.actual", input_cost_micros_per_million=4_000_000,
                   output_cost_micros_per_million=4_000_000),
        )
        frozen_facts = (
            facts("openai.fast", duration_p95_ms=300, estimated_cost_micros=25),
            facts("openai.actual", duration_p95_ms=900, estimated_cost_micros=100,
                  error_count=10),
        )
        decision = decide_route(
            request(), targets, frozen_facts, built_in_policy("reliable"), context()
        )

        comparison = compare_shadow(
            actual_target_id="openai.actual",
            decision=decision,
            target_facts=frozen_facts,
        )

        self.assertEqual(comparison.recommended_target_id, "openai.fast")
        self.assertEqual(comparison.actual_state, "eligible")
        self.assertFalse(comparison.agreement)
        self.assertGreater(comparison.score_advantage, 0)
        self.assertEqual(comparison.estimated_cost_delta_micros, -75)
        self.assertEqual(comparison.cost_currency, "USD")
        self.assertEqual(comparison.estimated_latency_delta_ms, -600)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            comparison.agreement = True

    def test_shadow_comparison_distinguishes_rejected_unknown_and_no_route(self):
        from openusage_bar.routing_engine import decide_route
        from openusage_bar.routing_evaluation import compare_shadow
        from openusage_bar.routing_policy import built_in_policy

        disabled = target("openai.disabled", enabled=False)
        decision = decide_route(
            request(), (disabled,), (facts("openai.disabled"),),
            built_in_policy("reliable"), context(),
        )
        rejected = compare_shadow(
            actual_target_id="openai.disabled",
            decision=decision,
            target_facts=(facts("openai.disabled"),),
        )
        unknown = compare_shadow(
            actual_target_id="openai.unconfigured",
            decision=decision,
            target_facts=(facts("openai.disabled"),),
        )

        self.assertEqual(rejected.actual_state, "rejected")
        self.assertEqual(rejected.actual_rejection_codes, ("target_disabled",))
        self.assertIsNone(rejected.recommended_target_id)
        self.assertFalse(rejected.agreement)
        self.assertEqual(unknown.actual_state, "unknown")
        self.assertEqual(unknown.actual_rejection_codes, ())

    def test_shadow_scores_an_eligible_actual_target_beyond_public_alternatives(self):
        from openusage_bar.routing_evaluation import evaluate_shadow_case
        from openusage_bar.routing_policy import built_in_policy

        route_targets = tuple(target(f"openai.target-{index:02d}") for index in range(18))
        actual_id = route_targets[-1].target_id
        frozen_facts = tuple(
            facts(
                value.target_id,
                headroom_bp=(2_000 if value.target_id == actual_id else 9_000),
                duration_p95_ms=(10_000 if value.target_id == actual_id else 200),
                estimated_cost_micros=(1_000 if value.target_id == actual_id else 10),
            )
            for value in route_targets
        )

        decision, comparison = evaluate_shadow_case(
            actual_target_id=actual_id,
            route_request=request(),
            targets=route_targets,
            target_facts=frozen_facts,
            policy=built_in_policy("reliable"),
            context=context(),
        )

        public_ids = {
            value.target_id
            for value in (() if decision.selected is None else (decision.selected,))
            + decision.alternatives
        }
        self.assertNotIn(actual_id, public_ids)
        self.assertEqual(comparison.actual_state, "eligible")
        self.assertIsNotNone(comparison.actual_score)
        self.assertGreater(comparison.score_advantage, 0)

    def test_replay_is_canonical_and_aggregates_policy_evidence(self):
        from openusage_bar.routing_evaluation import ReplayCase, replay_cases
        from openusage_bar.routing_policy import built_in_policy

        route_targets = (target("openai.best"), target("openai.actual"))
        frozen_facts = (
            facts("openai.best", duration_p95_ms=250, estimated_cost_micros=40),
            facts("openai.actual", duration_p95_ms=750, estimated_cost_micros=90,
                  error_count=15),
        )
        cases = (
            ReplayCase(
                case_id="case-b",
                actual_target_id="openai.actual",
                request=request(),
                targets=route_targets,
                target_facts=frozen_facts,
                context=context(),
            ),
            ReplayCase(
                case_id="case-a",
                actual_target_id="openai.best",
                request=request(),
                targets=route_targets,
                target_facts=frozen_facts,
                context=context(),
            ),
        )

        report = replay_cases(cases, built_in_policy("reliable"))

        self.assertEqual(tuple(value.case_id for value in report.results),
                         ("case-a", "case-b"))
        self.assertEqual(report.summary.case_count, 2)
        self.assertEqual(report.summary.selection_count, 2)
        self.assertEqual(report.summary.agreement_count, 1)
        self.assertEqual(report.summary.agreement_basis_points, 5_000)
        self.assertEqual(report.summary.no_route_count, 0)
        self.assertEqual(report.summary.actual_rejected_count, 0)
        self.assertEqual(report.summary.comparable_score_count, 2)
        self.assertGreater(report.summary.mean_score_advantage, 0)
        self.assertEqual(report.summary.comparable_latency_count, 2)
        self.assertEqual(report.summary.mean_estimated_latency_delta_ms, -250)
        self.assertEqual(len(report.summary.cost_deltas), 1)
        self.assertEqual(report.summary.cost_deltas[0].currency, "USD")
        self.assertEqual(report.summary.cost_deltas[0].case_count, 2)
        self.assertEqual(report.summary.cost_deltas[0].total_delta_micros, -50)

    def test_replay_rejects_duplicate_cases_policy_mismatch_and_unbounded_batches(self):
        from openusage_bar.routing_evaluation import MAX_REPLAY_CASES, ReplayCase, replay_cases
        from openusage_bar.routing_policy import built_in_policy

        case = ReplayCase(
            case_id="case-one",
            actual_target_id="openai.best",
            request=request(),
            targets=(target("openai.best"),),
            target_facts=(facts("openai.best"),),
            context=context(),
        )
        with self.assertRaisesRegex(ValueError, "duplicate replay case"):
            replay_cases((case, case), built_in_policy("reliable"))
        with self.assertRaisesRegex(ValueError, "policy does not match"):
            replay_cases((case,), built_in_policy("fast"))
        with self.assertRaisesRegex(ValueError, "too many replay cases"):
            replay_cases(tuple(case for _ in range(MAX_REPLAY_CASES + 1)),
                         built_in_policy("reliable"))

    def test_replay_rejects_empty_and_duplicate_frozen_target_identifiers(self):
        from openusage_bar.routing_evaluation import MAX_REPLAY_TARGETS, ReplayCase, replay_cases
        from openusage_bar.routing_policy import built_in_policy

        with self.assertRaisesRegex(ValueError, "at least one replay case"):
            replay_cases((), built_in_policy("reliable"))

        with self.assertRaisesRegex(ValueError, "duplicate target"):
            ReplayCase(
                case_id="case-duplicate-targets",
                actual_target_id="openai.best",
                request=request(),
                targets=(target("openai.best"), target("openai.best")),
                target_facts=(facts("openai.best"),),
                context=context(),
            )

        with self.assertRaisesRegex(ValueError, "duplicate target facts"):
            ReplayCase(
                case_id="case-duplicate-facts",
                actual_target_id="openai.best",
                request=request(),
                targets=(target("openai.best"),),
                target_facts=(facts("openai.best"), facts("openai.best")),
                context=context(),
            )

        with self.assertRaisesRegex(ValueError, "too many target facts"):
            ReplayCase(
                case_id="case-too-many-facts",
                actual_target_id="openai.best",
                request=request(),
                targets=(target("openai.best"),),
                target_facts=tuple(
                    facts(f"openai.fact-{index}")
                    for index in range(MAX_REPLAY_TARGETS + 1)
                ),
                context=context(),
            )


if __name__ == "__main__":
    unittest.main()
