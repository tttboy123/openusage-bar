from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from ..codex_daily import CodexLocalDailyImporter
from ..codex_subscription import CodexSubscriptionAdapter
from ..cc_switch import CcSwitchCostImporter, CcSwitchStatusAdapter
from ..claude_code_daily import ClaudeCodeLocalDailyImporter
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
from ..openai_organization import (
    OpenAIOrganizationCardAdapter,
    OpenAIOrganizationImporter,
)
from ..openusage_adapter import OpenUsageAdapter
from ..omniroute import OmniRouteCostImporter
from ..step_plan import StepPlanAdapter, endpoints_for_site
from .contracts import ProviderBinding
from .registry import AdapterRegistry


class ObserverPlatform(Protocol):
    supports_legacy_unmodeled_sources: bool

    def supports_source(self, family_id: str, source_id: str) -> bool: ...

    def supports_any_source_id(self, source_id: str) -> bool: ...

    def supports_all_source_id(self, source_id: str) -> bool: ...


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
    *,
    clock: Callable[[], datetime],
    keychain: object | None = None,
    keychain_factory: Callable[[], object] | None = None,
    observer_platform: ObserverPlatform | None = None,
) -> AdapterRegistry:
    """Build the production registry with shared, bounded dependencies."""

    if (keychain is None) == (keychain_factory is None):
        raise ValueError("provide exactly one keychain or keychain factory")

    resolved_keychain = keychain

    def shared_keychain() -> object:
        nonlocal resolved_keychain
        if resolved_keychain is None:
            assert keychain_factory is not None
            resolved_keychain = keychain_factory()
        return resolved_keychain

    def supports(*sources: tuple[str, str]) -> bool:
        return observer_platform is None or all(
            observer_platform.supports_source(family_id, source_id)
            for family_id, source_id in sources
        )

    def supports_openusage() -> bool:
        return (
            observer_platform is None
            or observer_platform.supports_all_source_id("openusage")
        )

    def supports_legacy_unmodeled() -> bool:
        return (
            observer_platform is None
            or observer_platform.supports_legacy_unmodeled_sources is True
        )

    registry = AdapterRegistry()
    generic_client = BoundedHTTPClient()
    daily_feed_client = BoundedHTTPClient(allowed_redirect_hosts=set())
    openai_client = BoundedHTTPClient(allowed_redirect_hosts=set())
    moonshot_client = BoundedHTTPClient(allowed_redirect_hosts=set())

    registry.register_global(
        lambda: ProviderBinding(
            provider_id="openusage", family_id="openusage",
            quota_sources=(_quota_source(
                OpenUsageAdapter(clock), "openusage.cards", 10, "child_process"
            ),),
            usage_sources=(_performance_source(
                OpenUsageDailyImporter(clock=clock), "child_process"
            ),),
        ),
        supports_openusage,
    )
    registry.register_global(
        lambda: ProviderBinding(
            provider_id="kiro_cli", family_id="kiro_cli",
            quota_sources=(_quota_source(
                KiroQuotaAdapter(clock=clock), "kiro.codewhisperer", 20
            ),),
        ),
        lambda: supports(
            ("kiro_cli", "kiro_keychain"),
            ("kiro_cli", "kiro_codewhisperer_api"),
        ),
    )
    registry.register_global(
        lambda: ProviderBinding(
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
        ),
        lambda: supports(("codex", "codex_local_log")),
    )
    registry.register_global(
        lambda: ProviderBinding(
            provider_id="claude_code", family_id="claude_code",
            usage_sources=(_performance_source(
                ClaudeCodeLocalDailyImporter(clock=clock), "local_file"
            ),),
        ),
        supports_legacy_unmodeled,
    )
    registry.register_global(
        lambda: ProviderBinding(
            provider_id="cc_switch", family_id="cc_switch",
            quota_sources=(_quota_source(
                CcSwitchStatusAdapter(), "cc_switch.status", 30, "local_file"
            ),),
            cost_sources=(_performance_source(
                CcSwitchCostImporter(), "local_file"
            ),),
        ),
        lambda: supports(
            ("cc_switch", "cc_switch.status"),
            ("cc_switch", "cc_switch.rollups"),
        ),
    )
    registry.register_global(
        lambda: ProviderBinding(
            provider_id="omniroute", family_id="omniroute",
            cost_sources=(_performance_source(
                OmniRouteCostImporter(), "local_file"
            ),),
        ),
        lambda: supports(("omniroute", "omniroute.usage")),
    )
    def deepseek_openusage() -> ProviderBinding:
        openusage_adapter = OpenUsageDeepSeekAdapter(clock=clock)
        return ProviderBinding(
            provider_id="deepseek", family_id="deepseek",
            balance_sources=(
                _performance_source(DeepSeekBalanceAdapter(), "network"),
                _performance_source(openusage_adapter, "child_process"),
            ),
            cost_sources=(
                _performance_source(openusage_adapter, "child_process"),
            ),
        )

    registry.register_global(
        deepseek_openusage,
        lambda: supports(
            ("deepseek", "deepseek_official_api"),
            ("deepseek", "openusage"),
        ),
    )

    def minimax(config: MiniMaxConfig) -> ProviderBinding:
        endpoints = minimax_endpoints_for_site(config.site)
        client = BoundedHTTPClient(
            allowed_reserved_hosts={endpoints.host},
            allowed_redirect_hosts=set(),
        )
        usage_sources = (
            (_performance_source(
                MiniMaxBillingImporter(
                    config, shared_keychain(), client, clock
                ),
                "network",
            ),)
            if endpoints.billing is not None
            else ()
        )
        return ProviderBinding(
            provider_id=config.provider_id, family_id="minimax",
            quota_sources=(_quota_source(MiniMaxCodingPlanAdapter(
                config, shared_keychain(), client, clock
            ), "minimax.coding_plan", 20),),
            usage_sources=usage_sources,
        )

    def openai(config: OpenAIOrganizationConfig) -> ProviderBinding:
        importer = _performance_source(
            OpenAIOrganizationImporter(
                config, shared_keychain(), openai_client, clock
            ),
            "network",
        )
        return ProviderBinding(
            provider_id=config.provider_id, family_id="openai",
            quota_sources=(_quota_source(
                OpenAIOrganizationCardAdapter(
                    config, shared_keychain(), clock
                ),
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
                        config, shared_keychain(), moonshot_client, clock
                    ),
                    "network",
                ),
            ),
        )

    def daily_feed(config: DailyUsageFeedConfig) -> ProviderBinding:
        importer = _performance_source(
            DailyUsageFeedImporter(
                config, shared_keychain(), daily_feed_client, clock
            ),
            "network",
        )
        return ProviderBinding(
            provider_id=config.provider_id, family_id=config.family_id,
            quota_sources=(_quota_source(
                DailyUsageFeedCardAdapter(
                    config, shared_keychain(), clock
                ),
                "custom.daily", 20
            ),),
            usage_sources=(importer,),
        )

    def cost_feed(config: DailyCostFeedConfig) -> ProviderBinding:
        importer = _performance_source(
            DailyCostFeedImporter(
                config, shared_keychain(), daily_feed_client, clock
            ),
            "network",
        )
        return ProviderBinding(
            provider_id=config.provider_id, family_id=config.family_id,
            quota_sources=(_quota_source(
                DailyCostFeedCardAdapter(
                    config, shared_keychain(), clock
                ),
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
                config, shared_keychain(), client, clock
            ), "step_plan.quota", 20),),
        )

    def generic(config: GenericProviderConfig) -> ProviderBinding:
        return ProviderBinding(
            provider_id=config.provider_id,
            family_id=config.family_id or config.provider_id,
            quota_sources=(_quota_source(GenericHTTPSAdapter(
                config, shared_keychain(), generic_client, clock
            ), "generic.quota", 20),),
        )

    def minimax_available(config: MiniMaxConfig) -> bool:
        sources = [("minimax", "minimax_builtin_api")]
        if config.site == "china":
            sources.append(("minimax", "minimax_china_billing_web"))
        return supports(*sources)

    registry.register_config(
        MiniMaxConfig, minimax, minimax_available
    )
    registry.register_config(
        MoonshotConfig,
        moonshot,
        lambda _config: supports(("moonshot", "moonshot_official_api")),
    )
    registry.register_config(
        OpenAIOrganizationConfig,
        openai,
        lambda _config: supports(("openai", "openai_admin_api")),
    )
    registry.register_config(
        DailyUsageFeedConfig,
        daily_feed,
        lambda _config: supports_legacy_unmodeled(),
    )
    registry.register_config(
        DailyCostFeedConfig,
        cost_feed,
        lambda _config: supports_legacy_unmodeled(),
    )
    registry.register_config(
        StepPlanConfig,
        step_plan,
        lambda _config: supports(
            ("step_plan", "step_plan_browser_session"),
            ("step_plan", "step_plan_official_api"),
        ),
    )
    registry.register_config(
        GenericProviderConfig,
        generic,
        lambda _config: supports_legacy_unmodeled(),
    )
    return registry
