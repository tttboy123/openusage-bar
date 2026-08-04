from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from openusage_bar.aggregator import BoundedReadOnlyKeychain, build_headless_refresher
from openusage_bar.codex_daily import CodexLocalDailyImporter
from openusage_bar.codex_subscription import CodexSubscriptionAdapter
from openusage_bar.config import (
    DailyCostFeedConfig,
    DailyUsageFeedConfig,
    GenericProviderConfig,
    MiniMaxConfig,
    MoonshotConfig,
    OpenAIOrganizationConfig,
    StepPlanConfig,
)
from openusage_bar.cost_feed import DailyCostFeedImporter
from openusage_bar.daily_feed import DailyUsageFeedImporter
from openusage_bar.daily_history import OpenUsageDailyImporter
from openusage_bar.deepseek_openusage import OpenUsageDeepSeekAdapter
from openusage_bar.generic import GenericHTTPSAdapter
from openusage_bar.kiro import KiroQuotaAdapter
from openusage_bar.minimax import MiniMaxBillingImporter, MiniMaxCodingPlanAdapter
from openusage_bar.moonshot import MoonshotBalanceAdapter
from openusage_bar.openai_organization import OpenAIOrganizationImporter
from openusage_bar.openusage_adapter import OpenUsageDiscoveryAdapter
from openusage_bar.performance_timing import RefreshTimingRecorder
from openusage_bar.providers.builtins import default_registry
from openusage_bar.providers.contracts import (
    ProviderBinding,
    ProviderDescriptor,
    QuotaCollectionResult,
    QuotaFetchFailure,
    SourceAttribution,
)
from openusage_bar.providers.registry import AdapterRegistry, UnknownProviderConfig
from openusage_bar.step_plan import StepPlanAdapter


NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def descriptor(
    provider_id: str, family_id: str = "custom"
) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id=provider_id,
        family_id=family_id,
        display_name=provider_id.replace("-", " ").title(),
        category="api",
    )


class AdapterRegistryTests(unittest.TestCase):
    def registry(self) -> AdapterRegistry:
        return default_registry(
            clock=lambda: NOW, keychain=BoundedReadOnlyKeychain()
        )

    def configs(self):
        return [
            MiniMaxConfig("minimax-work", "MiniMax Work"),
            MoonshotConfig(
                "moonshot-work", "Kimi Work", site="china", account_ref="work"
            ),
            OpenAIOrganizationConfig("openai", "OpenAI Org"),
            DailyUsageFeedConfig(
                provider_id="glm-work", name="GLM Work", family_id="zai",
                endpoint="https://api.example.test/usage", method="GET",
                header_name="Authorization", auth_prefix="Bearer",
                items_path="data.items", date_path="day", model_path="model",
                input_tokens_path="input", output_tokens_path="output",
                total_tokens_path="total", since_parameter="from",
                until_parameter="to",
            ),
            DailyCostFeedConfig(
                provider_id="cost-work", name="Cost Work", family_id="openai",
                endpoint="https://api.example.test/cost", method="GET",
                header_name="Authorization", auth_prefix="Bearer",
                items_path="data.items", date_path="day",
                amount_path="amount", currency_path="currency",
                since_parameter="from", until_parameter="to",
            ),
            StepPlanConfig("step-work", "Step Plan", site="international"),
            GenericProviderConfig(
                provider_id="generic-work", name="Generic Work",
                endpoint="https://api.example.test/quota",
                header_name="Authorization", auth_prefix="Bearer",
                primary_path="data.remaining",
            ),
        ]

    def test_provider_descriptor_combines_stable_identity_with_attempt_source(self):
        descriptor = ProviderDescriptor(
            provider_id="step-work",
            family_id="step_plan",
            display_name="Step Plan Work",
            category="subscription",
        )

        instance = descriptor.observed(
            NOW,
            SourceAttribution(
                credential_source="step_plan_browser_session",
                source_kind="browser_session",
            ),
        )

        self.assertEqual(instance.provider_id, "step-work")
        self.assertEqual(instance.family_id, "step_plan")
        self.assertEqual(instance.display_name, "Step Plan Work")
        self.assertEqual(instance.category, "subscription")
        self.assertEqual(
            instance.credential_source, "step_plan_browser_session"
        )
        self.assertEqual(instance.source_kind, "browser_session")
        self.assertEqual(instance.observed_at, "2026-07-18T00:00:00.000000Z")

    def test_provider_binding_rejects_descriptor_identity_mismatch(self):
        descriptor = ProviderDescriptor(
            provider_id="different",
            family_id="step_plan",
            display_name="Step Plan",
            category="subscription",
        )

        with self.assertRaisesRegex(
            ValueError, "descriptor must match binding identity"
        ):
            ProviderBinding(
                provider_id="step-work",
                family_id="step_plan",
                descriptor=descriptor,
            )

    def test_collection_result_keeps_failure_separate_from_source_attribution(self):
        attribution = SourceAttribution(
            credential_source="step_plan_official_api",
            source_kind="official_api",
        )

        collection = QuotaCollectionResult(
            result=QuotaFetchFailure("auth_required"),
            attribution=attribution,
        )

        self.assertEqual(collection.result.error_code, "auth_required")
        self.assertEqual(collection.attribution, attribution)

    def test_source_attribution_rejects_noncanonical_public_values(self):
        with self.assertRaisesRegex(ValueError, "credential_source"):
            SourceAttribution(
                credential_source="not canonical",
                source_kind="official_api",
            )
        with self.assertRaisesRegex(ValueError, "source_kind"):
            SourceAttribution(
                credential_source="step_plan_official_api",
                source_kind="unknown_transport",
            )

    def test_current_configs_build_the_existing_adapter_and_importer_graph(self):
        bindings = {
            binding.provider_id: binding
            for binding in self.registry().build(self.configs())
        }

        expected = {
            "openusage": ((), (OpenUsageDailyImporter,), ()),
            "deepseek": ((), (), (OpenUsageDeepSeekAdapter,)),
            "kiro_cli": ((KiroQuotaAdapter,), (), ()),
            "codex": (
                (CodexSubscriptionAdapter,), (CodexLocalDailyImporter,), (),
            ),
            "cost-work": ((), (), (DailyCostFeedImporter,)),
            "minimax-work": (
                (MiniMaxCodingPlanAdapter,), (MiniMaxBillingImporter,), (),
            ),
            "moonshot-work": ((), (), ()),
            "openai": (
                (),
                (OpenAIOrganizationImporter,),
                (OpenAIOrganizationImporter,),
            ),
            "glm-work": (
                (), (DailyUsageFeedImporter,), (),
            ),
            "step-work": ((StepPlanAdapter,), (), ()),
            "generic-work": ((GenericHTTPSAdapter,), (), ()),
        }
        self.assertEqual(set(bindings), set(expected))
        for provider_id, groups in expected.items():
            binding = bindings[provider_id]
            self.assertEqual(tuple(map(type, binding.quota_sources)), groups[0])
            self.assertEqual(tuple(map(type, binding.usage_sources)), groups[1])
            self.assertEqual(tuple(map(type, binding.cost_sources)), groups[2])
        self.assertEqual(
            tuple(map(type, bindings["openusage"].discovery_sources)),
            (OpenUsageDiscoveryAdapter,),
        )
        self.assertEqual(
            tuple(map(type, bindings["moonshot-work"].balance_sources)),
            (MoonshotBalanceAdapter,),
        )
        self.assertEqual(
            tuple(map(type, bindings["deepseek"].balance_sources)),
            (OpenUsageDeepSeekAdapter,),
        )
        self.assertIs(
            bindings["openai"].usage_sources[0],
            bindings["openai"].cost_sources[0],
        )
        self.assertIs(
            bindings["minimax-work"].quota_sources[0].client,
            bindings["minimax-work"].usage_sources[0].client,
        )

    def test_minimax_sites_use_isolated_clients_and_only_verified_usage_sources(self):
        bindings = {
            binding.provider_id: binding
            for binding in self.registry().build(
                [
                    MiniMaxConfig(
                        "minimax-cn", "MiniMax China", site="china"
                    ),
                    MiniMaxConfig(
                        "minimax-global",
                        "MiniMax Global",
                        site="international",
                    ),
                ]
            )
        }

        china = bindings["minimax-cn"]
        international = bindings["minimax-global"]
        self.assertEqual(
            tuple(map(type, china.usage_sources)),
            (MiniMaxBillingImporter,),
        )
        self.assertEqual(international.usage_sources, ())
        self.assertIsNot(
            china.quota_sources[0].client,
            international.quota_sources[0].client,
        )
        self.assertEqual(
            china.quota_sources[0].client.allowed_reserved_hosts,
            frozenset({"www.minimaxi.com"}),
        )
        self.assertEqual(
            international.quota_sources[0].client.allowed_reserved_hosts,
            frozenset({"www.minimax.io"}),
        )
        self.assertEqual(
            china.quota_sources[0].client.allowed_redirect_hosts,
            frozenset(),
        )
        self.assertEqual(
            international.quota_sources[0].client.allowed_redirect_hosts,
            frozenset(),
        )

    def test_step_plan_reuses_the_shared_bounded_read_only_keychain(self):
        keychain = BoundedReadOnlyKeychain()
        binding = next(
            item
            for item in default_registry(
                clock=lambda: NOW, keychain=keychain
            ).build([StepPlanConfig("step-work", "Step Plan")])
            if item.provider_id == "step-work"
        )

        self.assertIs(binding.quota_sources[0].keychain, keychain)

    def test_config_order_does_not_change_stable_bindings(self):
        forward = self.registry().build(self.configs())
        reverse = self.registry().build(reversed(self.configs()))

        def graph(bindings):
            return [(
                binding.provider_id,
                binding.family_id,
                tuple(type(source).__name__ for source in binding.quota_sources),
                tuple(type(source).__name__ for source in binding.usage_sources),
                tuple(type(source).__name__ for source in binding.cost_sources),
            ) for binding in bindings]

        self.assertEqual(graph(forward), graph(reverse))
        self.assertEqual(
            [binding.provider_id for binding in forward],
            sorted(binding.provider_id for binding in forward),
        )

    def test_openusage_discovery_is_separate_from_direct_quota_sources(self):
        bindings = self.registry().build(self.configs())
        openusage = next(
            binding for binding in bindings if binding.provider_id == "openusage"
        )
        self.assertEqual(openusage.quota_sources, ())
        self.assertIsInstance(
            openusage.discovery_sources[0], OpenUsageDiscoveryAdapter
        )

        with patch(
            "openusage_bar.config.ProviderConfigStore.load",
            return_value=self.configs(),
        ):
            refresher = build_headless_refresher(Mock())
        self.assertFalse(hasattr(refresher, "aggregator"))
        self.assertIsInstance(
            refresher.discovery_sources[0][2], OpenUsageDiscoveryAdapter
        )
        direct_types = [
            type(adapter) for _descriptor, _source_id, adapter
            in refresher.quota_sources
        ]
        self.assertIn(CodexSubscriptionAdapter, direct_types)
        self.assertIn(KiroQuotaAdapter, direct_types)

    def test_builtin_sources_declare_privacy_safe_performance_classes(self):
        bindings = {
            binding.provider_id: binding
            for binding in self.registry().build(self.configs())
        }

        self.assertEqual(
            bindings["openusage"].discovery_sources[0].performance_source_class,
            "child_process",
        )
        self.assertEqual(
            bindings["openusage"].usage_sources[0].performance_source_class,
            "child_process",
        )
        self.assertEqual(
            bindings["codex"].quota_sources[0].performance_source_class,
            "local_file",
        )
        self.assertEqual(
            bindings["codex"].usage_sources[0].performance_source_class,
            "local_file",
        )
        for provider_id in (
            "kiro_cli",
            "minimax-work",
            "moonshot-work",
            "openai",
            "glm-work",
            "cost-work",
            "step-work",
            "generic-work",
        ):
            binding = bindings[provider_id]
            sources = (
                *binding.balance_sources,
                *binding.quota_sources,
                *binding.usage_sources,
                *binding.cost_sources,
            )
            self.assertTrue(sources)
            self.assertTrue(
                all(
                    source.performance_source_class == "network"
                    for source in sources
                )
            )

    def test_headless_refresher_shares_one_timing_recorder(self):
        recorder = RefreshTimingRecorder()
        with patch(
            "openusage_bar.config.ProviderConfigStore.load", return_value=[]
        ):
            refresher = build_headless_refresher(
                Mock(), timing_recorder=recorder
            )

        self.assertIs(refresher.timing_recorder, recorder)
        self.assertIs(refresher.collector.timing_recorder, recorder)
        self.assertEqual(
            refresher.performance_timing_snapshot(),
            {
                "schemaVersion": 1,
                "scope": "source-class",
                "classes": [],
            },
        )

    def test_duplicate_source_ids_and_provider_ids_are_rejected(self):
        class Source:
            source_id = "same.source"
            def fetch(self):  # pragma: no cover - structural fixture only
                raise AssertionError

        registry = AdapterRegistry()
        registry.register_global(lambda: ProviderBinding(
            provider_id="duplicate-sources", family_id="custom",
            descriptor=descriptor("duplicate-sources"),
            quota_sources=(Source(), Source()),
        ))
        with self.assertRaisesRegex(ValueError, "duplicate quota source IDs"):
            registry.build([])

        registry = AdapterRegistry()
        registry.register_global(lambda: ProviderBinding(
            "same", "one", descriptor("same", "one")
        ))
        registry.register_global(lambda: ProviderBinding(
            "same", "two", descriptor("same", "two")
        ))
        with self.assertRaisesRegex(ValueError, "duplicate provider IDs"):
            registry.build([])

    def test_sources_are_sorted_by_priority_then_stable_source_id(self):
        class Source:
            def __init__(self, source_id, priority):
                self.source_id = source_id
                self.source_priority = priority
            def fetch(self):  # pragma: no cover - structural fixture only
                raise AssertionError

        registry = AdapterRegistry()
        registry.register_global(lambda: ProviderBinding(
            provider_id="ordered", family_id="custom",
            descriptor=descriptor("ordered"),
            quota_sources=(
                Source("z", 20), Source("b", 10), Source("a", 10),
            ),
        ))
        binding = registry.build([])[0]
        self.assertEqual(
            [source.source_id for source in binding.quota_sources],
            ["a", "b", "z"],
        )

    def test_unknown_and_subclassed_config_types_fail_closed(self):
        @dataclass(frozen=True)
        class UnknownConfig:
            provider_id: str

        with self.assertRaisesRegex(UnknownProviderConfig, "not registered"):
            self.registry().build([UnknownConfig("unknown")])

        class MiniMaxSubclass(MiniMaxConfig):
            pass

        with self.assertRaisesRegex(UnknownProviderConfig, "not registered"):
            self.registry().build([MiniMaxSubclass("minimax", "MiniMax")])


if __name__ == "__main__":
    unittest.main()
