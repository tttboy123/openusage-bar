from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ..codex_subscription import CodexSubscriptionAdapter
from ..config import (
    DailyUsageFeedConfig,
    GenericProviderConfig,
    MiniMaxConfig,
    OpenAIOrganizationConfig,
    StepPlanConfig,
)
from ..daily_feed import DailyUsageFeedCardAdapter, DailyUsageFeedImporter
from ..daily_history import OpenUsageDailyImporter
from ..generic import GenericHTTPSAdapter
from ..kiro import KiroQuotaAdapter
from ..minimax import MiniMaxBillingImporter, MiniMaxCodingPlanAdapter
from ..network import BoundedHTTPClient
from ..openai_organization import (
    OpenAIOrganizationCardAdapter,
    OpenAIOrganizationImporter,
)
from ..openusage_adapter import OpenUsageAdapter
from ..step_plan import StepPlanAdapter, endpoints_for_site
from .contracts import ProviderBinding
from .registry import AdapterRegistry


def _quota_source(source: object, source_id: str, priority: int) -> object:
    # Existing adapters are intentionally left behavior-compatible in Task 1;
    # registry metadata makes their cross-Provider merge order explicit.
    source.source_id = source_id
    source.source_priority = priority
    return source


def default_registry(
    *, clock: Callable[[], datetime], keychain: object
) -> AdapterRegistry:
    """Build the production registry with shared, bounded dependencies."""

    registry = AdapterRegistry()
    generic_client = BoundedHTTPClient()
    daily_feed_client = BoundedHTTPClient(allowed_redirect_hosts=set())
    minimax_client = BoundedHTTPClient(
        allowed_reserved_hosts={"www.minimaxi.com"},
        allowed_redirect_hosts=set(),
    )
    openai_client = BoundedHTTPClient(allowed_redirect_hosts=set())
    step_plan_keychain: object | None = None

    registry.register_global(lambda: ProviderBinding(
        provider_id="openusage", family_id="openusage",
        quota_sources=(_quota_source(
            OpenUsageAdapter(clock), "openusage.cards", 10
        ),),
        usage_sources=(OpenUsageDailyImporter(clock=clock),),
    ))
    registry.register_global(lambda: ProviderBinding(
        provider_id="kiro_cli", family_id="kiro_cli",
        quota_sources=(_quota_source(
            KiroQuotaAdapter(clock=clock), "kiro.quota", 20
        ),),
    ))
    registry.register_global(lambda: ProviderBinding(
        provider_id="codex", family_id="codex",
        quota_sources=(_quota_source(
            CodexSubscriptionAdapter(clock=clock), "codex.quota", 20
        ),),
    ))

    def minimax(config: MiniMaxConfig) -> ProviderBinding:
        importer = MiniMaxBillingImporter(config, keychain, minimax_client, clock)
        return ProviderBinding(
            provider_id=config.provider_id, family_id="minimax",
            quota_sources=(_quota_source(MiniMaxCodingPlanAdapter(
                config, keychain, minimax_client, clock
            ), "minimax.quota", 20),),
            usage_sources=(importer,),
        )

    def openai(config: OpenAIOrganizationConfig) -> ProviderBinding:
        importer = OpenAIOrganizationImporter(config, keychain, openai_client, clock)
        return ProviderBinding(
            provider_id=config.provider_id, family_id="openai",
            quota_sources=(_quota_source(
                OpenAIOrganizationCardAdapter(config, keychain, clock),
                "openai.organization", 20
            ),),
            usage_sources=(importer,), cost_sources=(importer,),
        )

    def daily_feed(config: DailyUsageFeedConfig) -> ProviderBinding:
        importer = DailyUsageFeedImporter(config, keychain, daily_feed_client, clock)
        return ProviderBinding(
            provider_id=config.provider_id, family_id=config.family_id,
            quota_sources=(_quota_source(
                DailyUsageFeedCardAdapter(config, keychain, clock),
                "custom.daily", 20
            ),),
            usage_sources=(importer,),
        )

    def step_plan(config: StepPlanConfig) -> ProviderBinding:
        nonlocal step_plan_keychain
        if step_plan_keychain is None:
            try:
                # Resolve lazily so unavailable Security/PyObjC support keeps the
                # established read-only fallback and remains test-injectable.
                from ..keychain import MacOSKeychain
                step_plan_keychain = MacOSKeychain()
            except (ImportError, OSError, RuntimeError):
                step_plan_keychain = keychain
        endpoints = endpoints_for_site(config.site)
        client = BoundedHTTPClient(
            allowed_reserved_hosts={endpoints.api_host, endpoints.platform_host},
            allowed_redirect_hosts=set(),
        )
        return ProviderBinding(
            provider_id=config.provider_id, family_id="step_plan",
            quota_sources=(_quota_source(StepPlanAdapter(
                config, step_plan_keychain, client, clock
            ), "step_plan.quota", 20),),
        )

    def generic(config: GenericProviderConfig) -> ProviderBinding:
        return ProviderBinding(
            provider_id=config.provider_id, family_id=config.provider_id,
            quota_sources=(_quota_source(GenericHTTPSAdapter(
                config, keychain, generic_client, clock
            ), "generic.quota", 20),),
        )

    registry.register_config(MiniMaxConfig, minimax)
    registry.register_config(OpenAIOrganizationConfig, openai)
    registry.register_config(DailyUsageFeedConfig, daily_feed)
    registry.register_config(StepPlanConfig, step_plan)
    registry.register_config(GenericProviderConfig, generic)
    return registry
