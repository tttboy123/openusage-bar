"""Normalize resource and Runtime snapshots into conservative route facts."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Iterable

from .query import BalanceItem, CapacityProvider, ResourceSnapshotResult, SourceStatusItem
from .routing_contract import (
    MAX_COUNTER,
    RouteRequest,
    RouteTarget,
    TargetFacts,
)
from .runtime_store import RuntimeSummary, RuntimeSummaryGroup


_AUTH_ERRORS = frozenset({
    "auth_required",
    "authentication_failed",
    "forbidden",
    "invalid_credentials",
    "unauthorized",
})
_SOURCE_PRECEDENCE = {
    "ok": 0,
    "unknown": 1,
    "error": 2,
    "unavailable": 3,
    "authentication_failed": 4,
}


def _quota_applies(value: CapacityProvider, target: RouteTarget) -> bool:
    if (
        value.provider_id != target.provider_id
        or value.account_ref != target.fact_account_ref
    ):
        return False
    if value.applies_to.kind in {"subscription", "account"}:
        return True
    return (
        value.applies_to.kind == "model"
        and target.model_id in value.applies_to.model_ids
    )


def _ratio_basis_points(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= 1:
        return None
    return min(10_000, max(0, int(value * 10_000)))


def _resource_state(
    quotas: tuple[CapacityProvider, ...],
) -> tuple[str, int | None]:
    if not quotas:
        return "missing", None
    known = tuple(
        value
        for value in (_ratio_basis_points(row.remaining_ratio) for row in quotas)
        if value is not None
    )
    stale = any(row.stale or row.state == "stale" for row in quotas)
    unusable = sum(
        1
        for row in quotas
        if row.state != "ok" or _ratio_basis_points(row.remaining_ratio) is None
    )
    headroom = min(known) if known else None
    if stale:
        return "stale", headroom
    if unusable:
        return ("partial" if known else "missing"), headroom
    return "complete", headroom


def _source_status(value: SourceStatusItem | None) -> str:
    if value is None:
        return "unknown"
    if value.state == "ok":
        return "ok"
    if value.state == "authentication_failed" or value.error_code in _AUTH_ERRORS:
        return "authentication_failed"
    if value.state in {"stale", "temporarily_unavailable", "unavailable"}:
        return "unavailable"
    if value.state in {"error", "failed"}:
        return "error"
    return "unknown"


def _combined_source_state(
    provider_id: str,
    source_ids: tuple[str, ...],
    statuses: tuple[SourceStatusItem, ...],
) -> str:
    if not source_ids:
        return "unknown"
    index: dict[tuple[str, str], SourceStatusItem | None] = {}
    duplicates: set[tuple[str, str]] = set()
    for status in statuses:
        key = (status.provider_id, status.source_id)
        if key in index:
            duplicates.add(key)
        else:
            index[key] = status
    values = []
    for source_id in sorted(set(source_ids)):
        key = (provider_id, source_id)
        values.append(
            "unknown" if key in duplicates else _source_status(index.get(key))
        )
    return max(values, key=lambda value: _SOURCE_PRECEDENCE[value])


def _declared_cost_micros(
    target: RouteTarget,
    route_request: RouteRequest,
) -> tuple[int | None, str | None]:
    if target.cost_currency is None:
        return None, None
    assert target.input_cost_micros_per_million is not None
    assert target.output_cost_micros_per_million is not None
    value = (
        Decimal(target.input_cost_micros_per_million)
        * route_request.task.estimated_input_tokens
        + Decimal(target.output_cost_micros_per_million)
        * route_request.task.max_output_tokens
    ) / 1_000_000
    value = value.to_integral_value(rounding=ROUND_CEILING)
    if value > MAX_COUNTER:
        return None, None
    return int(value), target.cost_currency


def _runtime_cost_micros(
    group: RuntimeSummaryGroup, total_tokens: int
) -> tuple[int | None, str | None]:
    if (
        group.cost_coverage_state != "complete"
        or group.total_tokens <= 0
        or len(group.costs) != 1
    ):
        return None, None
    value = (
        Decimal(group.costs[0].cost_micros)
        * total_tokens
        / group.total_tokens
    ).to_integral_value(rounding=ROUND_CEILING)
    if value > MAX_COUNTER:
        return None, None
    return int(value), group.costs[0].currency.upper()


def _balance_applies(value: BalanceItem, target: RouteTarget) -> bool:
    return (
        value.provider_id == target.provider_id
        and value.account_ref == target.fact_account_ref
        and value.currency == target.balance_currency
    )


def _balance_micros(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        decimal = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if not decimal.is_finite() or decimal < 0:
        return None
    micros = (decimal * 1_000_000).to_integral_value(rounding=ROUND_FLOOR)
    if micros > MAX_COUNTER:
        return None
    return int(micros)


def _balance_state(
    balances: tuple[BalanceItem, ...],
) -> tuple[str, int | None]:
    if not balances:
        return "missing", None
    known = tuple(
        value
        for value in (_balance_micros(row.available) for row in balances)
        if value is not None
    )
    balance = min(known) if known else None
    if any(row.stale or row.state == "stale" for row in balances):
        return "stale", balance
    unusable = any(
        row.state != "ok" or _balance_micros(row.available) is None
        for row in balances
    )
    if len(balances) != 1 or unusable:
        return ("partial" if known else "missing"), balance
    return "complete", balance


def _runtime_values(
    target: RouteTarget,
    summary: RuntimeSummary | None,
) -> tuple[str, int, int, int | None, int | None, RuntimeSummaryGroup | None]:
    if summary is None or target.runtime_scope_ref is None:
        return "missing", 0, 0, None, None, None
    matches = tuple(
        group
        for group in summary.groups
        if group.provider_id == target.provider_id
        and group.model_id == target.model_id
        and group.scope_ref == target.runtime_scope_ref
    )
    if len(matches) != 1:
        state = (
            "partial"
            if summary.coverage_state == "partial" or summary.omitted_group_count
            else "missing"
        )
        return state, 0, 0, None, None, None
    group = matches[0]
    counts = dict(group.status_counts)
    if (
        len(counts) != len(group.status_counts)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts.values()
        )
        or sum(counts.values()) != group.observation_count
    ):
        return "partial", 0, 0, None, None, None
    completed = counts.get("completed", 0)
    return (
        "complete",
        group.observation_count,
        group.observation_count - completed,
        group.latency.duration_p95_ms,
        group.latency.ttft_p95_ms,
        group,
    )


def build_target_facts(
    *,
    targets: tuple[RouteTarget, ...],
    route_request: RouteRequest,
    resource_snapshot: ResourceSnapshotResult,
    runtime_summary: RuntimeSummary | None,
    available_connections: Iterable[str],
) -> tuple[TargetFacts, ...]:
    """Return canonical target facts without joining unrelated account scopes."""
    if (
        not isinstance(route_request, RouteRequest)
        or not isinstance(resource_snapshot, ResourceSnapshotResult)
        or runtime_summary is not None
        and not isinstance(runtime_summary, RuntimeSummary)
    ):
        raise ValueError("routing facts input is invalid")
    values = tuple(targets)
    if any(not isinstance(value, RouteTarget) for value in values):
        raise ValueError("routing facts input is invalid")
    ids = [value.target_id for value in values]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate route target")
    if isinstance(available_connections, (str, bytes)):
        raise ValueError("routing connections are invalid")
    connections = frozenset(available_connections)
    if any(not isinstance(value, str) for value in connections):
        raise ValueError("routing connections are invalid")

    total_tokens = (
        route_request.task.estimated_input_tokens
        + route_request.task.max_output_tokens
    )
    result: list[TargetFacts] = []
    for target in sorted(values, key=lambda value: value.target_id):
        quotas = tuple(
            row
            for row in resource_snapshot.quota_windows
            if _quota_applies(row, target)
        )
        balances = tuple(
            row
            for row in resource_snapshot.balances
            if _balance_applies(row, target)
        )
        estimated_cost, estimated_cost_currency = _declared_cost_micros(
            target,
            route_request,
        )
        if target.resource_mode == "quota":
            resource_state, headroom = _resource_state(quotas)
            balance_micros = None
            balance_currency = None
            source_state = _combined_source_state(
                target.provider_id,
                tuple(row.source_id for row in quotas),
                resource_snapshot.sources,
            )
        else:
            resource_state, balance_micros = _balance_state(balances)
            balance_currency = (
                target.balance_currency if balance_micros is not None else None
            )
            headroom = None
            source_state = _combined_source_state(
                target.provider_id,
                tuple(row.source_id for row in balances),
                resource_snapshot.sources,
            )
        (
            runtime_state,
            observation_count,
            error_count,
            duration_p95_ms,
            ttft_p95_ms,
            runtime_group,
        ) = _runtime_values(target, runtime_summary)
        if estimated_cost is None and runtime_group is not None:
            estimated_cost, estimated_cost_currency = _runtime_cost_micros(
                runtime_group,
                total_tokens,
            )
        if (
            target.resource_mode == "balance"
            and estimated_cost_currency != target.balance_currency
        ):
            estimated_cost = None
            estimated_cost_currency = None
        result.append(TargetFacts(
            target_id=target.target_id,
            connection_state=(
                "available"
                if target.connection_ref in connections
                else "unavailable"
            ),
            source_state=source_state,
            resource_state=resource_state,
            resource_mode=target.resource_mode,
            headroom_bp=headroom,
            balance_micros=balance_micros,
            balance_currency=balance_currency,
            runtime_state=runtime_state,
            observation_count=observation_count,
            error_count=error_count,
            duration_p95_ms=duration_p95_ms,
            ttft_p95_ms=ttft_p95_ms,
            estimated_cost_micros=estimated_cost,
            estimated_cost_currency=estimated_cost_currency,
        ))
    return tuple(result)
