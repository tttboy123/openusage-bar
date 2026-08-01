"""Provider runtime contracts and registration."""

from .contracts import (
    BalanceCollectionResult,
    CostAdapter,
    CostImportSuccess,
    DiscoveryAdapter,
    DiscoveryFetchFailure,
    DiscoveryFetchSuccess,
    ImportFailure,
    ProviderBinding,
    ProviderDescriptor,
    ProviderDiscoveryObservation,
    QuotaCollectionResult,
    QuotaAdapter,
    QuotaFetchFailure,
    QuotaFetchSuccess,
    UsageAdapter,
    UsageImportSuccess,
    SourceAttribution,
)
from .registry import AdapterRegistry, UnknownProviderConfig

__all__ = [
    "AdapterRegistry",
    "BalanceCollectionResult",
    "CostAdapter",
    "CostImportSuccess",
    "DiscoveryAdapter",
    "DiscoveryFetchFailure",
    "DiscoveryFetchSuccess",
    "ImportFailure",
    "ProviderBinding",
    "ProviderDescriptor",
    "ProviderDiscoveryObservation",
    "QuotaCollectionResult",
    "QuotaAdapter",
    "QuotaFetchFailure",
    "QuotaFetchSuccess",
    "UnknownProviderConfig",
    "UsageAdapter",
    "UsageImportSuccess",
    "SourceAttribution",
]
