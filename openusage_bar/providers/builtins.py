from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ..codex_daily import CodexLocalDailyImporter
from ..codex_subscription import CodexSubscriptionAdapter
from ..cc_switch import CcSwitchCostImporter, CcSwitchStatusAdapter
from ..config import (
    DailyCostFeedConfig,
    DailyUsageFeedConfig,
    GenericProviderConfig,
    MiniMaxConfig,
    MoonshotConfig,
    OpenAIOrganizationConfig,
    StepPlanConfig,
)
from ..cost_feed import DailyCostFeedCardAdapter, DailyCostFeedImporter
from ..daily_feed import DailyUsageFeedCardAdapter, DailyUsageFeedImporter
from ..daily_history import OpenUsageDailyImporter
from ..deepseek import DeepSeekBalanceAdapter
from ..generic import GenericHTTPSAdapter
from ..kiro import KiroQuotaAdapter
from ..minimax import (
    MiniMaxBillingImporter,
    MiniMaxCodingPlanAdapter,
    minimax_endpoints_for_site,
)
from ..moonshot import MoonshotBalanceAdapter
from ..network import BoundedHTTPClient
from ..openai_organization import (
    OpenAIOrganizationCardAdapter,
    OpenAIOrganizationImporter,
)
from ..openusage_adapter import OpenUsageAdapter
from ..omniroute import OmniRouteCostImporter
from ..step_plan import StepPlanAdapter, endpoints_for_site
from .contracts import ProviderBinding
from .registry import AdapterRegistry


def _performance_source(source: object, source_class: str) -> object:
    if source_class not in {"network", "local_file", "child_process"}:
        raise ValueError("invalid performance source class")
    source.performance_source_class = source_class
    return source


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


def default_registry(
    *, clock: Callable[[], datetime], keychain: object
) -> AdapterRegistry:
    """Build the production registry with shared, bounded dependencies."""

    registry = AdapterRegistry()
    generic_client = BoundedHTTPClient()
    daily_feed_client = BoundedHTTPClient(allowed_redirect_hosts=set())
    openai_client = BoundedHTTPClient(allowed_redirect_hosts=set())
    moonshot_client = BoundedHTTPClient(allowed_redirect_hosts=set())

    registry.register_global(lambda: ProviderBinding(
        provider_id="openusage", family_id="openusage",
        quota_sources=(_quota_source(
            OpenUsageAdapter(clock), "openusage.cards", 10, "child_process"
        ),),
        usage_sources=(_performance_source(
            OpenUsageDailyImporter(clock=clock), "child_process"
        ),),
    ))
    registry.register_global(lambda: ProviderBinding(
        provider_id="kiro_cli", family_id="kiro_cli",
        quota_sources=(_quota_source(
            KiroQuotaAdapter(clock=clock), "kiro.codewhisperer", 20
        ),),
    ))
    registry.register_global(lambda: ProviderBinding(
        provider_id="codex", family_id="codex",
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
    registry.register_global(lambda: ProviderBinding(
        provider_id="cc_switch", family_id="cc_switch",
        quota_sources=(_quota_source(
            CcSwitchStatusAdapter(), "cc_switch.status", 30, "local_file"
        ),),
        cost_sources=(_performance_source(
            CcSwitchCostImporter(), "local_file"
        ),),
    ))
    registry.register_global(lambda: ProviderBinding(
        provider_id="omniroute", family_id="omniroute",
        cost_sources=(_performance_source(
            OmniRouteCostImporter(), "local_file"
        ),),
    ))
    registry.register_global(lambda: ProviderBinding(
        provider_id="deepseek", family_id="deepseek",
        balance_sources=(_performance_source(
            DeepSeekBalanceAdapter(), "network"
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
            quota_sources=(_quota_source(MiniMaxCodingPlanAdapter(
                config, keychain, client, clock
            ), "minimax.coding_plan", 20),),
            usage_sources=usage_sources,
        )

    def openai(config: OpenAIOrganizationConfig) -> ProviderBinding:
        importer = _performance_source(
            OpenAIOrganizationImporter(config, keychain, openai_client, clock),
            "network",
        )
        return ProviderBinding(
            provider_id=config.provider_id, family_id="openai",
            quota_sources=(_quota_source(
                OpenAIOrganizationCardAdapter(config, keychain, clock),
                "openai.organization", 20
            ),),
            usage_sources=(importer,), cost_sources=(importer,),
        )

    def moonshot(config: MoonshotConfig) -> ProviderBinding:
        return ProviderBinding(
            provider_id=config.provider_id,
            family_id="moonshot",
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
        importer = _performance_source(
            DailyUsageFeedImporter(
                config, keychain, daily_feed_client, clock
            ),
            "network",
        )
        return ProviderBinding(
            provider_id=config.provider_id, family_id=config.family_id,
            quota_sources=(_quota_source(
                DailyUsageFeedCardAdapter(config, keychain, clock),
                "custom.daily", 20
            ),),
            usage_sources=(importer,),
        )

    def cost_feed(config: DailyCostFeedConfig) -> ProviderBinding:
        importer = _performance_source(
            DailyCostFeedImporter(
                config, keychain, daily_feed_client, clock
            ),
            "network",
        )
        return ProviderBinding(
            provider_id=config.provider_id, family_id=config.family_id,
            quota_sources=(_quota_source(
                DailyCostFeedCardAdapter(config, keychain, clock),
                "custom.cost", 20
            ),),
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
            quota_sources=(_quota_source(StepPlanAdapter(
                config, keychain, client, clock
            ), "step_plan.quota", 20),),
        )

    def generic(config: GenericProviderConfig) -> ProviderBinding:
        return ProviderBinding(
            provider_id=config.provider_id,
            family_id=config.family_id or config.provider_id,
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
