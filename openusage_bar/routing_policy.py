"""Validated deterministic policies for the local routing engine."""

from __future__ import annotations

from dataclasses import dataclass

from .routing_contract import (
    EXECUTION_CLASSES,
    MAX_COUNTER,
    TARGET_PRIVACY_CLASSES,
    _currency,
    _stable_id,
)


@dataclass(frozen=True)
class RoutePolicy:
    policy_id: str
    revision: int
    reliability_weight: int
    headroom_weight: int
    latency_weight: int
    cost_weight: int
    min_headroom_bp: int
    max_error_rate_bp: int
    min_runtime_samples: int
    latency_reference_ms: int
    cost_reference_micros: int
    cost_currency: str
    min_balance_micros: int
    balance_reference_micros: int
    unknown_penalty: int
    require_cost: bool = False
    require_runtime: bool = False
    allowed_execution_classes: frozenset[str] = EXECUTION_CLASSES
    allowed_privacy_classes: frozenset[str] = TARGET_PRIVACY_CLASSES

    def __post_init__(self) -> None:
        _stable_id("policy_id", self.policy_id)
        weights = (
            self.reliability_weight,
            self.headroom_weight,
            self.latency_weight,
            self.cost_weight,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in weights):
            raise ValueError("routing policy weights are invalid")
        if sum(weights) != 100:
            raise ValueError("routing policy weights must sum to 100")
        for name, value, maximum in (
            ("revision", self.revision, MAX_COUNTER),
            ("min_headroom_bp", self.min_headroom_bp, 10_000),
            ("max_error_rate_bp", self.max_error_rate_bp, 10_000),
            ("min_runtime_samples", self.min_runtime_samples, MAX_COUNTER),
            ("latency_reference_ms", self.latency_reference_ms, MAX_COUNTER),
            ("cost_reference_micros", self.cost_reference_micros, MAX_COUNTER),
            ("min_balance_micros", self.min_balance_micros, MAX_COUNTER),
            ("balance_reference_micros", self.balance_reference_micros, MAX_COUNTER),
            ("unknown_penalty", self.unknown_penalty, 10_000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                raise ValueError(f"{name} is invalid")
        if (
            self.revision == 0
            or self.latency_reference_ms == 0
            or self.cost_reference_micros == 0
            or self.balance_reference_micros == 0
        ):
            raise ValueError("routing policy references must be positive")
        _currency("cost_currency", self.cost_currency)
        if not isinstance(self.require_cost, bool) or not isinstance(self.require_runtime, bool):
            raise ValueError("routing policy flags must be booleans")
        executions = frozenset(self.allowed_execution_classes)
        privacy = frozenset(self.allowed_privacy_classes)
        if not executions or not executions <= EXECUTION_CLASSES:
            raise ValueError("allowed execution classes are invalid")
        if not privacy or not privacy <= TARGET_PRIVACY_CLASSES:
            raise ValueError("allowed privacy classes are invalid")
        object.__setattr__(self, "allowed_execution_classes", executions)
        object.__setattr__(self, "allowed_privacy_classes", privacy)


_COMMON = {
    "revision": 1,
    "min_headroom_bp": 500,
    "max_error_rate_bp": 2_000,
    "min_runtime_samples": 5,
    "latency_reference_ms": 10_000,
    "cost_reference_micros": 1_000,
    "cost_currency": "USD",
    "min_balance_micros": 1_000_000,
    "balance_reference_micros": 20_000_000,
    "unknown_penalty": 4_000,
}

_BUILT_INS = {
    "reliable": RoutePolicy(
        policy_id="reliable",
        reliability_weight=50,
        headroom_weight=25,
        latency_weight=15,
        cost_weight=10,
        **_COMMON,
    ),
    "balanced": RoutePolicy(
        policy_id="balanced",
        reliability_weight=35,
        headroom_weight=25,
        latency_weight=20,
        cost_weight=20,
        **_COMMON,
    ),
    "economy": RoutePolicy(
        policy_id="economy",
        reliability_weight=30,
        headroom_weight=20,
        latency_weight=10,
        cost_weight=40,
        require_cost=True,
        **_COMMON,
    ),
    "fast": RoutePolicy(
        policy_id="fast",
        reliability_weight=35,
        headroom_weight=15,
        latency_weight=40,
        cost_weight=10,
        require_runtime=True,
        **_COMMON,
    ),
    "private": RoutePolicy(
        policy_id="private",
        reliability_weight=50,
        headroom_weight=25,
        latency_weight=15,
        cost_weight=10,
        allowed_privacy_classes=frozenset({"local_only", "direct_provider"}),
        **_COMMON,
    ),
}


def built_in_policy(policy_id: str) -> RoutePolicy:
    try:
        return _BUILT_INS[policy_id]
    except KeyError as error:
        raise ValueError("routing policy was not found") from error


def built_in_policies() -> tuple[RoutePolicy, ...]:
    return tuple(_BUILT_INS[name] for name in sorted(_BUILT_INS))


def built_in_policy_ids() -> frozenset[str]:
    return frozenset(_BUILT_INS)
