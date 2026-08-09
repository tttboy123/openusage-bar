from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from openusage_bar.capabilities import registry as provider_capabilities
from openusage_bar.config import GenericProviderConfig, OpenAIOrganizationConfig
from openusage_bar.activity_store import ActivityStore
from openusage_bar.aggregator import build_headless_refresher
from openusage_bar.local_api import LocalAPIRouter
from openusage_bar.provider_catalog import ObserverPlatformResolver, catalog
from openusage_bar.providers.builtins import default_registry
from openusage_bar.providers.registry import UnknownProviderConfig


class _StatusOnlyQuery:
    def source_status(self) -> dict[str, object]:
        return {
            "schemaVersion": "1.0",
            "dataRevision": 0,
            "generatedAt": "2026-08-09T00:00:00Z",
            "sources": [],
        }


class ObserverPlatformResolverTests(unittest.TestCase):
    def test_runtime_platform_mapping_is_closed_and_does_not_guess(self) -> None:
        expected = {
            "darwin": "macos",
            "win32": "windows",
            "linux": "linux",
            "linux2": "linux",
        }
        for runtime_platform, operating_system in expected.items():
            with self.subTest(runtime_platform=runtime_platform):
                resolver = ObserverPlatformResolver(
                    catalog, runtime_platform=runtime_platform
                )
                self.assertEqual(resolver.operating_system, operating_system)

        for runtime_platform in ("freebsd", "MACOS", "", None, True, 1):
            with self.subTest(runtime_platform=type(runtime_platform).__name__):
                resolver = ObserverPlatformResolver(
                    catalog, runtime_platform=runtime_platform
                )
                self.assertIsNone(resolver.operating_system)

    def test_summary_distinguishes_verified_zero_from_unknown(self) -> None:
        macos = ObserverPlatformResolver(catalog, runtime_platform="darwin")
        windows = ObserverPlatformResolver(catalog, runtime_platform="win32")
        unknown = ObserverPlatformResolver(catalog, runtime_platform="freebsd")

        self.assertEqual(
            (
                macos.summary.support,
                macos.summary.supported_source_count,
                macos.summary.total_source_count,
                macos.summary.reason_code,
            ),
            ("supported", 49, 49, "supported_sources_available"),
        )
        self.assertEqual(
            (
                windows.summary.support,
                windows.summary.supported_source_count,
                windows.summary.total_source_count,
                windows.summary.reason_code,
            ),
            ("unsupported", 0, 49, "source_level_evidence_unverified"),
        )
        self.assertEqual(
            (
                unknown.summary.support,
                unknown.summary.supported_source_count,
                unknown.summary.total_source_count,
                unknown.summary.reason_code,
            ),
            ("unknown", None, 49, "runtime_platform_unknown"),
        )

    def test_source_records_share_the_summary_reason_vocabulary(self) -> None:
        windows = ObserverPlatformResolver(catalog, runtime_platform="win32")
        records = windows.source_capabilities

        self.assertEqual(len(records), 49)
        self.assertTrue(all(not record.supported for record in records))
        self.assertEqual(
            {record.reason_code for record in records},
            {"source_level_evidence_unverified"},
        )
        self.assertFalse(windows.supports_source("codex", "codex_local_log"))
        self.assertFalse(windows.supports_any_source_id("openusage"))

    def test_shared_source_adapter_requires_every_catalog_use_to_be_supported(
        self,
    ) -> None:
        changed = False
        families = []
        for family in catalog.families:
            sources = []
            for source in family.sources:
                if source.source_id == "openusage" and not changed:
                    sources.append(
                        replace(source, operating_systems=frozenset({"linux"}))
                    )
                    changed = True
                else:
                    sources.append(source)
            families.append(replace(family, sources=tuple(sources)))
        self.assertTrue(changed)
        mixed_catalog = replace(catalog, families=tuple(families))
        resolver = ObserverPlatformResolver(
            mixed_catalog, runtime_platform="darwin"
        )

        self.assertTrue(resolver.supports_any_source_id("openusage"))
        self.assertFalse(resolver.supports_all_source_id("openusage"))

        with patch(
            "openusage_bar.providers.builtins.OpenUsageAdapter",
            side_effect=AssertionError(
                "partially verified shared adapter was constructed"
            ),
        ):
            bindings = default_registry(
                clock=lambda: datetime.now(timezone.utc),
                keychain=object(),
                observer_platform=resolver,
            ).build([])
        self.assertNotIn(
            "openusage", {binding.provider_id for binding in bindings}
        )

    def test_unmodeled_legacy_sources_never_inherit_portable_cli_evidence(
        self,
    ) -> None:
        families = tuple(
            replace(
                family,
                sources=tuple(
                    replace(
                        source,
                        operating_systems=frozenset(
                            {*source.operating_systems, "linux"}
                        ),
                    )
                    if source.source_id == "openusage"
                    else source
                    for source in family.sources
                ),
            )
            for family in catalog.families
        )
        portable_cli_catalog = replace(catalog, families=families)
        resolver = ObserverPlatformResolver(
            portable_cli_catalog, runtime_platform="linux"
        )
        self.assertTrue(resolver.supports_all_source_id("openusage"))
        self.assertFalse(resolver.supports_legacy_unmodeled_sources)

        config = GenericProviderConfig(
            provider_id="custom-api",
            name="Custom API",
            endpoint="https://example.invalid/quota",
            header_name="Authorization",
            auth_prefix="Bearer ",
            primary_path="remaining",
        )
        bindings = default_registry(
            clock=lambda: datetime.now(timezone.utc),
            keychain=object(),
            observer_platform=resolver,
        ).build([config])
        self.assertEqual(
            [binding.provider_id for binding in bindings],
            ["openusage"],
        )

    def test_resolver_is_pure_and_never_probes_the_host(self) -> None:
        forbidden = AssertionError("resolver touched the host")
        with (
            patch("builtins.open", side_effect=forbidden),
            patch("os.getenv", side_effect=forbidden),
            patch("socket.socket", side_effect=forbidden),
            patch("subprocess.Popen", side_effect=forbidden),
        ):
            resolver = ObserverPlatformResolver(
                catalog, runtime_platform="linux"
            )
            self.assertEqual(resolver.summary.supported_source_count, 0)
            self.assertEqual(len(resolver.source_capabilities), 49)


class ObserverPlatformIntegrationContractTests(unittest.TestCase):
    def test_local_api_projects_one_summary_and_per_source_support(self) -> None:
        resolver = ObserverPlatformResolver(catalog, runtime_platform="win32")
        router = LocalAPIRouter(
            _StatusOnlyQuery(),
            provider_registry=provider_capabilities,
            observer_platform=resolver,
        )

        payload = router._payload("/v1/capabilities", {})
        self.assertEqual(
            payload["observerPlatform"],
            {
                "operatingSystem": "windows",
                "support": "unsupported",
                "supportedSourceCount": 0,
                "totalSourceCount": 49,
                "reasonCode": "source_level_evidence_unverified",
            },
        )
        projected = [
            source["platformSupport"]
            for provider in payload["providers"]
            for source in provider["sources"]
        ]
        self.assertEqual(len(projected), 49)
        self.assertEqual(
            projected,
            [
                {
                    "state": "unsupported",
                    "reasonCode": "source_level_evidence_unverified",
                }
            ]
            * 49,
        )

    def test_unknown_runtime_never_serializes_supported_count_as_zero(self) -> None:
        resolver = ObserverPlatformResolver(
            catalog, runtime_platform="unrecognized"
        )
        router = LocalAPIRouter(
            _StatusOnlyQuery(),
            provider_registry=provider_capabilities,
            observer_platform=resolver,
        )

        payload = router._payload("/v1/capabilities", {})
        self.assertEqual(payload["observerPlatform"]["support"], "unknown")
        self.assertIsNone(
            payload["observerPlatform"]["supportedSourceCount"]
        )
        self.assertEqual(
            {
                source["platformSupport"]["state"]
                for provider in payload["providers"]
                for source in provider["sources"]
            },
            {"unknown"},
        )

    def test_adapter_factories_are_not_constructed_without_source_evidence(
        self,
    ) -> None:
        resolver = ObserverPlatformResolver(catalog, runtime_platform="linux")
        with patch(
            "openusage_bar.providers.builtins.OpenUsageAdapter",
            side_effect=AssertionError("unsupported adapter was constructed"),
        ):
            bindings = default_registry(
                clock=lambda: datetime.now(timezone.utc),
                keychain=object(),
                observer_platform=resolver,
            ).build([])
        self.assertEqual(bindings, ())

    def test_configured_adapter_factory_is_gated_before_construction(self) -> None:
        resolver = ObserverPlatformResolver(catalog, runtime_platform="win32")
        config = OpenAIOrganizationConfig(
            provider_id="openai-work",
            name="OpenAI work",
        )
        with (
            patch(
                "openusage_bar.providers.builtins.OpenAIOrganizationImporter",
                side_effect=AssertionError(
                    "unsupported importer was constructed"
                ),
            ),
            patch(
                "openusage_bar.providers.builtins.OpenAIOrganizationCardAdapter",
                side_effect=AssertionError(
                    "unsupported adapter was constructed"
                ),
            ),
        ):
            bindings = default_registry(
                clock=lambda: datetime.now(timezone.utc),
                keychain=object(),
                observer_platform=resolver,
            ).build([config])
        self.assertEqual(bindings, ())

    def test_macos_adapter_registry_preserves_existing_global_bindings(
        self,
    ) -> None:
        resolver = ObserverPlatformResolver(catalog, runtime_platform="darwin")
        bindings = default_registry(
            clock=lambda: datetime.now(timezone.utc),
            keychain=object(),
            observer_platform=resolver,
        ).build([])
        self.assertEqual(
            [binding.provider_id for binding in bindings],
            [
                "cc_switch",
                "claude_code",
                "codex",
                "deepseek",
                "kiro_cli",
                "omniroute",
                "openusage",
            ],
        )

    def test_platform_gating_does_not_hide_unknown_config_types(self) -> None:
        resolver = ObserverPlatformResolver(catalog, runtime_platform="linux")
        registry = default_registry(
            clock=lambda: datetime.now(timezone.utc),
            keychain=object(),
            observer_platform=resolver,
        )
        with self.assertRaises(UnknownProviderConfig):
            registry.build([object()])

    def test_headless_observer_starts_with_an_empty_verified_source_set(
        self,
    ) -> None:
        resolver = ObserverPlatformResolver(catalog, runtime_platform="linux")
        store = ActivityStore(":memory:")
        self.addCleanup(store.close)
        with (
            patch(
                "openusage_bar.aggregator.default_keychain",
                side_effect=AssertionError(
                    "zero-source Observer touched the credential backend"
                ),
            ),
            patch(
                "openusage_bar.config.ProviderConfigStore.load",
                return_value=[],
            ),
        ):
            refresher = build_headless_refresher(
                store,
                observer_platform=resolver,
            )

        self.assertEqual(refresher.quota_sources, ())
        self.assertEqual(refresher.balance_sources, ())
        self.assertEqual(refresher.eager_usage_provider_ids, ())
        self.assertIsNone(refresher.collector.importer)


if __name__ == "__main__":
    unittest.main()
