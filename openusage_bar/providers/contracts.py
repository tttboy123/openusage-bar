from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from ..activity_store import DailyCostRow, DailyUsageRow, QuotaObservation
from ..models import Overview, ProviderCard


class QuotaAdapter(Protocol):
    source_id: str

    def fetch_quota(self) -> "QuotaFetchResult": ...


class LegacyCardAdapter(Protocol):
    """0.4 compatibility contract for current card-producing sources.

    Task 2 replaces the presentation result with a fact-specific quota result;
    keeping the existing fetch shape here makes the registry refactor behavior
    preserving and lets that migration happen independently.
    """

    def fetch(self) -> Overview | ProviderCard: ...


class UsageAdapter(Protocol):
    usage_source_id: str
    account_ref: str

    def fetch_usage(self, since: date, until: date) -> "UsageImportResult": ...


class CostAdapter(Protocol):
    cost_source_id: str
    account_ref: str

    def fetch_costs(self, since: date, until: date) -> "CostImportResult": ...


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


UsageImportResult = UsageImportSuccess | ImportFailure
CostImportResult = CostImportSuccess | ImportFailure
QuotaFetchResult = QuotaFetchSuccess | QuotaFetchFailure


@dataclass(frozen=True)
class ProviderBinding:
    provider_id: str
    family_id: str
    quota_sources: tuple[QuotaAdapter | LegacyCardAdapter, ...] = ()
    usage_sources: tuple[UsageAdapter, ...] = ()
    cost_sources: tuple[CostAdapter, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "quota_sources", tuple(self.quota_sources))
        object.__setattr__(self, "usage_sources", tuple(self.usage_sources))
        object.__setattr__(self, "cost_sources", tuple(self.cost_sources))
