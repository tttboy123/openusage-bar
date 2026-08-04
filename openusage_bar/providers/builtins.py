from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ..codex_daily import CodexLocalDailyImporter
from ..codex_subscription import CodexSubscriptionAdapter
from ..config import (
    DailyCostFeedConfig,
    DailyUsageFeedConfig,
    GenericProviderConfig,
    MiniMaxConfig,
    MoonshotConfig,
    OpenAIOrganizationConfig,
    StepPlanConfig,
)
from ..cost_feed import DailyCostFeedImporter
from ..daily_feed import DailyUsageFeedImporter
from ..daily_history import OpenUsageDailyImporter
from ..deepseek_openusage import OpenUsageDeepSeekAdapter
from ..generic import GenericHTTPSAdapter
from ..kiro import KiroQuotaAdapter
from ..minimax import (
    MiniMaxBillingImporter,
    MiniMaxCodingPlanAdapter,
    minimax_endpoints_for_site,
)
from ..moonshot import MoonshotBalanceAdapter
from ..network import BoundedHTTPClient
from ..openai_organization import OpenAIOrganizationImporter
from ..openusage_adapter import OpenUsageDiscoveryAdapter
from ..step_plan import StepPlanAdapter, endpoints_for_site
from .contracts import ProviderBinding, ProviderDescriptor, SourceAttribution
from .registry import AdapterRegistry


def _performance_source(source: object, source_class: str) -> object:
    if source_class not in {"network", "local_file", "child_process"}:
        raise ValueError("invalid performance source class")
    source.performance_source_class = source_class
    return source


def _attributed_source(
    source: object,
    source_class: str,
    credential_source: str,
    source_kind: str,
) -> object:
    source.source_attribution = SourceAttribution(
        credential_source=credential_source,
        source_kind=source_kind,
    )
    return _performance_source(source, source_class)


def _quota_source(
    source: object,
    source_id: str,
    priority: int,
    source_class: str = "network",
) -> object:
    # Existing adapters are intentionally left behavior-compatible in Task 1;
    # registry metadata makes their cross-Provider merge order explicit.
    source.source_id = source_id
    source.source_priority = priority
    return _performance_source(source, source_class)


def _descriptor(
    provider_id: str,
    family_id: str,
    display_name: str,
    category: str,
) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id=provider_id,
        family_id=family_id,
        display_name=display_name,
        category=category,
    )


def default_registry(
    *, clock: Callable[[], datetime], keychain: object
) -> AdapterRegistry:
    """Build the production registry with shared, bounded dependencies."""

    registry = AdapterRegistry()
    generic_client = BoundedHTTPClient()
    daily_feed_client = BoundedHTTPClient(allowed_redirect_hosts=set())
    openai_client = BoundedHTTPClient(allowed_redirect_hosts=set())
    moonshot_client = BoundedHTTPClient(allowed_redirect_hosts=set())

    def openusage() -> ProviderBinding:
        importer = _performance_source(
            OpenUsageDailyImporter(clock=clock), "child_process"
        )
        discovery = _performance_source(
            OpenUsageDiscoveryAdapter(
                clock=clock,
                openusage_path=importer.openusage_path,
                capability_probe=importer.export_v1_capabilities,
            ),
            "child_process",
        )
        return ProviderBinding(
            provider_id="openusage", family_id="openusage",
            descriptor=_descriptor(
                "openusage", "openusage", "OpenUsage", "local_tool"
            ),
            discovery_sources=(discovery,),
            usage_sources=(importer,),
        )

    registry.register_global(openusage)
    def deepseek_openusage() -> ProviderBinding:
        adapter = OpenUsageDeepSeekAdapter(clock=clock)
        return ProviderBinding(
            provider_id="deepseek", family_id="deepseek",
            descriptor=_descriptor("deepseek", "deepseek", "DeepSeek", "api"),
            balance_sources=(_performance_source(adapter, "child_process"),),
            cost_sources=(_performance_source(adapter, "child_process"),),
        )

    registry.register_global(deepseek_openusage)
    registry.register_global(lambda: ProviderBinding(
        provider_id="kiro_cli", family_id="kiro_cli",
        descriptor=_descriptor(
            "kiro_cli", "kiro_cli", "Kiro", "subscription"
        ),
        quota_sources=(_quota_source(
            KiroQuotaAdapter(clock=clock), "kiro.codewhisperer", 20
        ),),
    ))
    registry.register_global(lambda: ProviderBinding(
        provider_id="codex", family_id="codex",
        descriptor=_descriptor("codex", "codex", "Codex", "subscription"),
        quota_sources=(_quota_source(
            CodexSubscriptionAdapter(clock=clock),
            "codex.local_rate_limits",
            20,
            "local_file",
        ),),
        usage_sources=(_performance_source(
            CodexLocalDailyImporter(clock=clock), "local_file"
        ),),
    ))

    def minimax(config: MiniMaxConfig) -> ProviderBinding:
        endpoints = minimax_endpoints_for_site(config.site)
        client = BoundedHTTPClient(
            allowed_reserved_hosts={endpoints.host},
            allowed_redirect_hosts=set(),
        )
        usage_sources = (
            (_performance_source(
                MiniMaxBillingImporter(config, keychain, client, clock),
                "network",
            ),)
            if endpoints.billing is not None
            else ()
        )
        return ProviderBinding(
            provider_id=config.provider_id, family_id="minimax",
            descriptor=_descriptor(
                config.provider_id, "minimax", config.name, "subscription"
            ),
            quota_sources=(_quota_source(MiniMaxCodingPlanAdapter(
                config, keychain, client, clock
            ), "minimax.coding_plan", 20),),
            usage_sources=usage_sources,
        )

    def openai(config: OpenAIOrganizationConfig) -> ProviderBinding:
        importer = _attributed_source(
            OpenAIOrganizationImporter(config, keychain, openai_client, clock),
            "network",
            "openai_admin_api",
            "official_api",
        )
        return ProviderBinding(
            provider_id=config.provider_id, family_id="openai",
            descriptor=_descriptor(
                config.provider_id, "openai", config.name, "api"
            ),
            usage_sources=(importer,), cost_sources=(importer,),
        )

    def moonshot(config: MoonshotConfig) -> ProviderBinding:
        return ProviderBinding(
            provider_id=config.provider_id,
            family_id="moonshot",
            descriptor=_descriptor(
                config.provider_id, "moonshot", config.name, "api"
            ),
            balance_sources=(
                _performance_source(
                    MoonshotBalanceAdapter(
                        config, keychain, moonshot_client, clock
                    ),
                    "network",
                ),
            ),
        )

    def daily_feed(config: DailyUsageFeedConfig) -> ProviderBinding:
        importer = _attributed_source(
            DailyUsageFeedImporter(
                config, keychain, daily_feed_client, clock
            ),
            "network",
            "api_key",
            "generic_https",
        )
        return ProviderBinding(
            provider_id=config.provider_id, family_id=config.family_id,
            descriptor=_descriptor(
                config.provider_id, config.family_id, config.name, "api"
            ),
            usage_sources=(importer,),
        )

    def cost_feed(config: DailyCostFeedConfig) -> ProviderBinding:
        importer = _attributed_source(
            DailyCostFeedImporter(
                config, keychain, daily_feed_client, clock
            ),
            "network",
            "api_key",
            "generic_https",
        )
        return ProviderBinding(
            provider_id=config.provider_id, family_id=config.family_id,
            descriptor=_descriptor(
                config.provider_id, config.family_id, config.name, "api"
            ),
            cost_sources=(importer,),
        )

    def step_plan(config: StepPlanConfig) -> ProviderBinding:
        endpoints = endpoints_for_site(config.site)
        client = BoundedHTTPClient(
            allowed_reserved_hosts={endpoints.api_host, endpoints.platform_host},
            allowed_redirect_hosts=set(),
        )
        return ProviderBinding(
            provider_id=config.provider_id, family_id="step_plan",
            descriptor=_descriptor(
                config.provider_id, "step_plan", config.name, "subscription"
            ),
            quota_sources=(_quota_source(StepPlanAdapter(
                config, keychain, client, clock
            ), "step_plan.quota", 20),),
        )

    def generic(config: GenericProviderConfig) -> ProviderBinding:
        return ProviderBinding(
            provider_id=config.provider_id,
            family_id=config.family_id or config.provider_id,
            descriptor=_descriptor(
                config.provider_id,
                config.family_id or config.provider_id,
                config.name,
                "api",
            ),
            quota_sources=(_quota_source(GenericHTTPSAdapter(
                config, keychain, generic_client, clock
            ), "generic.quota", 20),),
        )

    registry.register_config(MiniMaxConfig, minimax)
    registry.register_config(MoonshotConfig, moonshot)
    registry.register_config(OpenAIOrganizationConfig, openai)
    registry.register_config(DailyUsageFeedConfig, daily_feed)
    registry.register_config(DailyCostFeedConfig, cost_feed)
    registry.register_config(StepPlanConfig, step_plan)
    registry.register_config(GenericProviderConfig, generic)
    return registry
