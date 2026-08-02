"""Pure deterministic target filtering and reliability-first scoring."""

from __future__ import annotations

from .routing_contract import (
    DecisionContext,
    MAX_ALTERNATIVES,
    REJECTION_REASON_ORDER,
    RejectedTarget,
    RouteDecision,
    RouteRequest,
    RouteTarget,
    ScoreComponents,
    ScoredTarget,
    TargetFacts,
)
from .routing_policy import RoutePolicy


MAX_TARGETS = 128


def _unique_by_id(values, *, name: str):
    result = {}
    for value in values:
        identifier = value.target_id
        if identifier in result:
            raise ValueError(f"duplicate {name}")
        result[identifier] = value
    return result


def _missing_facts(target_id: str) -> TargetFacts:
    return TargetFacts(
        target_id=target_id,
        connection_state="unavailable",
        source_state="unknown",
        resource_state="missing",
        resource_mode="quota",
        headroom_bp=None,
        balance_micros=None,
        balance_currency=None,
        runtime_state="missing",
        observation_count=0,
        error_count=0,
        duration_p95_ms=None,
        ttft_p95_ms=None,
        estimated_cost_micros=None,
        estimated_cost_currency=None,
    )


def _not_allowed(route_request: RouteRequest, target: RouteTarget) -> bool:
    constraints = route_request.constraints
    return (
        bool(constraints.allow_providers)
        and target.provider_id not in constraints.allow_providers
        or target.provider_id in constraints.deny_providers
        or bool(constraints.allow_targets)
        and target.target_id not in constraints.allow_targets
        or target.target_id in constraints.deny_targets
    )


def _privacy_compatible(requested: str, actual: str) -> bool:
    if requested == "local_only":
        return actual == "local_only"
    if requested == "direct_provider":
        return actual in {"local_only", "direct_provider"}
    return True


def _region_compatible(requested: tuple[str, ...], actual: tuple[str, ...]) -> bool:
    if not requested:
        return True
    return "global" in actual or bool(set(requested) & set(actual))


def _error_rate_bp(value: TargetFacts) -> int | None:
    if value.observation_count == 0:
        return None
    return value.error_count * 10_000 // value.observation_count


def _cost_matches(value: TargetFacts, currency: str) -> bool:
    return (
        value.estimated_cost_micros is not None
        and value.estimated_cost_currency == currency
    )


def _rejection_codes(
    route_request: RouteRequest,
    target: RouteTarget,
    value: TargetFacts,
    policy: RoutePolicy,
) -> tuple[str, ...]:
    codes: set[str] = set()
    if not target.enabled:
        codes.add("target_disabled")
    if not target.adapter_available or target.execution_class not in policy.allowed_execution_classes:
        codes.add("adapter_unavailable")
    if _not_allowed(route_request, target):
        codes.add("not_allowed")
    if not set(route_request.task.required_capabilities) <= set(target.capabilities):
        codes.add("capability_missing")
    if target.context_window_tokens < route_request.task.minimum_context_window_tokens:
        codes.add("context_too_small")
    if (
        target.privacy_class not in policy.allowed_privacy_classes
        or not _privacy_compatible(route_request.task.privacy, target.privacy_class)
    ):
        codes.add("privacy_incompatible")
    if not _region_compatible(route_request.task.regions, target.regions):
        codes.add("region_incompatible")
    if value.connection_state != "available":
        codes.add("connection_unavailable")
    if value.source_state != "ok":
        codes.add("source_unhealthy")

    resource_value_missing = (
        value.resource_mode == "quota" and value.headroom_bp is None
        or value.resource_mode == "balance" and value.balance_micros is None
    )
    if value.resource_state == "missing" or (
        value.resource_state == "complete" and resource_value_missing
    ):
        codes.add("fact_missing")
    elif value.resource_state == "stale":
        codes.add("fact_stale")
    elif value.resource_state == "partial":
        codes.add("coverage_partial")
    if (
        value.resource_mode == "quota"
        and value.headroom_bp is not None
        and value.headroom_bp < policy.min_headroom_bp
    ):
        codes.add("quota_reserve_exceeded")

    error_rate = _error_rate_bp(value)
    if (
        value.runtime_state == "complete"
        and value.observation_count >= policy.min_runtime_samples
        and error_rate is not None
        and error_rate > policy.max_error_rate_bp
    ):
        codes.add("error_rate_exceeded")
    if policy.require_runtime:
        if value.runtime_state == "missing":
            codes.add("fact_missing")
        elif value.runtime_state == "partial":
            codes.add("coverage_partial")
        elif value.observation_count < policy.min_runtime_samples:
            codes.add("fact_missing")

    maximum_cost = route_request.constraints.maximum_estimated_cost_micros
    required_currencies: set[str] = set()
    if policy.require_cost:
        required_currencies.add(policy.cost_currency)
    if maximum_cost is not None:
        assert route_request.constraints.cost_currency is not None
        required_currencies.add(route_request.constraints.cost_currency)
    if (
        route_request.session is not None
        and route_request.session.remaining_budget_micros is not None
    ):
        assert route_request.session.budget_currency is not None
        required_currencies.add(route_request.session.budget_currency)
    if value.resource_mode == "balance" and value.balance_currency is not None:
        required_currencies.add(value.balance_currency)
    cost_required = value.resource_mode == "balance" or bool(required_currencies)
    cost_usable = (
        value.estimated_cost_micros is not None
        and value.estimated_cost_currency is not None
        and all(
            _cost_matches(value, currency)
            for currency in required_currencies
        )
    )
    if not cost_usable:
        if cost_required:
            codes.add("cost_unknown")
    else:
        assert value.estimated_cost_micros is not None
        if maximum_cost is not None and value.estimated_cost_micros > maximum_cost:
            codes.add("cost_limit_exceeded")
        if (
            value.resource_mode == "balance"
            and value.balance_micros is not None
            and value.balance_micros - value.estimated_cost_micros
            < policy.min_balance_micros
        ):
            codes.add("balance_reserve_exceeded")
        if (
            route_request.session is not None
            and route_request.session.remaining_budget_micros is not None
        ):
            remaining = route_request.session.remaining_budget_micros
            reserve = route_request.session.reserve_micros
            assert remaining is not None and reserve is not None
            if value.estimated_cost_micros > remaining or remaining - value.estimated_cost_micros < reserve:
                codes.add("session_budget_exceeded")

    return tuple(code for code in REJECTION_REASON_ORDER if code in codes)


def _components(value: TargetFacts, policy: RoutePolicy) -> ScoreComponents:
    error_rate = _error_rate_bp(value)
    if (
        value.runtime_state == "complete"
        and value.observation_count >= policy.min_runtime_samples
        and error_rate is not None
    ):
        reliability = max(0, 10_000 - error_rate)
    else:
        reliability = policy.unknown_penalty
    if value.resource_mode == "quota":
        headroom = (
            value.headroom_bp
            if value.headroom_bp is not None
            else policy.unknown_penalty
        )
    elif (
        value.balance_micros is not None
        and _cost_matches(value, value.balance_currency or "")
    ):
        assert value.estimated_cost_micros is not None
        remaining = max(0, value.balance_micros - value.estimated_cost_micros)
        headroom = min(
            10_000,
            remaining * 10_000 // policy.balance_reference_micros,
        )
    else:
        headroom = policy.unknown_penalty
    if value.duration_p95_ms is None:
        latency = policy.unknown_penalty
    else:
        latency = max(0, 10_000 - value.duration_p95_ms * 10_000 // policy.latency_reference_ms)
    if not _cost_matches(value, policy.cost_currency):
        cost = policy.unknown_penalty
    else:
        cost = max(0, 10_000 - value.estimated_cost_micros * 10_000 // policy.cost_reference_micros)
    return ScoreComponents(reliability, headroom, latency, cost)


def _score(components: ScoreComponents, policy: RoutePolicy) -> int:
    return (
        components.reliability * policy.reliability_weight
        + components.headroom * policy.headroom_weight
        + components.latency * policy.latency_weight
        + components.cost * policy.cost_weight
    ) // 100


def _score_reasons(value: TargetFacts, policy: RoutePolicy) -> tuple[str, ...]:
    result = ["healthy_source"]
    if value.resource_mode == "quota" and value.headroom_bp is not None:
        result.append("quota_headroom")
    elif value.resource_mode == "balance" and value.balance_micros is not None:
        result.append("balance_headroom")
    error_rate = _error_rate_bp(value)
    if value.observation_count >= policy.min_runtime_samples and error_rate is not None:
        result.append("low_recent_error_rate")
    if value.duration_p95_ms is not None:
        result.append("latency_observed")
    if _cost_matches(value, policy.cost_currency):
        result.append("cost_known")
    return tuple(result)


def decide_route(
    route_request: RouteRequest,
    targets: tuple[RouteTarget, ...],
    target_facts: tuple[TargetFacts, ...],
    policy: RoutePolicy,
    context: DecisionContext,
) -> RouteDecision:
    if not isinstance(route_request, RouteRequest) or not isinstance(policy, RoutePolicy):
        raise ValueError("routing input is invalid")
    if route_request.policy_id != policy.policy_id:
        raise ValueError("routing policy does not match request")
    if len(targets) > MAX_TARGETS:
        raise ValueError("too many route targets")
    target_map = _unique_by_id(targets, name="route target")
    facts_map = _unique_by_id(target_facts, name="target facts")

    eligible: list[ScoredTarget] = []
    rejected: list[RejectedTarget] = []
    for target_id in sorted(target_map):
        target = target_map[target_id]
        value = facts_map.get(target_id)
        if value is None:
            value = _missing_facts(target_id)
        codes = _rejection_codes(route_request, target, value, policy)
        if codes:
            rejected.append(RejectedTarget(target_id, codes))
            continue
        components = _components(value, policy)
        eligible.append(ScoredTarget(
            target_id=target.target_id,
            provider_id=target.provider_id,
            account_ref=target.account_ref,
            model_id=target.model_id,
            score=_score(components, policy),
            components=components,
            reasons=_score_reasons(value, policy),
        ))

    eligible.sort(key=lambda item: (-item.score, item.target_id))
    selected = eligible[0] if eligible else None
    alternatives = tuple(eligible[1:1 + MAX_ALTERNATIVES])
    return RouteDecision(
        generated_at=context.generated_at,
        expires_at=context.expires_at,
        policy_id=policy.policy_id,
        policy_revision=policy.revision,
        data_revision=context.data_revision,
        runtime_revision=context.runtime_revision,
        selected=selected,
        alternatives=alternatives,
        rejected=tuple(rejected),
    )
