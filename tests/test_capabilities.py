import unittest
from dataclasses import FrozenInstanceError, replace

from openusage_bar.capabilities import (
    CapabilityState,
    CredentialType,
    MetricFamily,
    ObservationState,
    OperatingSystem,
    ProviderDescriptor,
    ProviderCapabilities,
    ProviderRegistry,
    QuotaWindow,
    QuotaWindowCapability,
    SourceCapability,
    SourceKind,
    SourceProvenance,
    SourceStability,
    registry,
    state_from_card,
)
from openusage_bar.models import ProviderStatus


class ProviderCapabilityTests(unittest.TestCase):
    @staticmethod
    def _unknown_capabilities():
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

    @staticmethod
    def _local_source(source_id="source"):
        return SourceCapability(
            source_id,
            SourceKind.LOCAL_LOG,
            12,
            300,
            CredentialType.LOCAL,
            frozenset({OperatingSystem.MACOS}),
            SourceStability.STABLE,
            SourceProvenance.PROVIDER_LOCAL,
        )

    @classmethod
    def _descriptor(cls):
        return ProviderDescriptor(
            "demo",
            "Demo",
            "api",
            frozenset({MetricFamily.TOKEN_ACTIVITY}),
            frozenset({"cn"}),
            False,
            (cls._local_source(),),
            cls._unknown_capabilities(),
        )

    def test_registry_is_generated_from_all_37_catalog_families(self):
        self.assertEqual(len(registry.descriptors), 37)
        self.assertEqual(
            {descriptor.category for descriptor in registry.descriptors},
            {"api", "subscription", "local_tool"},
        )

    def test_registry_exposes_sorted_immutable_descriptor_tuple(self):
        descriptors = registry.descriptors

        self.assertIsInstance(descriptors, tuple)
        self.assertEqual(
            [descriptor.provider_id for descriptor in descriptors],
            sorted(descriptor.provider_id for descriptor in descriptors),
        )
        with self.assertRaises(AttributeError):
            descriptors.append(registry.require("codex"))

    def test_descriptor_declares_capabilities_and_ordered_sources(self):
        descriptor = registry.require("minimax")

        self.assertEqual(
            descriptor.metric_families,
            frozenset({MetricFamily.SUBSCRIPTION_QUOTA}),
        )
        self.assertEqual(descriptor.regions, frozenset({"cn", "international"}))
        self.assertEqual(
            [source.kind for source in descriptor.sources],
            [SourceKind.BUILTIN_API, SourceKind.OPENUSAGE],
        )
        self.assertEqual(descriptor.category, "subscription")
        self.assertEqual(
            [source.credential_type for source in descriptor.sources],
            [CredentialType.API_KEY, CredentialType.PROVIDER_OWNED],
        )
        self.assertEqual(
            descriptor.capabilities.quota_windows.values,
            (QuotaWindow.FIVE_HOUR, QuotaWindow.WEEKLY),
        )
        self.assertIs(
            descriptor.capabilities.reset_timestamps, CapabilityState.SUPPORTED
        )

    def test_registry_forwards_all_family_and_source_capability_fields(self):
        codex = registry.require("codex")
        self.assertIs(codex.capabilities.token_history, CapabilityState.SUPPORTED)
        self.assertIs(codex.capabilities.model_breakdown, CapabilityState.SUPPORTED)
        self.assertIs(codex.capabilities.billing, CapabilityState.UNKNOWN)
        self.assertEqual(
            codex.capabilities.quota_windows.values,
            (QuotaWindow.FIVE_HOUR, QuotaWindow.WEEKLY),
        )
        self.assertEqual(
            codex.sources[0].operating_systems,
            frozenset({OperatingSystem.MACOS}),
        )
        self.assertIs(codex.sources[0].stability, SourceStability.STABLE)
        self.assertIs(
            codex.sources[0].provenance, SourceProvenance.PROVIDER_LOCAL
        )
        self.assertIs(codex.sources[1].stability, SourceStability.PINNED)
        self.assertIs(
            codex.sources[1].provenance, SourceProvenance.OPENUSAGE_UPSTREAM
        )

    def test_unknown_provider_gets_dynamic_openusage_descriptor(self):
        descriptor = registry.resolve("future-provider", "Future Provider")

        self.assertEqual(descriptor.provider_id, "future-provider")
        self.assertEqual(descriptor.display_name, "Future Provider")
        self.assertEqual(
            descriptor.metric_families,
            frozenset({MetricFamily.TOKEN_ACTIVITY, MetricFamily.BILLING}),
        )
        self.assertNotIn(MetricFamily.SUBSCRIPTION_QUOTA, descriptor.metric_families)
        self.assertNotIn(MetricFamily.OPERATIONAL, descriptor.metric_families)
        self.assertIs(descriptor.sources[0].kind, SourceKind.OPENUSAGE)
        self.assertIs(
            descriptor.sources[0].credential_type, CredentialType.PROVIDER_OWNED
        )
        self.assertEqual(
            descriptor.capabilities,
            self._unknown_capabilities(),
        )
        self.assertEqual(
            descriptor.sources[0].operating_systems,
            frozenset({OperatingSystem.MACOS}),
        )
        self.assertIs(descriptor.sources[0].stability, SourceStability.PINNED)
        self.assertIs(
            descriptor.sources[0].provenance,
            SourceProvenance.OPENUSAGE_UPSTREAM,
        )

    def test_unknown_fallback_never_guesses_capabilities_from_name(self):
        expected = self._unknown_capabilities()
        for provider_id in (
            "future-codex", "minimax-next", "kiro_cli_beta", "step_plan_plus"
        ):
            with self.subTest(provider=provider_id):
                self.assertEqual(
                    registry.resolve(provider_id, provider_id).capabilities,
                    expected,
                )

    def test_codex_declares_local_log_before_openusage(self):
        descriptor = registry.require("codex")

        self.assertEqual(
            [source.kind for source in descriptor.sources],
            [SourceKind.LOCAL_LOG, SourceKind.OPENUSAGE],
        )
        self.assertIsNone(descriptor.sources[0].credential_scope)

    def test_openai_declares_official_organization_usage_and_costs(self):
        descriptor = registry.require("openai")

        self.assertEqual(
            descriptor.metric_families,
            frozenset({MetricFamily.TOKEN_ACTIVITY, MetricFamily.BILLING}),
        )
        self.assertEqual(
            [source.source_id for source in descriptor.sources],
            ["openai_admin_api", "openusage"],
        )
        self.assertIs(descriptor.sources[0].kind, SourceKind.OFFICIAL_API)
        self.assertIs(descriptor.sources[0].credential_type, CredentialType.API_KEY)
        self.assertEqual(
            descriptor.sources[0].credential_scope, "openai_admin_api_key"
        )
        self.assertIs(
            descriptor.capabilities.token_history, CapabilityState.SUPPORTED
        )
        self.assertIs(
            descriptor.capabilities.model_breakdown, CapabilityState.SUPPORTED
        )
        self.assertIs(descriptor.capabilities.billing, CapabilityState.SUPPORTED)
        self.assertIs(descriptor.capabilities.cost, CapabilityState.SUPPORTED)

    def test_stepfun_declares_session_then_official_api(self):
        descriptor = registry.require("step_plan")

        self.assertEqual(descriptor.regions, frozenset({"cn", "international"}))
        self.assertEqual(
            descriptor.metric_families,
            frozenset(
                {MetricFamily.SUBSCRIPTION_QUOTA, MetricFamily.BILLING}
            ),
        )
        self.assertEqual(
            [source.kind for source in descriptor.sources],
            [SourceKind.BROWSER_SESSION, SourceKind.OFFICIAL_API],
        )
        self.assertEqual(
            [source.credential_scope for source in descriptor.sources],
            ["step_plan_session", "step_plan_api_key"],
        )

    def test_kiro_declares_keychain_then_official_api_and_openusage(self):
        descriptor = registry.require("kiro_cli")

        self.assertEqual(
            [source.kind for source in descriptor.sources],
            [SourceKind.KEYCHAIN, SourceKind.OFFICIAL_API, SourceKind.OPENUSAGE],
        )
        self.assertEqual(
            [source.credential_scope for source in descriptor.sources],
            ["kiro", "kiro", None],
        )

    def test_canonical_state_does_not_turn_unknown_into_zero(self):
        self.assertIs(
            state_from_card(ProviderStatus.UNKNOWN, stale=False),
            ObservationState.TEMPORARILY_UNAVAILABLE,
        )

    def test_state_mapping_preserves_success_auth_failure_and_staleness(self):
        self.assertIs(state_from_card(ProviderStatus.OK, False), ObservationState.OK)
        self.assertIs(
            state_from_card(ProviderStatus.AUTH, False),
            ObservationState.AUTH_EXPIRED,
        )
        self.assertIs(
            state_from_card(ProviderStatus.RATE_LIMITED, False),
            ObservationState.TEMPORARILY_UNAVAILABLE,
        )
        self.assertIs(
            state_from_card(ProviderStatus.ERROR, False),
            ObservationState.TEMPORARILY_UNAVAILABLE,
        )
        for status in ProviderStatus:
            with self.subTest(status=status):
                self.assertIs(
                    state_from_card(status, stale=True),
                    ObservationState.STALE,
                )

    def test_enums_have_only_the_version_one_wire_values(self):
        self.assertEqual(
            {member.value for member in CapabilityState},
            {"supported", "unsupported", "unknown"},
        )
        self.assertEqual(
            {member.value for member in QuotaWindow},
            {
                "session", "five_hour", "weekly", "monthly",
                "billing_cycle", "model_specific",
            },
        )
        self.assertEqual(
            {member.value for member in OperatingSystem},
            {"macos", "windows", "linux"},
        )
        self.assertEqual(
            {member.value for member in SourceStability},
            {"stable", "experimental", "pinned", "opaque"},
        )
        self.assertEqual(
            {member.value for member in SourceProvenance},
            {
                "openusage_upstream", "openusage_bar_builtin",
                "provider_official", "provider_local", "user_session",
            },
        )
        self.assertEqual(
            {member.value for member in CredentialType},
            {
                "provider_owned", "api_key", "oauth", "cli", "local",
                "keychain", "browser_session",
            },
        )
        self.assertEqual(
            {member.value for member in MetricFamily},
            {"subscription_quota", "token_activity", "billing", "operational"},
        )
        self.assertEqual(
            {member.value for member in SourceKind},
            {
                "openusage",
                "builtin_api",
                "official_api",
                "cli",
                "local_log",
                "local_database",
                "keychain",
                "browser_session",
            },
        )
        self.assertEqual(
            {member.value for member in ObservationState},
            {
                "ok",
                "unsupported",
                "not_configured",
                "auth_expired",
                "permission_blocked",
                "temporarily_unavailable",
                "stale",
            },
        )

    def test_capability_values_are_frozen_and_validate_required_metadata(self):
        source = SourceCapability(
            "openusage", SourceKind.OPENUSAGE, 12, 300,
            CredentialType.PROVIDER_OWNED,
            frozenset({OperatingSystem.MACOS}),
            SourceStability.PINNED,
            SourceProvenance.OPENUSAGE_UPSTREAM,
        )
        capabilities = self._unknown_capabilities()
        descriptor = ProviderDescriptor(
            "demo",
            "Demo",
            "api",
            frozenset({MetricFamily.TOKEN_ACTIVITY}),
            frozenset(),
            False,
            (source,),
            capabilities,
        )

        with self.assertRaises(FrozenInstanceError):
            source.timeout_seconds = 1
        with self.assertRaises(FrozenInstanceError):
            descriptor.display_name = "Changed"
        with self.assertRaises(FrozenInstanceError):
            capabilities.billing = CapabilityState.SUPPORTED
        with self.assertRaises(FrozenInstanceError):
            capabilities.quota_windows.state = CapabilityState.SUPPORTED
        with self.assertRaises(ValueError):
            SourceCapability("", SourceKind.OPENUSAGE, 12, 300, CredentialType.PROVIDER_OWNED, frozenset({OperatingSystem.MACOS}), SourceStability.PINNED, SourceProvenance.OPENUSAGE_UPSTREAM)
        with self.assertRaises(ValueError):
            SourceCapability("openusage", SourceKind.OPENUSAGE, 0, 300, CredentialType.PROVIDER_OWNED, frozenset({OperatingSystem.MACOS}), SourceStability.PINNED, SourceProvenance.OPENUSAGE_UPSTREAM)
        with self.assertRaises(ValueError):
            ProviderDescriptor("", "Demo", "api", frozenset(), frozenset(), False, (source,), capabilities)
        with self.assertRaises(ValueError):
            ProviderDescriptor("demo", "", "api", frozenset(), frozenset(), False, (source,), capabilities)
        with self.assertRaises(ValueError):
            ProviderDescriptor("demo", "Demo", "api", frozenset(), frozenset(), False, (), capabilities)
        with self.assertRaises(ValueError):
            QuotaWindowCapability(CapabilityState.SUPPORTED, ())
        with self.assertRaises(ValueError):
            QuotaWindowCapability(
                CapabilityState.UNKNOWN, (QuotaWindow.WEEKLY,)
            )
        with self.assertRaises(ValueError):
            SourceCapability(
                "source", SourceKind.LOCAL_LOG, 12, 300, CredentialType.LOCAL,
                frozenset({OperatingSystem.LINUX}), SourceStability.STABLE,
                SourceProvenance.PROVIDER_LOCAL,
            )

    def test_quota_window_capability_rejects_type_and_state_mutations(self):
        self.assertEqual(
            QuotaWindowCapability(
                CapabilityState.SUPPORTED,
                (QuotaWindow.WEEKLY,),
            ).values,
            (QuotaWindow.WEEKLY,),
        )
        self.assertEqual(
            QuotaWindowCapability(CapabilityState.UNKNOWN, ()).values,
            (),
        )
        self.assertEqual(
            QuotaWindowCapability(CapabilityState.UNSUPPORTED, ()).values,
            (),
        )

        for state in ("supported", "unknown", True, object()):
            with self.subTest(state=state):
                with self.assertRaises(TypeError):
                    QuotaWindowCapability(state, ())
        for values in ([], set(), frozenset(), "weekly", None, True):
            with self.subTest(values=values):
                with self.assertRaises(TypeError):
                    QuotaWindowCapability(CapabilityState.UNKNOWN, values)
        for value in ("weekly", True, object()):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    QuotaWindowCapability(CapabilityState.SUPPORTED, (value,))

        with self.assertRaises(ValueError):
            QuotaWindowCapability(CapabilityState.SUPPORTED, ())
        for state in (CapabilityState.UNKNOWN, CapabilityState.UNSUPPORTED):
            with self.subTest(state=state):
                with self.assertRaises(ValueError):
                    QuotaWindowCapability(state, (QuotaWindow.WEEKLY,))

    def test_provider_capabilities_rejects_every_field_type_mutation(self):
        capabilities = self._unknown_capabilities()

        for value in ("unknown", True, object()):
            with self.subTest(field="quota_windows", value=value):
                with self.assertRaises(TypeError):
                    replace(capabilities, quota_windows=value)

        state_fields = (
            "token_history",
            "model_breakdown",
            "reset_timestamps",
            "billing",
            "credits",
            "balance",
            "cost",
            "rate_limits",
            "service_status",
        )
        for field in state_fields:
            for value in ("supported", "maybe", True, object()):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(TypeError):
                        replace(capabilities, **{field: value})

    def test_source_capability_rejects_every_field_type_mutation(self):
        source = self._local_source()
        invalid_fields = {
            "source_id": (True, object()),
            "kind": ("local_log", True, object()),
            "timeout_seconds": (True, 12.5, "12", None),
            "freshness_seconds": (True, 300.5, "300", None),
            "credential_type": ("local", True, object()),
            "operating_systems": (
                {"macos"},
                {OperatingSystem.MACOS},
                (OperatingSystem.MACOS,),
                [OperatingSystem.MACOS],
                "macos",
                True,
            ),
            "stability": ("stable", "preview", True, object()),
            "provenance": ("provider_local", "guessed", True, object()),
            "credential_scope": (True, 1, object()),
        }
        for field, values in invalid_fields.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(TypeError):
                        replace(source, **{field: value})

        for operating_systems in (
            frozenset({"macos"}),
            frozenset({True}),
            frozenset({object()}),
        ):
            with self.subTest(operating_systems=operating_systems):
                with self.assertRaises(TypeError):
                    replace(source, operating_systems=operating_systems)

    def test_provider_descriptor_rejects_every_field_type_mutation(self):
        descriptor = self._descriptor()
        invalid_fields = {
            "provider_id": (True, object()),
            "display_name": (True, object()),
            "category": (True, object()),
            "metric_families": (
                {MetricFamily.TOKEN_ACTIVITY},
                (MetricFamily.TOKEN_ACTIVITY,),
                [MetricFamily.TOKEN_ACTIVITY],
                "token_activity",
                True,
            ),
            "regions": ({"cn"}, ("cn",), ["cn"], "cn", True),
            "supports_accounts": (0, 1, "false", None, object()),
            "sources": (
                [self._local_source()],
                frozenset({self._local_source()}),
                self._local_source(),
                "source",
                True,
            ),
            "capabilities": ("unknown", True, object()),
        }
        for field, values in invalid_fields.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(TypeError):
                        replace(descriptor, **{field: value})

        member_mutations = {
            "metric_families": (
                frozenset({"token_activity"}),
                frozenset({True}),
                frozenset({object()}),
            ),
            "regions": (
                frozenset({True}),
                frozenset({object()}),
            ),
            "sources": (("source",), (True,), (object(),)),
        }
        for field, values in member_mutations.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(TypeError):
                        replace(descriptor, **{field: value})

    def test_ids_and_credential_scopes_use_the_stable_config_grammar(self):
        valid = SourceCapability(
            "Source.v1_test-2",
            SourceKind.LOCAL_LOG,
            12,
            300,
            CredentialType.LOCAL,
            frozenset({OperatingSystem.MACOS}),
            SourceStability.STABLE,
            SourceProvenance.PROVIDER_LOCAL,
            credential_scope="Account.v1_test-2",
        )
        ProviderDescriptor(
            "Provider.v1_test-2",
            "Provider",
            "api",
            frozenset(),
            frozenset(),
            False,
            (valid,),
            self._unknown_capabilities(),
        )

        for malformed in (" leading", "trailing ", "with space", "path/id"):
            with self.subTest(source_id=malformed):
                with self.assertRaises(ValueError):
                    SourceCapability(
                        malformed, SourceKind.LOCAL_LOG, 12, 300,
                        CredentialType.LOCAL,
                        frozenset({OperatingSystem.MACOS}),
                        SourceStability.STABLE,
                        SourceProvenance.PROVIDER_LOCAL,
                    )
            with self.subTest(provider_id=malformed):
                with self.assertRaises(ValueError):
                    ProviderDescriptor(
                        malformed,
                        "Provider",
                        "api",
                        frozenset(),
                        frozenset(),
                        False,
                        (valid,),
                        self._unknown_capabilities(),
                    )
            with self.subTest(credential_scope=malformed):
                with self.assertRaises(ValueError):
                    SourceCapability(
                        "source",
                        SourceKind.LOCAL_LOG,
                        12,
                        300,
                        CredentialType.LOCAL,
                        frozenset({OperatingSystem.MACOS}),
                        SourceStability.STABLE,
                        SourceProvenance.PROVIDER_LOCAL,
                        credential_scope=malformed,
                    )

        for blank_scope in ("", " ", "\t"):
            with self.subTest(credential_scope=blank_scope):
                with self.assertRaises(ValueError):
                    SourceCapability(
                        "source",
                        SourceKind.LOCAL_LOG,
                        12,
                        300,
                        CredentialType.LOCAL,
                        frozenset({OperatingSystem.MACOS}),
                        SourceStability.STABLE,
                        SourceProvenance.PROVIDER_LOCAL,
                        credential_scope=blank_scope,
                    )

        with self.assertRaises(ValueError):
            registry.resolve("invalid/provider", "Invalid Provider")

    def test_registry_requires_known_provider_and_rejects_duplicate_ids(self):
        minimax = registry.require("minimax")
        self.assertIs(registry.resolve("minimax", "Ignored Name"), minimax)
        with self.assertRaises(KeyError):
            registry.require("future-provider")
        with self.assertRaises(ValueError):
            ProviderRegistry((minimax, minimax))


if __name__ == "__main__":
    unittest.main()
