"""Closed, local-only account pool selection with sanitized projections."""

from __future__ import annotations

import math
import threading
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

from ..config import ID_PATTERN
from .accounts import AccountState, ProviderAccountRef


_MAX_POOL_MEMBERS = 64
_EXCLUSION_REASONS = frozenset(
    {
        "cooldown",
        "credential_backend_unavailable",
        "cross_model_unconfirmed",
        "cross_provider_unconfirmed",
        "cross_region_unconfirmed",
        "disabled",
        "health_unknown",
        "metric_unknown",
        "quota_unknown",
        "unhealthy",
    }
)


class PoolStrategy(StrEnum):
    FIXED_FIRST = "fixed-first"
    ROUND_ROBIN = "round-robin"
    STICKY = "sticky"
    QUOTA_AWARE = "quota-aware"
    COST = "cost"
    LATENCY = "latency"
    RELIABILITY = "reliability"


def _stable_id(value: object) -> bool:
    return type(value) is str and ID_PATTERN.fullmatch(value) is not None


def _public_account_display_id(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 17
        and value.startswith("acct_")
        and all(character in "0123456789abcdef" for character in value[5:])
    )


def _closed_number(
    value: object,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("pool metric must be a number or null")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise ValueError("pool metric is out of range")
    if maximum is not None and number > maximum:
        raise ValueError("pool metric is out of range")
    return number


@dataclass(frozen=True)
class PoolMember:
    account_id: str = field(repr=False)
    priority: int = 100
    weight: int = 1

    def __post_init__(self) -> None:
        if not _stable_id(self.account_id):
            raise ValueError("pool account_id must use the stable identifier grammar")
        if type(self.priority) is not int or not 0 <= self.priority <= 10_000:
            raise ValueError("pool member priority is invalid")
        if type(self.weight) is not int or not 1 <= self.weight <= 100:
            raise ValueError("pool member weight is invalid")


@dataclass(frozen=True)
class AccountPool:
    pool_id: str
    revision: int
    strategy: PoolStrategy
    members: tuple[PoolMember, ...]
    cross_provider_fallback: bool = False
    cross_model_fallback: bool = False
    cross_region_fallback: bool = False

    def __post_init__(self) -> None:
        if not _stable_id(self.pool_id):
            raise ValueError("pool_id must use the stable identifier grammar")
        if type(self.revision) is not int or not 1 <= self.revision <= 2**63 - 1:
            raise ValueError("pool revision is invalid")
        if not isinstance(self.strategy, PoolStrategy):
            raise TypeError("pool strategy is invalid")
        if (
            type(self.members) is not tuple
            or not 1 <= len(self.members) <= _MAX_POOL_MEMBERS
            or any(type(member) is not PoolMember for member in self.members)
        ):
            raise ValueError("pool members are invalid")
        member_ids = tuple(member.account_id for member in self.members)
        if len(set(member_ids)) != len(member_ids):
            raise ValueError("pool member IDs must be unique")
        for value in (
            self.cross_provider_fallback,
            self.cross_model_fallback,
            self.cross_region_fallback,
        ):
            if type(value) is not bool:
                raise TypeError("pool fallback confirmations must be booleans")


@dataclass(frozen=True)
class AccountCandidate:
    account: ProviderAccountRef
    health: float | None = None
    quota_ratio: float | None = None
    cost: float | None = None
    latency_ms: float | None = None
    reliability: float | None = None
    disabled: bool = False
    cooldown: bool = False
    credential_backend_available: bool | None = None
    model_id: str | None = None
    region: str | None = None

    def __post_init__(self) -> None:
        if type(self.account) is not ProviderAccountRef:
            raise TypeError("candidate account is invalid")
        object.__setattr__(
            self, "health", _closed_number(self.health, maximum=1.0)
        )
        object.__setattr__(
            self, "quota_ratio", _closed_number(self.quota_ratio, maximum=1.0)
        )
        object.__setattr__(self, "cost", _closed_number(self.cost))
        object.__setattr__(
            self, "latency_ms", _closed_number(self.latency_ms)
        )
        object.__setattr__(
            self,
            "reliability",
            _closed_number(self.reliability, maximum=1.0),
        )
        if type(self.disabled) is not bool or type(self.cooldown) is not bool:
            raise TypeError("candidate state is invalid")
        if self.credential_backend_available is not None and type(
            self.credential_backend_available
        ) is not bool:
            raise TypeError("credential backend state is invalid")
        for name, value in (("model_id", self.model_id), ("region", self.region)):
            if value is not None and not _stable_id(value):
                raise ValueError(f"candidate {name} is invalid")


@dataclass(frozen=True)
class PoolExclusion:
    account_display_id: str
    reason: str

    def __post_init__(self) -> None:
        if not _stable_id(self.account_display_id):
            raise ValueError("account display ID is invalid")
        if type(self.reason) is not str or self.reason not in _EXCLUSION_REASONS:
            raise ValueError("pool exclusion reason is invalid")

    def to_public_dict(self) -> dict[str, str]:
        return {"accountDisplayId": self.account_display_id, "reason": self.reason}


@dataclass(frozen=True)
class PoolSelection:
    pool_id: str
    pool_revision: int
    strategy: PoolStrategy
    selected_account: ProviderAccountRef | None = field(repr=False)
    exclusions: tuple[PoolExclusion, ...]

    @property
    def selected_account_id(self) -> str | None:
        selected = self.selected_account
        return selected.account_id if selected is not None else None

    @property
    def status(self) -> str:
        return "selected" if self.selected_account is not None else "unavailable"

    def to_public_dict(self) -> dict[str, object]:
        selected = self.selected_account
        return {
            "poolId": self.pool_id,
            "poolRevision": self.pool_revision,
            "strategy": self.strategy.value,
            "status": self.status,
            "selectedAccountAlias": selected.alias if selected is not None else None,
            "selectedAccountDisplayId": (
                selected.display_id if selected is not None else None
            ),
            "selectedAccountState": (
                AccountState.HEALTHY.value if selected is not None else AccountState.UNKNOWN.value
            ),
            "exclusions": [item.to_public_dict() for item in self.exclusions],
        }


class PoolSelector:
    """Select from closed local facts; never reads credentials or Providers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._round_robin_offsets: dict[tuple[str, int], int] = {}

    def select(
        self,
        pool: AccountPool,
        *,
        candidates: tuple[AccountCandidate, ...],
        requested_provider_id: str,
        requested_model_id: str | None = None,
        requested_region: str | None = None,
        sticky_account_id: str | None = None,
    ) -> PoolSelection:
        if type(pool) is not AccountPool:
            raise TypeError("account pool is invalid")
        if type(candidates) is not tuple or any(
            type(candidate) is not AccountCandidate for candidate in candidates
        ):
            raise TypeError("account candidates are invalid")
        if not _stable_id(requested_provider_id):
            raise ValueError("requested provider is invalid")
        for value in (requested_model_id, requested_region, sticky_account_id):
            if value is not None and not _stable_id(value):
                raise ValueError("requested pool scope is invalid")
        candidate_map: dict[str, AccountCandidate] = {}
        for item in candidates:
            account_id = item.account.account_id
            if account_id in candidate_map:
                raise ValueError("candidate account IDs must be unique")
            candidate_map[account_id] = item

        eligible: list[tuple[PoolMember, AccountCandidate]] = []
        exclusions: list[PoolExclusion] = []
        for member in sorted(pool.members, key=lambda item: (item.priority, item.account_id)):
            candidate = candidate_map.get(member.account_id)
            if candidate is None:
                continue
            reason = self._exclusion_reason(
                pool,
                candidate,
                requested_provider_id=requested_provider_id,
                requested_model_id=requested_model_id,
                requested_region=requested_region,
            )
            if reason is None:
                reason = self._strategy_exclusion(pool.strategy, candidate)
            if reason is not None:
                exclusions.append(PoolExclusion(candidate.account.display_id, reason))
            else:
                eligible.append((member, candidate))

        selected = self._choose(
            pool,
            eligible,
            sticky_account_id=sticky_account_id,
        )
        return PoolSelection(
            pool_id=pool.pool_id,
            pool_revision=pool.revision,
            strategy=pool.strategy,
            selected_account=selected.account if selected is not None else None,
            exclusions=tuple(exclusions),
        )

    @staticmethod
    def _exclusion_reason(
        pool: AccountPool,
        candidate: AccountCandidate,
        *,
        requested_provider_id: str,
        requested_model_id: str | None,
        requested_region: str | None,
    ) -> str | None:
        if (
            candidate.account.provider_id != requested_provider_id
            and not pool.cross_provider_fallback
        ):
            return "cross_provider_unconfirmed"
        if (
            requested_model_id is not None
            and candidate.model_id is not None
            and candidate.model_id != requested_model_id
            and not pool.cross_model_fallback
        ):
            return "cross_model_unconfirmed"
        if (
            requested_region is not None
            and candidate.region is not None
            and candidate.region != requested_region
            and not pool.cross_region_fallback
        ):
            return "cross_region_unconfirmed"
        if candidate.disabled:
            return "disabled"
        if candidate.cooldown:
            return "cooldown"
        if candidate.credential_backend_available is False:
            return "credential_backend_unavailable"
        if candidate.health is None:
            return "health_unknown"
        if candidate.health <= 0:
            return "unhealthy"
        return None

    @staticmethod
    def _strategy_exclusion(
        strategy: PoolStrategy,
        candidate: AccountCandidate,
    ) -> str | None:
        if strategy is PoolStrategy.QUOTA_AWARE and candidate.quota_ratio is None:
            return "quota_unknown"
        metric = {
            PoolStrategy.COST: candidate.cost,
            PoolStrategy.LATENCY: candidate.latency_ms,
            PoolStrategy.RELIABILITY: candidate.reliability,
        }.get(strategy, 0.0)
        return "metric_unknown" if metric is None else None

    def _choose(
        self,
        pool: AccountPool,
        eligible: list[tuple[PoolMember, AccountCandidate]],
        *,
        sticky_account_id: str | None,
    ) -> AccountCandidate | None:
        if not eligible:
            return None
        if pool.strategy is PoolStrategy.STICKY and sticky_account_id is not None:
            for _member, candidate in eligible:
                if candidate.account.account_id == sticky_account_id:
                    return candidate
        if pool.strategy in (PoolStrategy.FIXED_FIRST, PoolStrategy.STICKY):
            return eligible[0][1]
        if pool.strategy is PoolStrategy.ROUND_ROBIN:
            weighted = [
                candidate
                for member, candidate in eligible
                for _index in range(member.weight)
            ]
            key = (pool.pool_id, pool.revision)
            with self._lock:
                offset = self._round_robin_offsets.get(key, 0)
                selected = weighted[offset % len(weighted)]
                self._round_robin_offsets[key] = (offset + 1) % len(weighted)
            return selected
        if pool.strategy is PoolStrategy.QUOTA_AWARE:
            return max(eligible, key=lambda item: (item[1].quota_ratio, -item[0].priority))[1]
        if pool.strategy is PoolStrategy.COST:
            return min(eligible, key=lambda item: (item[1].cost, item[0].priority))[1]
        if pool.strategy is PoolStrategy.LATENCY:
            return min(eligible, key=lambda item: (item[1].latency_ms, item[0].priority))[1]
        if pool.strategy is PoolStrategy.RELIABILITY:
            return max(eligible, key=lambda item: (item[1].reliability, -item[0].priority))[1]
        raise AssertionError("unreachable pool strategy")


def account_pools_public_payload(
    *,
    accounts: tuple[ProviderAccountRef, ...],
    pools: tuple[AccountPool, ...],
    candidates: tuple[AccountCandidate, ...] = (),
) -> dict[str, object]:
    """Project only renderer-safe account aliases and closed local facts."""

    if type(accounts) is not tuple or any(
        type(account) is not ProviderAccountRef for account in accounts
    ):
        raise TypeError("accounts are invalid")
    if type(pools) is not tuple or any(type(pool) is not AccountPool for pool in pools):
        raise TypeError("account pools are invalid")
    if type(candidates) is not tuple or any(
        type(candidate) is not AccountCandidate for candidate in candidates
    ):
        raise TypeError("account candidates are invalid")
    account_ids = tuple(account.account_id for account in accounts)
    candidate_ids = tuple(candidate.account.account_id for candidate in candidates)
    if len(set(account_ids)) != len(account_ids) or len(set(candidate_ids)) != len(
        candidate_ids
    ):
        raise ValueError("account IDs must be unique")
    known_accounts = set(account_ids)
    if any(
        member.account_id not in known_accounts
        for pool in pools
        for member in pool.members
    ):
        raise ValueError("pool references an unknown account")

    candidate_map = {
        candidate.account.account_id: candidate for candidate in candidates
    }
    public_accounts: list[dict[str, object]] = []
    for account in accounts:
        memberships = [
            {
                "poolId": pool.pool_id,
                "priority": member.priority,
                "weight": member.weight,
            }
            for pool in pools
            for member in pool.members
            if member.account_id == account.account_id
        ]
        memberships.sort(key=lambda item: (item["priority"], item["poolId"]))
        candidate = candidate_map.get(account.account_id)
        status = _public_candidate_status(candidate)
        quota_state = "unknown"
        cooldown_state = "unknown"
        if candidate is not None:
            if candidate.quota_ratio is not None:
                quota_state = (
                    "exhausted" if candidate.quota_ratio <= 0 else "available"
                )
            cooldown_state = "active" if candidate.cooldown else "inactive"
        public_accounts.append(
            {
                "alias": account.alias,
                "displayId": account.display_id,
                "status": status,
                "quota": {
                    "state": quota_state,
                    "remaining": None,
                    "limit": None,
                    "resetAt": None,
                },
                "cooldown": {"state": cooldown_state, "until": None},
                "pools": memberships,
                "priority": memberships[0]["priority"] if memberships else 0,
                "weight": memberships[0]["weight"] if memberships else 0,
            }
        )
    return {"accounts": public_accounts}


def _public_candidate_status(candidate: AccountCandidate | None) -> str:
    if candidate is None:
        return "unknown"
    if candidate.disabled:
        return "disabled"
    if candidate.cooldown:
        return "cooldown"
    if candidate.credential_backend_available is False:
        return "backend_unavailable"
    if candidate.quota_ratio is not None and candidate.quota_ratio <= 0:
        return "quota_exhausted"
    if candidate.health is not None and candidate.health > 0:
        return "ready"
    return "unknown"


def validate_account_pools_public_payload(
    value: object,
) -> dict[str, object] | None:
    """Rebuild the exact renderer contract, rejecting every additive field."""

    if type(value) is not dict or set(value) != {"accounts"}:
        return None
    raw_accounts = value.get("accounts")
    if type(raw_accounts) is not list or len(raw_accounts) > 256:
        return None
    result: list[dict[str, object]] = []
    display_ids: set[str] = set()
    for raw in raw_accounts:
        account = _validated_public_account(raw)
        if account is None:
            return None
        display_id = account["displayId"]
        assert isinstance(display_id, str)
        if display_id in display_ids:
            return None
        display_ids.add(display_id)
        result.append(account)
    return {"accounts": result}


def _validated_public_account(value: object) -> dict[str, object] | None:
    fields = {
        "alias",
        "displayId",
        "status",
        "quota",
        "cooldown",
        "pools",
        "priority",
        "weight",
    }
    if type(value) is not dict or set(value) != fields:
        return None
    alias = value.get("alias")
    if alias is not None and not _public_text(alias):
        return None
    display_id = value.get("displayId")
    if not _public_account_display_id(display_id):
        return None
    status = value.get("status")
    if status not in {
        "ready",
        "cooldown",
        "quota_exhausted",
        "disabled",
        "backend_unavailable",
        "unknown",
    }:
        return None
    quota = _validated_public_quota(value.get("quota"))
    cooldown = _validated_public_cooldown(value.get("cooldown"))
    pools = _validated_public_memberships(value.get("pools"))
    priority = _public_integer(value.get("priority"))
    weight = _public_integer(value.get("weight"))
    if quota is None or cooldown is None or pools is None:
        return None
    if priority is None or weight is None:
        return None
    return {
        "alias": alias,
        "displayId": display_id,
        "status": status,
        "quota": quota,
        "cooldown": cooldown,
        "pools": pools,
        "priority": priority,
        "weight": weight,
    }


def _validated_public_quota(value: object) -> dict[str, object] | None:
    if type(value) is not dict or set(value) != {
        "state",
        "remaining",
        "limit",
        "resetAt",
    }:
        return None
    state = value.get("state")
    if state not in {"available", "exhausted", "unknown"}:
        return None
    remaining = _public_optional_number(value.get("remaining"))
    limit = _public_optional_number(value.get("limit"))
    reset_at = value.get("resetAt")
    if remaining is False or limit is False or not _public_optional_timestamp(reset_at):
        return None
    return {
        "state": state,
        "remaining": None if remaining is None else remaining,
        "limit": None if limit is None else limit,
        "resetAt": reset_at,
    }


def _validated_public_cooldown(value: object) -> dict[str, object] | None:
    if type(value) is not dict or set(value) != {"state", "until"}:
        return None
    state = value.get("state")
    until = value.get("until")
    if state not in {"active", "inactive", "unknown"}:
        return None
    if not _public_optional_timestamp(until):
        return None
    return {"state": state, "until": until}


def _validated_public_memberships(value: object) -> list[dict[str, object]] | None:
    if type(value) is not list or len(value) > 64:
        return None
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in value:
        if type(item) is not dict or set(item) != {"poolId", "priority", "weight"}:
            return None
        pool_id = item.get("poolId")
        priority = _public_integer(item.get("priority"))
        weight = _public_integer(item.get("weight"))
        if (
            not _public_text(pool_id)
            or not _stable_id(pool_id)
            or pool_id in seen
            or priority is None
            or weight is None
        ):
            return None
        seen.add(pool_id)
        result.append({"poolId": pool_id, "priority": priority, "weight": weight})
    return result


def _public_text(value: object) -> bool:
    return (
        type(value) is str
        and value == value.strip()
        and 1 <= len(value) <= 128
        and all(
            not unicodedata.category(character).startswith("C")
            for character in value
        )
    )


def _public_integer(value: object) -> int | None:
    if type(value) is not int or not 0 <= value <= 1_000_000:
        return None
    return value


def _public_optional_number(value: object) -> float | None | bool:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else False


def _public_optional_timestamp(value: object) -> bool:
    return value is None or (
        _public_text(value)
        and isinstance(value, str)
        and len(value) <= 64
    )


__all__ = [
    "AccountCandidate",
    "AccountPool",
    "PoolExclusion",
    "PoolMember",
    "PoolSelection",
    "PoolSelector",
    "PoolStrategy",
    "account_pools_public_payload",
    "validate_account_pools_public_payload",
]
