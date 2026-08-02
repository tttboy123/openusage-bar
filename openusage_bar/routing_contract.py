"""Immutable, content-free values for local route decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable


MAX_COUNTER = 1_000_000_000_000
MAX_IDS = 50

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ANON_REF = re.compile(r"^anon_[0-9a-f]{16,64}$")
_CURRENCY = re.compile(r"^[A-Z][A-Z0-9_]{2,7}$")

TASK_KINDS = frozenset({"chat", "code", "reasoning", "embedding", "image", "audio", "other"})
PRIVACY_LEVELS = frozenset({"local_only", "direct_provider", "allow_proxy"})
TARGET_PRIVACY_CLASSES = frozenset({"local_only", "direct_provider", "proxy"})
EXECUTION_CLASSES = frozenset({"direct_api", "subscription_cli", "openai_compatible", "self_hosted"})
RESOURCE_MODES = frozenset({"quota", "balance"})
CONNECTION_STATES = frozenset({"available", "unavailable"})
SOURCE_STATES = frozenset({"ok", "error", "authentication_failed", "unavailable", "unknown"})
FACT_STATES = frozenset({"complete", "partial", "missing", "stale"})
RUNTIME_STATES = frozenset({"complete", "partial", "missing"})


def _stable_id(name: str, value: object) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{name} must use the stable identifier grammar")
    return value


def _enum(name: str, value: object, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{name} is invalid")
    return value


def _currency(name: str, value: object) -> str:
    if not isinstance(value, str) or _CURRENCY.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _integer(name: str, value: object, *, maximum: int = MAX_COUNTER) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{name} is invalid")
    return value


def _optional_integer(name: str, value: object, *, maximum: int = MAX_COUNTER) -> int | None:
    if value is None:
        return None
    return _integer(name, value, maximum=maximum)


def _ids(name: str, values: Iterable[str], *, maximum: int = MAX_IDS) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must contain stable identifiers")
    result = tuple(sorted({_stable_id(name, value) for value in values}))
    if len(result) > maximum:
        raise ValueError(f"{name} contains too many identifiers")
    return result


def _utc_timestamp(name: str, value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{name} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{name} must be a UTC timestamp") from error
    if parsed.utcoffset() is None or parsed.astimezone(timezone.utc).utcoffset() != parsed.utcoffset():
        raise ValueError(f"{name} must be a UTC timestamp")
    return parsed


@dataclass(frozen=True)
class RouteTarget:
    target_id: str
    provider_id: str
    account_ref: str
    model_id: str
    connection_ref: str
    execution_class: str
    execution_adapter_id: str
    resource_mode: str
    fact_account_ref: str | None
    runtime_scope_ref: str | None
    balance_currency: str | None
    cost_currency: str | None
    input_cost_micros_per_million: int | None
    output_cost_micros_per_million: int | None
    enabled: bool
    adapter_available: bool
    regions: tuple[str, ...]
    privacy_class: str
    capabilities: tuple[str, ...]
    context_window_tokens: int
    quality_tier: int

    def __post_init__(self) -> None:
        for name in (
            "target_id", "provider_id", "account_ref", "model_id",
            "connection_ref", "execution_adapter_id",
        ):
            _stable_id(name, getattr(self, name))
        _enum("execution_class", self.execution_class, EXECUTION_CLASSES)
        _enum("resource_mode", self.resource_mode, RESOURCE_MODES)
        _enum("privacy_class", self.privacy_class, TARGET_PRIVACY_CLASSES)
        if self.fact_account_ref is not None:
            _stable_id("fact_account_ref", self.fact_account_ref)
        if (
            self.runtime_scope_ref is not None
            and (
                not isinstance(self.runtime_scope_ref, str)
                or _ANON_REF.fullmatch(self.runtime_scope_ref) is None
            )
        ):
            raise ValueError("runtime_scope_ref must be anonymous")
        if self.balance_currency is not None:
            _currency("balance_currency", self.balance_currency)
        if (self.resource_mode == "balance") != (self.balance_currency is not None):
            raise ValueError("balance currency does not match resource mode")
        cost_values = (
            self.cost_currency,
            self.input_cost_micros_per_million,
            self.output_cost_micros_per_million,
        )
        if any(value is None for value in cost_values) != all(
            value is None for value in cost_values
        ):
            raise ValueError("target cost metadata is incomplete")
        if self.cost_currency is not None:
            _currency("cost_currency", self.cost_currency)
            _optional_integer(
                "input_cost_micros_per_million",
                self.input_cost_micros_per_million,
            )
            _optional_integer(
                "output_cost_micros_per_million",
                self.output_cost_micros_per_million,
            )
        if not isinstance(self.enabled, bool) or not isinstance(self.adapter_available, bool):
            raise ValueError("target flags must be booleans")
        object.__setattr__(self, "regions", _ids("regions", self.regions))
        object.__setattr__(self, "capabilities", _ids("capabilities", self.capabilities))
        _integer("context_window_tokens", self.context_window_tokens)
        _integer("quality_tier", self.quality_tier, maximum=5)


@dataclass(frozen=True)
class RouteTask:
    kind: str
    required_capabilities: tuple[str, ...]
    estimated_input_tokens: int
    max_output_tokens: int
    minimum_context_window_tokens: int
    privacy: str
    regions: tuple[str, ...]

    def __post_init__(self) -> None:
        _enum("task kind", self.kind, TASK_KINDS)
        _enum("task privacy", self.privacy, PRIVACY_LEVELS)
        object.__setattr__(
            self,
            "required_capabilities",
            _ids("required_capabilities", self.required_capabilities),
        )
        object.__setattr__(self, "regions", _ids("regions", self.regions))
        _integer("estimated_input_tokens", self.estimated_input_tokens)
        _integer("max_output_tokens", self.max_output_tokens)
        _integer("minimum_context_window_tokens", self.minimum_context_window_tokens)
        if self.estimated_input_tokens + self.max_output_tokens > MAX_COUNTER:
            raise ValueError("task token estimate is invalid")


@dataclass(frozen=True)
class RouteConstraints:
    allow_providers: tuple[str, ...] = ()
    deny_providers: tuple[str, ...] = ()
    allow_targets: tuple[str, ...] = ()
    deny_targets: tuple[str, ...] = ()
    maximum_estimated_cost_micros: int | None = None
    cost_currency: str | None = None

    def __post_init__(self) -> None:
        for name in ("allow_providers", "deny_providers", "allow_targets", "deny_targets"):
            object.__setattr__(self, name, _ids(name, getattr(self, name)))
        _optional_integer(
            "maximum_estimated_cost_micros",
            self.maximum_estimated_cost_micros,
        )
        if (self.maximum_estimated_cost_micros is None) != (self.cost_currency is None):
            raise ValueError("cost limit and currency must be supplied together")
        if self.cost_currency is not None:
            _currency("cost_currency", self.cost_currency)


@dataclass(frozen=True)
class RouteSession:
    session_ref: str
    remaining_budget_micros: int | None
    reserve_micros: int | None
    budget_currency: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.session_ref, str) or _ANON_REF.fullmatch(self.session_ref) is None:
            raise ValueError("session_ref must be anonymous")
        remaining = _optional_integer("remaining_budget_micros", self.remaining_budget_micros)
        reserve = _optional_integer("reserve_micros", self.reserve_micros)
        if (remaining is None) != (reserve is None):
            raise ValueError("session budget and reserve must be supplied together")
        if (remaining is None) != (self.budget_currency is None):
            raise ValueError("session budget and currency must be supplied together")
        if self.budget_currency is not None:
            _currency("budget_currency", self.budget_currency)


@dataclass(frozen=True)
class RouteRequest:
    policy_id: str
    task: RouteTask
    constraints: RouteConstraints = field(default_factory=RouteConstraints)
    session: RouteSession | None = None

    def __post_init__(self) -> None:
        _stable_id("policy_id", self.policy_id)
        if not isinstance(self.task, RouteTask):
            raise ValueError("task is invalid")
        if not isinstance(self.constraints, RouteConstraints):
            raise ValueError("constraints are invalid")
        if self.session is not None and not isinstance(self.session, RouteSession):
            raise ValueError("session is invalid")


@dataclass(frozen=True)
class TargetFacts:
    target_id: str
    connection_state: str
    source_state: str
    resource_state: str
    resource_mode: str
    headroom_bp: int | None
    balance_micros: int | None
    balance_currency: str | None
    runtime_state: str
    observation_count: int
    error_count: int
    duration_p95_ms: int | None
    ttft_p95_ms: int | None
    estimated_cost_micros: int | None
    estimated_cost_currency: str | None

    def __post_init__(self) -> None:
        _stable_id("target_id", self.target_id)
        _enum("connection_state", self.connection_state, CONNECTION_STATES)
        _enum("source_state", self.source_state, SOURCE_STATES)
        _enum("resource_state", self.resource_state, FACT_STATES)
        _enum("resource_mode", self.resource_mode, RESOURCE_MODES)
        _enum("runtime_state", self.runtime_state, RUNTIME_STATES)
        _optional_integer("headroom_bp", self.headroom_bp, maximum=10_000)
        _optional_integer("balance_micros", self.balance_micros)
        if (self.balance_micros is None) != (self.balance_currency is None):
            raise ValueError("balance amount and currency must be supplied together")
        if self.balance_currency is not None:
            _currency("balance_currency", self.balance_currency)
        if self.resource_mode == "quota" and (
            self.balance_micros is not None or self.balance_currency is not None
        ):
            raise ValueError("quota facts cannot carry a balance")
        if self.resource_mode == "balance" and self.headroom_bp is not None:
            raise ValueError("balance facts cannot carry quota headroom")
        observations = _integer("observation_count", self.observation_count)
        errors = _integer("error_count", self.error_count)
        if errors > observations:
            raise ValueError("error_count exceeds observation_count")
        _optional_integer("duration_p95_ms", self.duration_p95_ms)
        _optional_integer("ttft_p95_ms", self.ttft_p95_ms)
        _optional_integer("estimated_cost_micros", self.estimated_cost_micros)
        if (self.estimated_cost_micros is None) != (
            self.estimated_cost_currency is None
        ):
            raise ValueError("estimated cost and currency must be supplied together")
        if self.estimated_cost_currency is not None:
            _currency("estimated_cost_currency", self.estimated_cost_currency)


@dataclass(frozen=True)
class DecisionContext:
    generated_at: str
    expires_at: str
    data_revision: int
    runtime_revision: int | None

    def __post_init__(self) -> None:
        generated = _utc_timestamp("generated_at", self.generated_at)
        expires = _utc_timestamp("expires_at", self.expires_at)
        if expires <= generated:
            raise ValueError("expires_at must be after generated_at")
        _integer("data_revision", self.data_revision)
        _optional_integer("runtime_revision", self.runtime_revision)


@dataclass(frozen=True)
class ScoreComponents:
    reliability: int
    headroom: int
    latency: int
    cost: int


@dataclass(frozen=True)
class ScoredTarget:
    target_id: str
    provider_id: str
    account_ref: str
    model_id: str
    score: int
    components: ScoreComponents
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RejectedTarget:
    target_id: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class RouteDecision:
    generated_at: str
    expires_at: str
    policy_id: str
    policy_revision: int
    data_revision: int
    runtime_revision: int | None
    selected: ScoredTarget | None
    alternatives: tuple[ScoredTarget, ...]
    rejected: tuple[RejectedTarget, ...]
