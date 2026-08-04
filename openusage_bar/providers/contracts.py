from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from ..activity_records import (
    BalanceObservation,
    DailyCostRow,
    DailyUsageRow,
    ProviderInstance,
    QuotaObservation,
    validate_id,
    validate_safe_display_name,
)
from ..provider_catalog import PROVIDER_CATEGORIES, SOURCE_KINDS


_PROVIDER_INSTANCE_SOURCE_KINDS = SOURCE_KINDS | frozenset({"generic_https"})
DISCOVERY_STATES = frozenset({
    "available",
    "near_limit",
    "limited",
    "auth_required",
    "unsupported",
    "error",
    "unknown",
})


@dataclass(frozen=True)
class SourceAttribution:
    credential_source: str
    source_kind: str

    def __post_init__(self) -> None:
        validate_id("credential_source", self.credential_source)
        if self.source_kind not in _PROVIDER_INSTANCE_SOURCE_KINDS:
            raise ValueError("source_kind must be a canonical source kind")


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    family_id: str
    display_name: str
    category: str

    def __post_init__(self) -> None:
        validate_id("provider_id", self.provider_id)
        validate_id("family_id", self.family_id)
        validate_safe_display_name(self.display_name)
        if self.category not in PROVIDER_CATEGORIES:
            raise ValueError("category must be a canonical provider category")

    def observed(
        self, observed_at: datetime, attribution: SourceAttribution
    ) -> ProviderInstance:
        return ProviderInstance(
            provider_id=self.provider_id,
            family_id=self.family_id,
            display_name=self.display_name,
            category=self.category,
            credential_source=attribution.credential_source,
            source_kind=attribution.source_kind,
            observed_at=observed_at.isoformat(),
        )


class QuotaAdapter(Protocol):
    source_id: str
    source_priority: int

    def fetch_quota(self) -> "QuotaCollectionResult": ...


class UsageAdapter(Protocol):
    usage_source_id: str
    account_ref: str

    def fetch_usage(self, since: date, until: date) -> "UsageImportResult": ...


class CostAdapter(Protocol):
    cost_source_id: str
    account_ref: str

    def fetch_costs(self, since: date, until: date) -> "CostImportResult": ...


class BalanceAdapter(Protocol):
    source_id: str
    source_priority: int

    def fetch_balance(self) -> "BalanceCollectionResult": ...


class DiscoveryAdapter(Protocol):
    source_id: str
    source_priority: int

    def fetch_discovery(self) -> "DiscoveryFetchResult": ...


_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class ImportFailure:
    error_code: str

    def __post_init__(self) -> None:
        if _ERROR_CODE.fullmatch(self.error_code) is None:
            raise ValueError("Import failure requires a sanitized error code")

    @property
    def ok(self) -> bool:
        return False


@dataclass(frozen=True)
class UsageImportSuccess:
    since: date
    until: date
    rows: tuple[DailyUsageRow, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True)
class CostImportSuccess:
    since: date
    until: date
    rows: tuple[DailyCostRow, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True)
class QuotaFetchSuccess:
    observations: tuple[QuotaObservation, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "observations", tuple(self.observations))
        if not self.observations:
            raise ValueError("Quota success requires at least one observation")

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True)
class QuotaFetchFailure:
    error_code: str

    def __post_init__(self) -> None:
        if _ERROR_CODE.fullmatch(self.error_code) is None:
            raise ValueError("Quota failure requires a sanitized error code")

    @property
    def ok(self) -> bool:
        return False


@dataclass(frozen=True)
class BalanceFetchSuccess:
    observations: tuple[BalanceObservation, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "observations", tuple(self.observations))
        if not self.observations:
            raise ValueError("Balance success requires at least one observation")

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True)
class BalanceFetchFailure:
    error_code: str

    def __post_init__(self) -> None:
        if _ERROR_CODE.fullmatch(self.error_code) is None:
            raise ValueError("Balance failure requires a sanitized error code")

    @property
    def ok(self) -> bool:
        return False


@dataclass(frozen=True)
class ProviderDiscoveryObservation:
    descriptor: ProviderDescriptor
    attribution: SourceAttribution
    source_id: str
    state: str
    observed_at: datetime

    def __post_init__(self) -> None:
        validate_id("source_id", self.source_id)
        if self.state not in DISCOVERY_STATES:
            raise ValueError("discovery state must be canonical")
        if (
            not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
        ):
            raise ValueError("discovery time must include a timezone")


@dataclass(frozen=True)
class DiscoveryFetchSuccess:
    observations: tuple[ProviderDiscoveryObservation, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "observations", tuple(self.observations))
        provider_ids = tuple(
            observation.descriptor.provider_id for observation in self.observations
        )
        if provider_ids != tuple(sorted(set(provider_ids))):
            raise ValueError("discovery observations require unique sorted providers")

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True)
class DiscoveryFetchFailure:
    error_code: str

    def __post_init__(self) -> None:
        if _ERROR_CODE.fullmatch(self.error_code) is None:
            raise ValueError("Discovery failure requires a sanitized error code")

    @property
    def ok(self) -> bool:
        return False


UsageImportResult = UsageImportSuccess | ImportFailure
CostImportResult = CostImportSuccess | ImportFailure
QuotaFetchResult = QuotaFetchSuccess | QuotaFetchFailure
BalanceFetchResult = BalanceFetchSuccess | BalanceFetchFailure
DiscoveryFetchResult = DiscoveryFetchSuccess | DiscoveryFetchFailure


@dataclass(frozen=True)
class QuotaCollectionResult:
    result: QuotaFetchResult
    attribution: SourceAttribution

    def __post_init__(self) -> None:
        if not isinstance(self.result, (QuotaFetchSuccess, QuotaFetchFailure)):
            raise TypeError("quota collection requires a typed result")


@dataclass(frozen=True)
class BalanceCollectionResult:
    result: BalanceFetchResult
    attribution: SourceAttribution

    def __post_init__(self) -> None:
        if not isinstance(self.result, (BalanceFetchSuccess, BalanceFetchFailure)):
            raise TypeError("balance collection requires a typed result")


@dataclass(frozen=True)
class ProviderBinding:
    provider_id: str
    family_id: str
    descriptor: ProviderDescriptor
    discovery_sources: tuple[DiscoveryAdapter, ...] = ()
    balance_sources: tuple[BalanceAdapter, ...] = ()
    quota_sources: tuple[QuotaAdapter, ...] = ()
    usage_sources: tuple[UsageAdapter, ...] = ()
    cost_sources: tuple[CostAdapter, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.descriptor.provider_id != self.provider_id
            or self.descriptor.family_id != self.family_id
        ):
            raise ValueError("descriptor must match binding identity")
        object.__setattr__(self, "balance_sources", tuple(self.balance_sources))
        object.__setattr__(self, "discovery_sources", tuple(self.discovery_sources))
        object.__setattr__(self, "quota_sources", tuple(self.quota_sources))
        object.__setattr__(self, "usage_sources", tuple(self.usage_sources))
        object.__setattr__(self, "cost_sources", tuple(self.cost_sources))
