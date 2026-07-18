from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from openusage_bar.aggregator import BoundedReadOnlyKeychain, build_headless_refresher
from openusage_bar.codex_subscription import CodexSubscriptionAdapter
from openusage_bar.codex_daily import CodexLocalDailyImporter
from openusage_bar.config import (
    DailyCostFeedConfig,
    DailyUsageFeedConfig,
    GenericProviderConfig,
    MiniMaxConfig,
    OpenAIOrganizationConfig,
    StepPlanConfig,
)
from openusage_bar.cost_feed import DailyCostFeedCardAdapter, DailyCostFeedImporter
from openusage_bar.daily_feed import DailyUsageFeedCardAdapter, DailyUsageFeedImporter
from openusage_bar.daily_history import OpenUsageDailyImporter
from openusage_bar.generic import GenericHTTPSAdapter
from openusage_bar.kiro import KiroQuotaAdapter
from openusage_bar.minimax import MiniMaxBillingImporter, MiniMaxCodingPlanAdapter
from openusage_bar.openai_organization import (
    OpenAIOrganizationCardAdapter,
    OpenAIOrganizationImporter,
)
from openusage_bar.openusage_adapter import OpenUsageAdapter
from openusage_bar.providers.builtins import default_registry
from openusage_bar.providers.contracts import ProviderBinding
from openusage_bar.providers.registry import AdapterRegistry, UnknownProviderConfig
from openusage_bar.step_plan import StepPlanAdapter


NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


class AdapterRegistryTests(unittest.TestCase):
    def registry(self) -> AdapterRegistry:
        return default_registry(
            clock=lambda: NOW, keychain=BoundedReadOnlyKeychain()
        )

    def configs(self):
        return [
            MiniMaxConfig("minimax-work", "MiniMax Work"),
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

    def test_current_configs_build_the_existing_adapter_and_importer_graph(self):
        bindings = {
            binding.provider_id: binding
            for binding in self.registry().build(self.configs())
        }

        expected = {
            "openusage": ((OpenUsageAdapter,), (OpenUsageDailyImporter,), ()),
            "kiro_cli": ((KiroQuotaAdapter,), (), ()),
            "codex": (
                (CodexSubscriptionAdapter,), (CodexLocalDailyImporter,), (),
            ),
            "cost-work": ((DailyCostFeedCardAdapter,), (), (DailyCostFeedImporter,)),
            "minimax-work": (
                (MiniMaxCodingPlanAdapter,), (MiniMaxBillingImporter,), (),
            ),
            "openai": (
                (OpenAIOrganizationCardAdapter,),
                (OpenAIOrganizationImporter,),
                (OpenAIOrganizationImporter,),
            ),
            "glm-work": (
                (DailyUsageFeedCardAdapter,), (DailyUsageFeedImporter,), (),
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
        self.assertIs(
            bindings["openai"].usage_sources[0],
            bindings["openai"].cost_sources[0],
        )
        self.assertIs(
            bindings["minimax-work"].quota_sources[0].client,
            bindings["minimax-work"].usage_sources[0].client,
        )

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

    def test_openusage_base_precedes_direct_quota_overrides(self):
        bindings = self.registry().build(self.configs())
        ordered = sorted(
            (
                source.source_priority, binding.provider_id, type(source).__name__
            )
            for binding in bindings for source in binding.quota_sources
        )
        self.assertEqual(ordered[0][2], "OpenUsageAdapter")
        self.assertTrue(all(priority > ordered[0][0] for priority, _, _ in ordered[1:]))

        with patch(
            "openusage_bar.config.ProviderConfigStore.load",
            return_value=self.configs(),
        ):
            refresher = build_headless_refresher(Mock())
        runtime_types = [type(adapter) for adapter in refresher.aggregator.adapters]
        self.assertIs(runtime_types[0], OpenUsageAdapter)
        self.assertGreater(runtime_types.index(CodexSubscriptionAdapter), 0)
        self.assertGreater(runtime_types.index(KiroQuotaAdapter), 0)

    def test_duplicate_source_ids_and_provider_ids_are_rejected(self):
        class Source:
            source_id = "same.source"
            def fetch(self):  # pragma: no cover - structural fixture only
                raise AssertionError

        registry = AdapterRegistry()
        registry.register_global(lambda: ProviderBinding(
            provider_id="duplicate-sources", family_id="custom",
            quota_sources=(Source(), Source()),
        ))
        with self.assertRaisesRegex(ValueError, "duplicate quota source IDs"):
            registry.build([])

        registry = AdapterRegistry()
        registry.register_global(lambda: ProviderBinding("same", "one"))
        registry.register_global(lambda: ProviderBinding("same", "two"))
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
