from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from .config import ID_PATTERN
from .models import ProviderStatus
from .provider_catalog import ProviderFamily, catalog


def _require_instance(value: object, expected: type, field: str) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{field} must be {expected.__name__}")


def _require_int(value: object, field: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field} must be int")


class MetricFamily(StrEnum):
    SUBSCRIPTION_QUOTA = "subscription_quota"
    TOKEN_ACTIVITY = "token_activity"
    BILLING = "billing"
    OPERATIONAL = "operational"


class CapabilityState(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class QuotaWindow(StrEnum):
    SESSION = "session"
    FIVE_HOUR = "five_hour"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    BILLING_CYCLE = "billing_cycle"
    MODEL_SPECIFIC = "model_specific"


class OperatingSystem(StrEnum):
    MACOS = "macos"
    WINDOWS = "windows"
    LINUX = "linux"


class SourceStability(StrEnum):
    STABLE = "stable"
    EXPERIMENTAL = "experimental"
    PINNED = "pinned"
    OPAQUE = "opaque"


class SourceProvenance(StrEnum):
    OPENUSAGE_UPSTREAM = "openusage_upstream"
    OPENUSAGE_BAR_BUILTIN = "openusage_bar_builtin"
    PROVIDER_OFFICIAL = "provider_official"
    PROVIDER_LOCAL = "provider_local"
    USER_SESSION = "user_session"


class SourceKind(StrEnum):
    OPENUSAGE = "openusage"
    BUILTIN_API = "builtin_api"
    OFFICIAL_API = "official_api"
    CLI = "cli"
    LOCAL_LOG = "local_log"
    LOCAL_DATABASE = "local_database"
    KEYCHAIN = "keychain"
    BROWSER_SESSION = "browser_session"


class CredentialType(StrEnum):
    PROVIDER_OWNED = "provider_owned"
    API_KEY = "api_key"
    OAUTH = "oauth"
    CLI = "cli"
    LOCAL = "local"
    KEYCHAIN = "keychain"
    BROWSER_SESSION = "browser_session"


class ObservationState(StrEnum):
    OK = "ok"
    UNSUPPORTED = "unsupported"
    NOT_CONFIGURED = "not_configured"
    AUTH_EXPIRED = "auth_expired"
    PERMISSION_BLOCKED = "permission_blocked"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    STALE = "stale"


@dataclass(frozen=True)
class QuotaWindowCapability:
    state: CapabilityState
    values: tuple[QuotaWindow, ...]

    def __post_init__(self) -> None:
        _require_instance(self.state, CapabilityState, "Quota window state")
        _require_instance(self.values, tuple, "Quota window values")
        for value in self.values:
            _require_instance(value, QuotaWindow, "Quota window value")
        if self.values != tuple(
            sorted(self.values, key=lambda value: value.value)
        ):
            raise ValueError("Quota windows must be sorted")
        if len(set(self.values)) != len(self.values):
            raise ValueError("Quota windows must be unique")
        if self.state is CapabilityState.SUPPORTED and not self.values:
            raise ValueError("Supported quota windows must not be empty")
        if self.state is not CapabilityState.SUPPORTED and self.values:
            raise ValueError("Unknown or unsupported quota windows must be empty")


@dataclass(frozen=True)
class ProviderCapabilities:
    quota_windows: QuotaWindowCapability
    token_history: CapabilityState
    model_breakdown: CapabilityState
    reset_timestamps: CapabilityState
    billing: CapabilityState
    credits: CapabilityState
    balance: CapabilityState
    cost: CapabilityState
    rate_limits: CapabilityState
    service_status: CapabilityState

    def __post_init__(self) -> None:
        _require_instance(
            self.quota_windows,
            QuotaWindowCapability,
            "Provider quota windows",
        )
        states = (
            ("token_history", self.token_history),
            ("model_breakdown", self.model_breakdown),
            ("reset_timestamps", self.reset_timestamps),
            ("billing", self.billing),
            ("credits", self.credits),
            ("balance", self.balance),
            ("cost", self.cost),
            ("rate_limits", self.rate_limits),
            ("service_status", self.service_status),
        )
        for field, state in states:
            _require_instance(state, CapabilityState, f"Provider {field}")


@dataclass(frozen=True)
class SourceCapability:
    source_id: str
    kind: SourceKind
    timeout_seconds: int
    freshness_seconds: int
    credential_type: CredentialType
    operating_systems: frozenset[OperatingSystem]
    stability: SourceStability
    provenance: SourceProvenance
    credential_scope: str | None = None

    def __post_init__(self) -> None:
        _require_instance(self.source_id, str, "Source ID")
        _require_instance(self.kind, SourceKind, "Source kind")
        _require_int(self.timeout_seconds, "Source timeout")
        _require_int(self.freshness_seconds, "Source freshness")
        _require_instance(
            self.credential_type,
            CredentialType,
            "Source credential type",
        )
        _require_instance(
            self.operating_systems,
            frozenset,
            "Source operating systems",
        )
        for operating_system in self.operating_systems:
            _require_instance(
                operating_system,
                OperatingSystem,
                "Source operating system",
            )
        _require_instance(self.stability, SourceStability, "Source stability")
        _require_instance(self.provenance, SourceProvenance, "Source provenance")
        if self.credential_scope is not None:
            _require_instance(self.credential_scope, str, "Credential scope")
        if not ID_PATTERN.fullmatch(self.source_id):
            raise ValueError("Source ID may contain only letters, numbers, dot, underscore and dash")
        if self.timeout_seconds <= 0:
            raise ValueError("Source timeout must be positive")
        if self.freshness_seconds <= 0:
            raise ValueError("Source freshness must be positive")
        if OperatingSystem.MACOS not in self.operating_systems:
            raise ValueError("Source must support macos")
        if self.source_id == "openusage" and (
            self.credential_type is not CredentialType.PROVIDER_OWNED
            or self.credential_scope is not None
        ):
            raise ValueError("OpenUsage credentials must remain provider-owned")
        if self.credential_scope is not None and not ID_PATTERN.fullmatch(
            self.credential_scope
        ):
            raise ValueError(
                "Credential scope may contain only letters, numbers, dot, underscore and dash"
            )


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    display_name: str
    category: str
    metric_families: frozenset[MetricFamily]
    regions: frozenset[str]
    supports_accounts: bool
    sources: tuple[SourceCapability, ...]
    capabilities: ProviderCapabilities

    def __post_init__(self) -> None:
        _require_instance(self.provider_id, str, "Provider ID")
        _require_instance(self.display_name, str, "Provider display name")
        _require_instance(self.category, str, "Provider category")
        _require_instance(
            self.metric_families,
            frozenset,
            "Provider metric families",
        )
        for metric_family in self.metric_families:
            _require_instance(
                metric_family,
                MetricFamily,
                "Provider metric family",
            )
        _require_instance(self.regions, frozenset, "Provider regions")
        for region in self.regions:
            _require_instance(region, str, "Provider region")
        _require_instance(self.supports_accounts, bool, "Provider supports_accounts")
        _require_instance(self.sources, tuple, "Provider sources")
        for source in self.sources:
            _require_instance(source, SourceCapability, "Provider source")
        _require_instance(
            self.capabilities,
            ProviderCapabilities,
            "Provider capabilities",
        )
        if not ID_PATTERN.fullmatch(self.provider_id):
            raise ValueError(
                "Provider ID may contain only letters, numbers, dot, underscore and dash"
            )
        if not self.display_name.strip():
            raise ValueError("Provider display name must not be empty")
        if self.category not in {"api", "subscription", "local_tool"}:
            raise ValueError("Provider category is invalid")
        if not self.sources:
            raise ValueError("Provider must declare at least one source")


class ProviderRegistry:
    def __init__(self, descriptors: Iterable[ProviderDescriptor]) -> None:
        self._descriptors: dict[str, ProviderDescriptor] = {}
        for descriptor in descriptors:
            if descriptor.provider_id in self._descriptors:
                raise ValueError(f"Duplicate provider ID: {descriptor.provider_id}")
            self._descriptors[descriptor.provider_id] = descriptor

    def require(self, provider_id: str) -> ProviderDescriptor:
        return self._descriptors[provider_id]

    @property
    def descriptors(self) -> tuple[ProviderDescriptor, ...]:
        """Return the canonical, immutable provider catalog in stable order."""
        return tuple(
            self._descriptors[provider_id]
            for provider_id in sorted(self._descriptors)
        )

    def resolve(self, provider_id: str, display_name: str) -> ProviderDescriptor:
        existing = self._descriptors.get(provider_id)
        if existing is not None:
            return existing
        return ProviderDescriptor(
            provider_id=provider_id,
            display_name=display_name,
            category="api",
            metric_families=frozenset(
                {MetricFamily.TOKEN_ACTIVITY, MetricFamily.BILLING}
            ),
            regions=frozenset(),
            supports_accounts=False,
            sources=(
                SourceCapability(
                    source_id="openusage",
                    kind=SourceKind.OPENUSAGE,
                    timeout_seconds=12,
                    freshness_seconds=300,
                    credential_type=CredentialType.PROVIDER_OWNED,
                    operating_systems=frozenset({OperatingSystem.MACOS}),
                    stability=SourceStability.PINNED,
                    provenance=SourceProvenance.OPENUSAGE_UPSTREAM,
                ),
            ),
            capabilities=_unknown_capabilities(),
        )


def state_from_card(status: ProviderStatus, stale: bool) -> ObservationState:
    if stale or status is ProviderStatus.STALE:
        return ObservationState.STALE
    return {
        ProviderStatus.OK: ObservationState.OK,
        ProviderStatus.UNKNOWN: ObservationState.TEMPORARILY_UNAVAILABLE,
        ProviderStatus.AUTH: ObservationState.AUTH_EXPIRED,
        ProviderStatus.RATE_LIMITED: ObservationState.TEMPORARILY_UNAVAILABLE,
        ProviderStatus.ERROR: ObservationState.TEMPORARILY_UNAVAILABLE,
    }[status]


def _descriptor_from_family(family: ProviderFamily) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id=family.family_id,
        display_name=family.display_name,
        category=family.category,
        metric_families=frozenset(
            MetricFamily(metric) for metric in family.metric_families
        ),
        regions=family.regions,
        supports_accounts=family.supports_accounts,
        sources=tuple(
            SourceCapability(
                source_id=source.source_id,
                kind=SourceKind(source.kind),
                timeout_seconds=source.timeout_seconds,
                freshness_seconds=source.freshness_seconds,
                credential_type=CredentialType(source.credential_type),
                operating_systems=frozenset(
                    OperatingSystem(system) for system in source.operating_systems
                ),
                stability=SourceStability(source.stability),
                provenance=SourceProvenance(source.provenance),
                credential_scope=source.credential_scope,
            )
            for source in family.sources
        ),
        capabilities=ProviderCapabilities(
            quota_windows=QuotaWindowCapability(
                state=CapabilityState(family.capabilities.quota_windows.state),
                values=tuple(
                    QuotaWindow(window)
                    for window in family.capabilities.quota_windows.values
                ),
            ),
            token_history=CapabilityState(family.capabilities.token_history),
            model_breakdown=CapabilityState(family.capabilities.model_breakdown),
            reset_timestamps=CapabilityState(
                family.capabilities.reset_timestamps
            ),
            billing=CapabilityState(family.capabilities.billing),
            credits=CapabilityState(family.capabilities.credits),
            balance=CapabilityState(family.capabilities.balance),
            cost=CapabilityState(family.capabilities.cost),
            rate_limits=CapabilityState(family.capabilities.rate_limits),
            service_status=CapabilityState(family.capabilities.service_status),
        ),
    )


def _unknown_capabilities() -> ProviderCapabilities:
    return ProviderCapabilities(
        quota_windows=QuotaWindowCapability(CapabilityState.UNKNOWN, ()),
        token_history=CapabilityState.UNKNOWN,
        model_breakdown=CapabilityState.UNKNOWN,
        reset_timestamps=CapabilityState.UNKNOWN,
        billing=CapabilityState.UNKNOWN,
        credits=CapabilityState.UNKNOWN,
        balance=CapabilityState.UNKNOWN,
        cost=CapabilityState.UNKNOWN,
        rate_limits=CapabilityState.UNKNOWN,
        service_status=CapabilityState.UNKNOWN,
    )


registry = ProviderRegistry(
    _descriptor_from_family(family) for family in catalog.families
)
