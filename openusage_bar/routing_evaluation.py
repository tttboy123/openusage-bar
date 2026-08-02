from __future__ import annotations

from dataclasses import dataclass
import re

from .routing_contract import (
    DecisionContext,
    RouteDecision,
    RouteRequest,
    RouteTarget,
    TargetFacts,
)
from .routing_engine import decide_route
from .routing_policy import RoutePolicy


MAX_REPLAY_CASES = 256
MAX_REPLAY_TARGETS = 128
_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TARGET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ACTUAL_STATES = frozenset({"eligible", "rejected", "unknown"})


def _identifier(name: str, value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _signed_mean(total: int, count: int) -> int | None:
    if count == 0:
        return None
    magnitude = abs(total) // count
    return -magnitude if total < 0 else magnitude


@dataclass(frozen=True)
class ShadowComparison:
    actual_target_id: str
    recommended_target_id: str | None
    actual_state: str
    actual_rejection_codes: tuple[str, ...]
    agreement: bool
    recommended_score: int | None
    actual_score: int | None
    score_advantage: int | None
    estimated_cost_delta_micros: int | None
    cost_currency: str | None
    estimated_latency_delta_ms: int | None

    def __post_init__(self) -> None:
        _identifier("actual_target_id", self.actual_target_id, _TARGET_ID)
        if self.recommended_target_id is not None:
            _identifier("recommended_target_id", self.recommended_target_id, _TARGET_ID)
        if self.actual_state not in _ACTUAL_STATES:
            raise ValueError("actual_state is invalid")
        if not isinstance(self.actual_rejection_codes, tuple) or any(
            not isinstance(value, str) for value in self.actual_rejection_codes
        ):
            raise ValueError("actual rejection codes are invalid")
        if not isinstance(self.agreement, bool):
            raise ValueError("agreement is invalid")
        for name in (
            "recommended_score", "actual_score", "score_advantage",
            "estimated_cost_delta_micros", "estimated_latency_delta_ms",
        ):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"{name} is invalid")
        if (self.estimated_cost_delta_micros is None) != (self.cost_currency is None):
            raise ValueError("cost delta and currency must be supplied together")
        if self.cost_currency is not None and (
            not isinstance(self.cost_currency, str)
            or re.fullmatch(r"^[A-Z]{3}$", self.cost_currency) is None
        ):
            raise ValueError("cost_currency is invalid")


@dataclass(frozen=True)
class ReplayCase:
    case_id: str
    actual_target_id: str
    request: RouteRequest
    targets: tuple[RouteTarget, ...]
    target_facts: tuple[TargetFacts, ...]
    context: DecisionContext

    def __post_init__(self) -> None:
        _identifier("case_id", self.case_id, _CASE_ID)
        _identifier("actual_target_id", self.actual_target_id, _TARGET_ID)
        if not isinstance(self.request, RouteRequest):
            raise ValueError("replay request is invalid")
        if not isinstance(self.context, DecisionContext):
            raise ValueError("replay context is invalid")
        if not isinstance(self.targets, tuple) or any(
            not isinstance(value, RouteTarget) for value in self.targets
        ):
            raise ValueError("replay targets are invalid")
        if not isinstance(self.target_facts, tuple) or any(
            not isinstance(value, TargetFacts) for value in self.target_facts
        ):
            raise ValueError("replay facts are invalid")
        if len(self.targets) > MAX_REPLAY_TARGETS:
            raise ValueError("replay case contains too many targets")
        if len(self.target_facts) > MAX_REPLAY_TARGETS:
            raise ValueError("replay case contains too many target facts")
        target_ids = tuple(value.target_id for value in self.targets)
        if len(set(target_ids)) != len(target_ids):
            raise ValueError("replay targets contain duplicate target identifiers")
        fact_ids = tuple(value.target_id for value in self.target_facts)
        if len(set(fact_ids)) != len(fact_ids):
            raise ValueError("replay target facts contain duplicate target facts identifiers")


@dataclass(frozen=True)
class ReplayCaseResult:
    case_id: str
    decision: RouteDecision
    comparison: ShadowComparison


@dataclass(frozen=True)
class ReplayCostDelta:
    currency: str
    case_count: int
    total_delta_micros: int
    mean_delta_micros: int


@dataclass(frozen=True)
class ReplaySummary:
    case_count: int
    selection_count: int
    agreement_count: int
    agreement_basis_points: int
    no_route_count: int
    no_route_basis_points: int
    actual_rejected_count: int
    actual_rejected_basis_points: int
    comparable_score_count: int
    mean_score_advantage: int | None
    comparable_latency_count: int
    mean_estimated_latency_delta_ms: int | None
    cost_deltas: tuple[ReplayCostDelta, ...]


@dataclass(frozen=True)
class ReplayReport:
    policy_id: str
    policy_revision: int
    summary: ReplaySummary
    results: tuple[ReplayCaseResult, ...]


def compare_shadow(
    *,
    actual_target_id: str,
    decision: RouteDecision,
    target_facts: tuple[TargetFacts, ...],
    actual_decision: RouteDecision | None = None,
) -> ShadowComparison:
    _identifier("actual_target_id", actual_target_id, _TARGET_ID)
    if not isinstance(decision, RouteDecision):
        raise ValueError("route decision is invalid")
    if not isinstance(target_facts, tuple) or any(
        not isinstance(value, TargetFacts) for value in target_facts
    ):
        raise ValueError("target facts are invalid")
    if actual_decision is not None:
        if not isinstance(actual_decision, RouteDecision):
            raise ValueError("actual target decision is invalid")
        if (
            actual_decision.policy_id != decision.policy_id
            or actual_decision.policy_revision != decision.policy_revision
            or actual_decision.data_revision != decision.data_revision
            or actual_decision.runtime_revision != decision.runtime_revision
        ):
            raise ValueError("actual target decision does not match route decision")
    facts_by_id = {value.target_id: value for value in target_facts}
    if len(facts_by_id) != len(target_facts):
        raise ValueError("target facts contain duplicate identifiers")
    scored = (() if decision.selected is None else (decision.selected,)) + decision.alternatives
    scored_by_id = {value.target_id: value for value in scored}
    rejected_by_id = {value.target_id: value for value in decision.rejected}
    actual_scored = scored_by_id.get(actual_target_id)
    actual_rejected = rejected_by_id.get(actual_target_id)
    if actual_scored is None and actual_rejected is None and actual_decision is not None:
        if (
            actual_decision.selected is not None
            and actual_decision.selected.target_id == actual_target_id
        ):
            actual_scored = actual_decision.selected
        else:
            actual_rejected = next(
                (
                    value
                    for value in actual_decision.rejected
                    if value.target_id == actual_target_id
                ),
                None,
            )
    if actual_scored is not None:
        actual_state = "eligible"
        rejection_codes: tuple[str, ...] = ()
    elif actual_rejected is not None:
        actual_state = "rejected"
        rejection_codes = actual_rejected.reason_codes
    else:
        actual_state = "unknown"
        rejection_codes = ()
    selected = decision.selected
    selected_id = None if selected is None else selected.target_id
    selected_score = None if selected is None else selected.score
    actual_score = None if actual_scored is None else actual_scored.score
    score_advantage = (
        None
        if selected_score is None or actual_score is None
        else selected_score - actual_score
    )
    cost_delta: int | None = None
    cost_currency: str | None = None
    latency_delta: int | None = None
    if selected is not None:
        selected_facts = facts_by_id.get(selected.target_id)
        actual_facts = facts_by_id.get(actual_target_id)
        if selected_facts is not None and actual_facts is not None:
            if (
                selected_facts.estimated_cost_micros is not None
                and actual_facts.estimated_cost_micros is not None
                and selected_facts.estimated_cost_currency
                == actual_facts.estimated_cost_currency
            ):
                cost_delta = (
                    selected_facts.estimated_cost_micros
                    - actual_facts.estimated_cost_micros
                )
                cost_currency = selected_facts.estimated_cost_currency
            if (
                selected_facts.duration_p95_ms is not None
                and actual_facts.duration_p95_ms is not None
            ):
                latency_delta = (
                    selected_facts.duration_p95_ms - actual_facts.duration_p95_ms
                )
    return ShadowComparison(
        actual_target_id=actual_target_id,
        recommended_target_id=selected_id,
        actual_state=actual_state,
        actual_rejection_codes=rejection_codes,
        agreement=selected_id == actual_target_id,
        recommended_score=selected_score,
        actual_score=actual_score,
        score_advantage=score_advantage,
        estimated_cost_delta_micros=cost_delta,
        cost_currency=cost_currency,
        estimated_latency_delta_ms=latency_delta,
    )


def evaluate_shadow_case(
    *,
    actual_target_id: str,
    route_request: RouteRequest,
    targets: tuple[RouteTarget, ...],
    target_facts: tuple[TargetFacts, ...],
    policy: RoutePolicy,
    context: DecisionContext,
) -> tuple[RouteDecision, ShadowComparison]:
    """Evaluate one recommendation and the actual target without truncation loss."""
    decision = decide_route(route_request, targets, target_facts, policy, context)
    actual_target = next(
        (value for value in targets if value.target_id == actual_target_id), None
    )
    actual_decision = None
    if actual_target is not None:
        actual_facts = tuple(
            value for value in target_facts if value.target_id == actual_target_id
        )
        actual_decision = decide_route(
            route_request, (actual_target,), actual_facts, policy, context
        )
    return decision, compare_shadow(
        actual_target_id=actual_target_id,
        decision=decision,
        target_facts=target_facts,
        actual_decision=actual_decision,
    )


def replay_cases(cases: tuple[ReplayCase, ...], policy: RoutePolicy) -> ReplayReport:
    if not isinstance(cases, tuple) or any(
        not isinstance(value, ReplayCase) for value in cases
    ):
        raise ValueError("replay cases are invalid")
    if not cases:
        raise ValueError("at least one replay case is required")
    if len(cases) > MAX_REPLAY_CASES:
        raise ValueError("too many replay cases")
    case_ids = tuple(value.case_id for value in cases)
    if len(set(case_ids)) != len(case_ids):
        raise ValueError("duplicate replay case identifier")
    if not isinstance(policy, RoutePolicy):
        raise ValueError("replay policy is invalid")
    if any(value.request.policy_id != policy.policy_id for value in cases):
        raise ValueError("replay policy does not match request")

    results: list[ReplayCaseResult] = []
    for case in sorted(cases, key=lambda value: value.case_id):
        decision, comparison = evaluate_shadow_case(
            actual_target_id=case.actual_target_id,
            route_request=case.request,
            targets=case.targets,
            target_facts=case.target_facts,
            policy=policy,
            context=case.context,
        )
        results.append(ReplayCaseResult(case.case_id, decision, comparison))

    count = len(results)
    selections = sum(value.decision.selected is not None for value in results)
    agreements = sum(value.comparison.agreement for value in results)
    no_routes = count - selections
    rejected = sum(value.comparison.actual_state == "rejected" for value in results)
    score_values = tuple(
        value.comparison.score_advantage
        for value in results
        if value.comparison.score_advantage is not None
    )
    latency_values = tuple(
        value.comparison.estimated_latency_delta_ms
        for value in results
        if value.comparison.estimated_latency_delta_ms is not None
    )
    costs: dict[str, list[int]] = {}
    for value in results:
        comparison = value.comparison
        if (
            comparison.estimated_cost_delta_micros is not None
            and comparison.cost_currency is not None
        ):
            costs.setdefault(comparison.cost_currency, []).append(
                comparison.estimated_cost_delta_micros
            )
    cost_deltas = tuple(
        ReplayCostDelta(
            currency=currency,
            case_count=len(values),
            total_delta_micros=sum(values),
            mean_delta_micros=_signed_mean(sum(values), len(values)) or 0,
        )
        for currency, values in sorted(costs.items())
    )
    basis = lambda value: 0 if count == 0 else value * 10_000 // count
    summary = ReplaySummary(
        case_count=count,
        selection_count=selections,
        agreement_count=agreements,
        agreement_basis_points=basis(agreements),
        no_route_count=no_routes,
        no_route_basis_points=basis(no_routes),
        actual_rejected_count=rejected,
        actual_rejected_basis_points=basis(rejected),
        comparable_score_count=len(score_values),
        mean_score_advantage=_signed_mean(sum(score_values), len(score_values)),
        comparable_latency_count=len(latency_values),
        mean_estimated_latency_delta_ms=_signed_mean(
            sum(latency_values), len(latency_values)
        ),
        cost_deltas=cost_deltas,
    )
    return ReplayReport(policy.policy_id, policy.revision, summary, tuple(results))
