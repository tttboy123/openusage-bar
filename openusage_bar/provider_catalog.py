from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .config import ID_PATTERN


PROVIDER_CATEGORIES = frozenset({"api", "subscription", "local_tool"})
METRIC_FAMILIES = frozenset(
    {"subscription_quota", "token_activity", "billing", "operational"}
)
CREDENTIAL_TYPES = frozenset(
    {
        "provider_owned",
        "api_key",
        "oauth",
        "cli",
        "local",
        "keychain",
        "browser_session",
    }
)
SOURCE_KINDS = frozenset(
    {
        "openusage",
        "builtin_api",
        "official_api",
        "cli",
        "local_log",
        "local_database",
        "keychain",
        "browser_session",
    }
)
CAPABILITY_STATES = frozenset({"supported", "unsupported", "unknown"})
QUOTA_WINDOWS = frozenset(
    {
        "session",
        "five_hour",
        "weekly",
        "monthly",
        "billing_cycle",
        "model_specific",
    }
)
OPERATING_SYSTEMS = frozenset({"macos", "windows", "linux"})
SOURCE_STABILITIES = frozenset({"stable", "experimental", "pinned", "opaque"})
SOURCE_PROVENANCES = frozenset(
    {
        "openusage_upstream",
        "openusage_bar_builtin",
        "provider_official",
        "provider_local",
        "user_session",
    }
)
SOURCE_FACT_FAMILIES = frozenset(
    {
        "detection",
        "token_activity",
        "subscription_capacity",
        "api_balance",
        "api_spend",
    }
)
SOURCE_AUTHORITIES = frozenset(
    {
        "provider_official",
        "provider_local",
        "third_party",
        "user_supplied",
        "unknown",
    }
)
ACCOUNT_SCOPES = frozenset(
    {"local_profile", "configured_account", "organization", "provider", "unknown"}
)
MODEL_SCOPES = frozenset({"per_model", "aggregate", "mixed", "unknown"})
SOURCE_VERIFICATIONS = frozenset(
    {"live_account", "fixture", "upstream_declared", "unverified"}
)

_EXPECTED_UPSTREAM_FAMILY_IDS = tuple(
    sorted(
        {
            "openai", "anthropic", "azure_openai", "alibaba_cloud",
            "openrouter", "perplexity", "groq", "mistral", "moonshot",
            "deepseek", "xai", "zai", "gemini_api", "opencode",
            "gemini_cli", "copilot", "cursor", "claude_code", "codex",
            "amp", "goose", "hermes", "mux", "droid", "crush",
            "roocode", "kilo_code", "kiro_cli", "zed", "codebuff",
            "kimi_cli", "openclaw", "pi", "qwen_cli", "ollama",
        }
    )
)
_EXPECTED_BUILTIN_FAMILY_IDS = (
    "cc_switch",
    "minimax",
    "omniroute",
    "step_plan",
)
_EXPECTED_FAMILY_IDS = tuple(
    sorted(_EXPECTED_UPSTREAM_FAMILY_IDS + _EXPECTED_BUILTIN_FAMILY_IDS)
)
_SPECIAL_SOURCE_IDS = {
    "cc_switch": ("cc_switch.status", "cc_switch.rollups"),
    "deepseek": ("deepseek_official_api", "openusage"),
    "openai": ("openai_admin_api", "openusage"),
    "codex": ("codex_local_log", "openusage"),
    "kiro_cli": ("kiro_keychain", "kiro_codewhisperer_api", "openusage"),
    "minimax": (
        "minimax_builtin_api",
        "minimax_china_billing_web",
        "openusage",
    ),
    "moonshot": ("moonshot_official_api", "openusage"),
    "omniroute": ("omniroute.usage",),
    "step_plan": ("step_plan_browser_session", "step_plan_official_api"),
}
_EXPECTED_CREDENTIAL_SCOPES = {
    ("deepseek", "deepseek_official_api"): "deepseek_api_key",
    ("openai", "openai_admin_api"): "openai_admin_api_key",
    ("kiro_cli", "kiro_keychain"): "kiro",
    ("kiro_cli", "kiro_codewhisperer_api"): "kiro",
    ("minimax", "minimax_builtin_api"): "minimax",
    ("minimax", "minimax_china_billing_web"): "minimax",
    ("moonshot", "moonshot_official_api"): "moonshot_api_key",
    ("step_plan", "step_plan_browser_session"): "step_plan_session",
    ("step_plan", "step_plan_official_api"): "step_plan_api_key",
}

_TOP_LEVEL_FIELDS = frozenset({"schema_version", "upstream", "families"})
_UPSTREAM_FIELDS = frozenset({"version", "revision", "family_ids"})
_FAMILY_FIELDS = frozenset(
    {
        "aliases",
        "id",
        "display_name",
        "category",
        "metric_families",
        "regions",
        "supports_accounts",
        "capabilities",
        "sources",
    }
)
_REQUIRED_FAMILY_FIELDS = _FAMILY_FIELDS - {"aliases"}
_CAPABILITY_FIELDS = frozenset(
    {
        "quota_windows",
        "token_history",
        "model_breakdown",
        "reset_timestamps",
        "billing",
        "credits",
        "balance",
        "cost",
        "rate_limits",
        "service_status",
    }
)
_QUOTA_WINDOW_FIELDS = frozenset({"state", "values"})
_SOURCE_FIELDS = frozenset(
    {
        "source_id",
        "kind",
        "timeout_seconds",
        "freshness_seconds",
        "credential_type",
        "credential_scope",
        "operating_systems",
        "stability",
        "provenance",
        "fact_families",
        "authority",
        "account_scope",
        "model_scope",
        "verification",
    }
)


@dataclass(frozen=True)
class QuotaWindowCapability:
    state: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class ProviderCapabilities:
    quota_windows: QuotaWindowCapability
    token_history: str
    model_breakdown: str
    reset_timestamps: str
    billing: str
    credits: str
    balance: str
    cost: str
    rate_limits: str
    service_status: str


@dataclass(frozen=True)
class CatalogSource:
    source_id: str
    kind: str
    timeout_seconds: int
    freshness_seconds: int
    credential_type: str
    operating_systems: frozenset[str]
    stability: str
    provenance: str
    fact_families: frozenset[str]
    authority: str
    account_scope: str
    model_scope: str
    verification: str
    credential_scope: str | None = None


@dataclass(frozen=True)
class SourcePlatformCapability:
    family_id: str
    source_id: str
    supported: bool
    reason_code: str


@dataclass(frozen=True)
class ObserverPlatformSummary:
    operating_system: str | None
    support: str
    supported_source_count: int | None
    total_source_count: int
    reason_code: str


@dataclass(frozen=True)
class ProviderFamily:
    family_id: str
    display_name: str
    category: str
    metric_families: frozenset[str]
    regions: frozenset[str]
    supports_accounts: bool
    capabilities: ProviderCapabilities
    sources: tuple[CatalogSource, ...]
    aliases: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ProviderCatalog:
    upstream_version: str
    upstream_revision: str
    upstream_family_ids: tuple[str, ...]
    families: tuple[ProviderFamily, ...]
    _families: Mapping[str, ProviderFamily] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        upstream_family_ids = tuple(self.upstream_family_ids)
        families = tuple(self.families)
        object.__setattr__(self, "upstream_family_ids", upstream_family_ids)
        object.__setattr__(self, "families", families)
        object.__setattr__(
            self,
            "_families",
            MappingProxyType({family.family_id: family for family in families}),
        )

    @property
    def family_ids(self) -> tuple[str, ...]:
        return tuple(family.family_id for family in self.families)

    def require(self, family_id: str) -> ProviderFamily:
        return self._families[family_id]

    def require_operating_system(self, operating_system: str) -> None:
        """Validate a product distribution separately from the core schema."""
        selected = _require_enum(
            operating_system,
            OPERATING_SYSTEMS,
            "Distribution operating system",
        )
        unsupported = tuple(
            f"{family.family_id}/{source.source_id}"
            for family in self.families
            for source in family.sources
            if selected not in source.operating_systems
        )
        if unsupported:
            raise ValueError(
                f"{selected} distribution cannot register sources: "
                + ", ".join(unsupported)
            )

    def source_platform_capabilities(
        self, operating_system: str
    ) -> tuple[SourcePlatformCapability, ...]:
        """Return source support for an injected catalog operating system.

        This is a pure catalog query used by cross-platform clients to display
        capability state. It intentionally does not inspect the host runtime,
        credential store, filesystem, network, or executable PATH.
        """
        selected = _require_enum(
            operating_system,
            OPERATING_SYSTEMS,
            "Source capability operating system",
        )
        return tuple(
            SourcePlatformCapability(
                family_id=family.family_id,
                source_id=source.source_id,
                supported=selected in source.operating_systems,
                reason_code=(
                    "supported_sources_available"
                    if selected in source.operating_systems
                    else "source_level_evidence_unverified"
                ),
            )
            for family in self.families
            for source in family.sources
        )

    def search(self, query: str) -> tuple[ProviderFamily, ...]:
        """Search public labels without rewriting a Provider identity."""
        if not isinstance(query, str):
            raise TypeError("Provider search query must be a string")
        normalized = query.strip().casefold()
        if not normalized or len(normalized) > 128:
            return ()
        return tuple(
            family
            for family in self.families
            if any(
                normalized in label.casefold()
                for label in (
                    family.family_id,
                    family.display_name,
                    *sorted(family.aliases),
                )
            )
        )

    def resolve(self, family_id: str, display_name: str) -> ProviderFamily:
        existing = self._families.get(family_id)
        if existing is not None:
            return existing
        _require_id(family_id, "Family ID")
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError("Provider display name must not be empty")
        return ProviderFamily(
            family_id=family_id,
            display_name=display_name,
            category="api",
            metric_families=frozenset({"token_activity", "billing"}),
            regions=frozenset(),
            supports_accounts=False,
            capabilities=_unknown_capabilities(),
            sources=(
                CatalogSource(
                    "openusage",
                    "openusage",
                    12,
                    300,
                    "provider_owned",
                    frozenset({"macos"}),
                    "pinned",
                    "openusage_upstream",
                    frozenset({"detection"}),
                    "third_party",
                    "local_profile",
                    "unknown",
                    "unverified",
                ),
            ),
        )

    def instance_display_name(self, family_id: str, display_name: str) -> str:
        """Return the public label without guessing an instance's family.

        A configured label remains authoritative unless it is only the known
        family identifier with different casing. In that one exact case the
        catalog owns brand typography (for example ``minimax`` -> ``MiniMax``).
        Unknown families and meaningful account labels pass through unchanged.
        """
        family = self._families.get(family_id)
        if (
            family is not None
            and display_name.isascii()
            and display_name.lower() == family_id.lower()
        ):
            return family.display_name
        return display_name


@dataclass(frozen=True, init=False)
class ObserverPlatformResolver:
    """Resolve Observer support from an explicitly injected runtime name.

    The resolver is deliberately a pure, closed catalog projection. It never
    reads the host platform, environment, filesystem, credential store,
    executable PATH, or network. Application boundaries own injection of the
    runtime platform value.
    """

    operating_system: str | None
    summary: ObserverPlatformSummary
    source_capabilities: tuple[SourcePlatformCapability, ...]
    _source_by_pair: Mapping[
        tuple[str, str], SourcePlatformCapability
    ] = field(repr=False, compare=False)
    _supported_source_ids: frozenset[str] = field(
        repr=False, compare=False
    )

    def __init__(
        self,
        provider_catalog: ProviderCatalog,
        *,
        runtime_platform: object,
    ) -> None:
        if not isinstance(provider_catalog, ProviderCatalog):
            raise TypeError("provider_catalog must be a ProviderCatalog")

        operating_system = _observer_operating_system(runtime_platform)
        if operating_system is None:
            records = tuple(
                SourcePlatformCapability(
                    family_id=family.family_id,
                    source_id=source.source_id,
                    supported=False,
                    reason_code="runtime_platform_unknown",
                )
                for family in provider_catalog.families
                for source in family.sources
            )
            supported_count: int | None = None
            support = "unknown"
            reason_code = "runtime_platform_unknown"
        else:
            records = provider_catalog.source_platform_capabilities(
                operating_system
            )
            supported_count = sum(record.supported for record in records)
            support = "supported" if supported_count else "unsupported"
            reason_code = (
                "supported_sources_available"
                if supported_count
                else "source_level_evidence_unverified"
            )

        summary = ObserverPlatformSummary(
            operating_system=operating_system,
            support=support,
            supported_source_count=supported_count,
            total_source_count=len(records),
            reason_code=reason_code,
        )
        source_by_pair = MappingProxyType(
            {
                (record.family_id, record.source_id): record
                for record in records
            }
        )
        supported_source_ids = frozenset(
            record.source_id for record in records if record.supported
        )

        object.__setattr__(self, "operating_system", operating_system)
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "source_capabilities", records)
        object.__setattr__(self, "_source_by_pair", source_by_pair)
        object.__setattr__(
            self, "_supported_source_ids", supported_source_ids
        )

    def source_capability(
        self, family_id: str, source_id: str
    ) -> SourcePlatformCapability | None:
        """Return the frozen source fact, or ``None`` for an unknown pair."""
        if type(family_id) is not str or type(source_id) is not str:
            return None
        return self._source_by_pair.get((family_id, source_id))

    def supports_source(self, family_id: str, source_id: str) -> bool:
        record = self.source_capability(family_id, source_id)
        return record is not None and record.supported

    def supports_any_source_id(self, source_id: str) -> bool:
        if type(source_id) is not str:
            return False
        return source_id in self._supported_source_ids

    def supports_all_source_id(self, source_id: str) -> bool:
        """Require every catalog use of a shared source to be supported.

        A missing source ID and an unknown runtime both fail closed. This
        prevents one newly verified family from enabling a shared adapter for
        other families whose use of the same source remains unverified.
        """
        if type(source_id) is not str:
            return False
        matched = False
        for record in self.source_capabilities:
            if record.source_id != source_id:
                continue
            matched = True
            if not record.supported:
                return False
        return matched

    @property
    def supports_legacy_unmodeled_sources(self) -> bool:
        """Preserve legacy adapters only inside the established macOS contract."""
        return self.operating_system == "macos"


def _observer_operating_system(runtime_platform: object) -> str | None:
    if type(runtime_platform) is not str:
        return None
    if runtime_platform == "darwin":
        return "macos"
    if runtime_platform == "win32":
        return "windows"
    if runtime_platform.startswith("linux"):
        return "linux"
    return None


def load_provider_catalog(path: str | Path | None = None) -> ProviderCatalog:
    if path is None:
        resource = files("openusage_bar").joinpath(
            "resources/provider-catalog.v1.json"
        )
        payload = json.loads(resource.read_text(encoding="utf-8"))
    else:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return _parse_catalog(payload)


def _parse_catalog(payload: Any) -> ProviderCatalog:
    root = _require_object(payload, "catalog")
    _reject_unknown_fields(root, _TOP_LEVEL_FIELDS, "catalog")
    _require_exact_fields(root, _TOP_LEVEL_FIELDS, "catalog")
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise ValueError("Catalog schema_version must be 1")

    upstream = _require_object(root["upstream"], "upstream")
    _reject_unknown_fields(upstream, _UPSTREAM_FIELDS, "upstream")
    _require_exact_fields(upstream, _UPSTREAM_FIELDS, "upstream")
    version = _require_nonempty_string(upstream["version"], "upstream version")
    revision = _require_nonempty_string(
        upstream["revision"], "upstream revision"
    )
    upstream_family_ids = _require_sorted_unique_strings(
        upstream["family_ids"], "upstream family_ids", identifiers=True
    )
    if (
        version != "0.23.0"
        or revision != "3059f1b"
        or upstream_family_ids != _EXPECTED_UPSTREAM_FAMILY_IDS
    ):
        raise ValueError("Upstream provider boundary does not match OpenUsage 0.23.0")

    raw_families = root["families"]
    if not isinstance(raw_families, list) or not raw_families:
        raise ValueError("Catalog families must be a non-empty list")
    families = tuple(
        _parse_family(value, index) for index, value in enumerate(raw_families)
    )
    family_ids = tuple(family.family_id for family in families)
    if family_ids != tuple(sorted(family_ids)):
        raise ValueError("Catalog family IDs must be sorted")
    if len(set(family_ids)) != len(family_ids):
        raise ValueError("Catalog family IDs must be unique")
    if family_ids != _EXPECTED_FAMILY_IDS:
        raise ValueError(
            "Catalog family boundary must contain exactly 35 upstream and 2 builtins"
        )
    for family_id in upstream_family_ids:
        family = families[family_ids.index(family_id)]
        openusage_sources = [
            source for source in family.sources if source.source_id == "openusage"
        ]
        if len(openusage_sources) != 1:
            raise ValueError(
                f"Upstream family {family_id} must declare one openusage source"
            )
        source = openusage_sources[0]
        if source.kind != "openusage" or source.credential_type != "provider_owned":
            raise ValueError(
                f"Upstream family {family_id} has an invalid openusage source"
            )
        if source.credential_scope is not None:
            raise ValueError("OpenUsage source must not expose credential scope")
    for family in families:
        expected_source_ids = _SPECIAL_SOURCE_IDS.get(
            family.family_id, ("openusage",)
        )
        actual_source_ids = tuple(source.source_id for source in family.sources)
        if actual_source_ids != expected_source_ids:
            raise ValueError(
                f"Provider source boundary mismatch for {family.family_id}"
            )
        for source in family.sources:
            expected_scope = _EXPECTED_CREDENTIAL_SCOPES.get(
                (family.family_id, source.source_id)
            )
            if source.credential_scope != expected_scope:
                raise ValueError(
                    f"Provider source boundary has invalid credential scope for "
                    f"{family.family_id}/{source.source_id}"
                )

    return ProviderCatalog(
        upstream_version=version,
        upstream_revision=revision,
        upstream_family_ids=upstream_family_ids,
        families=families,
    )


def _parse_family(value: Any, index: int) -> ProviderFamily:
    context = f"family[{index}]"
    raw = _require_object(value, context)
    _reject_unknown_fields(raw, _FAMILY_FIELDS, context)
    _require_exact_fields(raw, _REQUIRED_FAMILY_FIELDS, context)
    family_id = _require_id(raw["id"], f"{context} ID")
    display_name = _require_nonempty_string(
        raw["display_name"], f"{context} display name"
    )
    aliases = _require_aliases(raw.get("aliases", []), f"{context} aliases")
    category = _require_enum(
        raw["category"], PROVIDER_CATEGORIES, f"{context} category"
    )
    metrics = _require_sorted_unique_strings(
        raw["metric_families"], f"{context} metric families"
    )
    if not set(metrics) <= METRIC_FAMILIES:
        raise ValueError(f"{context} has unsupported metric family")
    regions = _require_sorted_unique_strings(raw["regions"], f"{context} regions")
    if type(raw["supports_accounts"]) is not bool:
        raise ValueError(f"{context} supports_accounts must be a boolean")
    capabilities = _parse_capabilities(raw["capabilities"], context)
    raw_sources = raw["sources"]
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ValueError(f"{context} sources must be a non-empty list")
    sources = tuple(
        _parse_source(source, family_id, source_index)
        for source_index, source in enumerate(raw_sources)
    )
    source_ids = [source.source_id for source in sources]
    if len(set(source_ids)) != len(source_ids):
        raise ValueError(f"{context} source IDs must be unique")
    declared_facts = frozenset(
        fact
        for source in sources
        for fact in source.fact_families
    )
    token_supported = (
        capabilities.token_history == "supported"
        or capabilities.model_breakdown == "supported"
    )
    capacity_supported = (
        capabilities.quota_windows.state == "supported"
        or capabilities.reset_timestamps == "supported"
        or capabilities.credits == "supported"
    )
    # Legacy quota families may label a remaining credit pool as "balance".
    # A standalone API balance requires its own evidence only when the family
    # does not also declare subscription-capacity semantics.
    balance_supported = (
        capabilities.balance == "supported" and not capacity_supported
    )
    spend_supported = (
        capabilities.billing == "supported"
        or capabilities.cost == "supported"
    )
    evidence_contract = (
        ("token_activity", token_supported),
        ("subscription_capacity", capacity_supported),
        ("api_balance", balance_supported),
        ("api_spend", spend_supported),
    )
    for fact_family, supported in evidence_contract:
        if (fact_family in declared_facts) != supported:
            raise ValueError(
                f"{context} fact evidence does not match {fact_family} capability"
            )
    return ProviderFamily(
        family_id=family_id,
        display_name=display_name,
        category=category,
        metric_families=frozenset(metrics),
        regions=frozenset(regions),
        supports_accounts=raw["supports_accounts"],
        capabilities=capabilities,
        sources=sources,
        aliases=frozenset(aliases),
    )


def _parse_capabilities(value: Any, family_context: str) -> ProviderCapabilities:
    context = f"{family_context} capabilities"
    raw = _require_object(value, context)
    _reject_unknown_fields(raw, _CAPABILITY_FIELDS, context)
    _require_exact_fields(raw, _CAPABILITY_FIELDS, context)

    quota_context = f"{context} quota_windows"
    quota = _require_object(raw["quota_windows"], quota_context)
    _reject_unknown_fields(quota, _QUOTA_WINDOW_FIELDS, quota_context)
    _require_exact_fields(quota, _QUOTA_WINDOW_FIELDS, quota_context)
    quota_state = _require_enum(quota["state"], CAPABILITY_STATES, quota_context)
    quota_values = _require_sorted_unique_strings(
        quota["values"], f"{quota_context} values"
    )
    if not set(quota_values) <= QUOTA_WINDOWS:
        raise ValueError(f"{quota_context} has unsupported value")
    if quota_state == "supported" and not quota_values:
        raise ValueError(f"{quota_context} supported state requires values")
    if quota_state != "supported" and quota_values:
        raise ValueError(f"{quota_context} {quota_state} state requires empty values")

    states = {
        field: _require_enum(raw[field], CAPABILITY_STATES, f"{context} {field}")
        for field in _CAPABILITY_FIELDS - {"quota_windows"}
    }
    return ProviderCapabilities(
        quota_windows=QuotaWindowCapability(quota_state, quota_values),
        **states,
    )


def _parse_source(value: Any, family_id: str, index: int) -> CatalogSource:
    context = f"family {family_id} source[{index}]"
    raw = _require_object(value, context)
    _reject_unknown_fields(raw, _SOURCE_FIELDS, context)
    required = _SOURCE_FIELDS - {"credential_scope"}
    _require_exact_fields(raw, required, context)
    source_id = _require_id(raw["source_id"], f"{context} ID")
    kind = _require_enum(raw["kind"], SOURCE_KINDS, f"{context} kind")
    timeout = _require_positive_int(raw["timeout_seconds"], f"{context} timeout")
    freshness = _require_positive_int(
        raw["freshness_seconds"], f"{context} freshness"
    )
    credential_type = _require_enum(
        raw["credential_type"], CREDENTIAL_TYPES, f"{context} credential type"
    )
    scope = raw.get("credential_scope")
    if scope is not None:
        scope = _require_id(scope, f"{context} credential scope")
    if source_id == "openusage" and scope is not None:
        raise ValueError("OpenUsage source must not expose credential scope")
    operating_systems = _require_sorted_unique_strings(
        raw["operating_systems"], f"{context} operating systems"
    )
    if not set(operating_systems) <= OPERATING_SYSTEMS:
        raise ValueError(f"{context} has unsupported operating system")
    if not operating_systems:
        raise ValueError(f"{context} must declare an operating system")
    stability = _require_enum(raw["stability"], SOURCE_STABILITIES, context)
    provenance = _require_enum(raw["provenance"], SOURCE_PROVENANCES, context)
    fact_families = _require_sorted_unique_strings(
        raw["fact_families"], f"{context} fact families"
    )
    if not fact_families or not set(fact_families) <= SOURCE_FACT_FAMILIES:
        raise ValueError(f"{context} has invalid fact families")
    authority = _require_enum(
        raw["authority"], SOURCE_AUTHORITIES, f"{context} authority"
    )
    account_scope = _require_enum(
        raw["account_scope"], ACCOUNT_SCOPES, f"{context} account scope"
    )
    model_scope = _require_enum(
        raw["model_scope"], MODEL_SCOPES, f"{context} model scope"
    )
    verification = _require_enum(
        raw["verification"], SOURCE_VERIFICATIONS, f"{context} verification"
    )
    return CatalogSource(
        source_id=source_id,
        kind=kind,
        timeout_seconds=timeout,
        freshness_seconds=freshness,
        credential_type=credential_type,
        operating_systems=frozenset(operating_systems),
        stability=stability,
        provenance=provenance,
        fact_families=frozenset(fact_families),
        authority=authority,
        account_scope=account_scope,
        model_scope=model_scope,
        verification=verification,
        credential_scope=scope,
    )


def _require_object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return value


def _reject_unknown_fields(
    value: dict[str, Any], allowed: frozenset[str], context: str
) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{context} has unknown field(s): {sorted(unknown)}")


def _require_exact_fields(
    value: dict[str, Any], required: frozenset[str], context: str
) -> None:
    missing = required - set(value)
    if missing:
        raise ValueError(f"{context} is missing field(s): {sorted(missing)}")


def _require_nonempty_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be a non-empty string")
    return value


def _require_id(value: Any, context: str) -> str:
    text = _require_nonempty_string(value, context)
    if not ID_PATTERN.fullmatch(text):
        raise ValueError(
            f"{context} may contain only letters, numbers, dot, underscore and dash"
        )
    return text


def _require_positive_int(value: Any, context: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{context} must be a positive integer")
    return value


def _require_enum(value: Any, choices: frozenset[str], context: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{context} has invalid value")
    return value


def _require_sorted_unique_strings(
    value: Any, context: str, *, identifiers: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be a list")
    values = tuple(value)
    for entry in values:
        if identifiers:
            _require_id(entry, context)
        else:
            _require_nonempty_string(entry, context)
    if values != tuple(sorted(values)):
        raise ValueError(f"{context} must be sorted")
    if len(set(values)) != len(values):
        raise ValueError(f"{context} must contain unique values")
    return values


def _require_aliases(value: Any, context: str) -> tuple[str, ...]:
    aliases = _require_sorted_unique_strings(value, context)
    private_markers = (
        "authorization=", "credential=", "key=", "password=", "secret=", "token=",
    )
    for alias in aliases:
        folded = alias.casefold()
        if (
            alias != alias.strip()
            or len(alias) > 64
            or any(ord(character) < 32 for character in alias)
            or any(marker in folded for marker in private_markers)
        ):
            raise ValueError(f"{context} must contain bounded public labels")
    return aliases


def _unknown_capabilities() -> ProviderCapabilities:
    return ProviderCapabilities(
        quota_windows=QuotaWindowCapability("unknown", ()),
        token_history="unknown",
        model_breakdown="unknown",
        reset_timestamps="unknown",
        billing="unknown",
        credits="unknown",
        balance="unknown",
        cost="unknown",
        rate_limits="unknown",
        service_status="unknown",
    )


catalog = load_provider_catalog()
