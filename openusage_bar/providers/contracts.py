from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from ..models import Overview, ProviderCard


class QuotaAdapter(Protocol):
    """Compatibility contract for current card-producing quota sources.

    Task 2 replaces the presentation result with a fact-specific quota result;
    keeping the existing fetch shape here makes the registry refactor behavior
    preserving and lets that migration happen independently.
    """

    def fetch(self) -> Overview | ProviderCard: ...


class UsageAdapter(Protocol):
    usage_source_id: str

    def fetch_usage(self, since: date, until: date) -> Any: ...


class CostAdapter(Protocol):
    cost_source_id: str

    def fetch_costs(self, since: date, until: date) -> Any: ...


@dataclass(frozen=True)
class ProviderBinding:
    provider_id: str
    family_id: str
    quota_sources: tuple[QuotaAdapter, ...] = ()
    usage_sources: tuple[UsageAdapter, ...] = ()
    cost_sources: tuple[CostAdapter, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "quota_sources", tuple(self.quota_sources))
        object.__setattr__(self, "usage_sources", tuple(self.usage_sources))
        object.__setattr__(self, "cost_sources", tuple(self.cost_sources))
