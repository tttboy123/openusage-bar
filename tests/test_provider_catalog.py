import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from openusage_bar.provider_catalog import (
    CREDENTIAL_TYPES,
    METRIC_FAMILIES,
    PROVIDER_CATEGORIES,
    SOURCE_KINDS,
    ProviderCatalog,
    load_provider_catalog,
)


EXPECTED_UPSTREAM = {
    "openai", "anthropic", "azure_openai", "alibaba_cloud", "openrouter",
    "perplexity", "groq", "mistral", "moonshot", "deepseek", "xai", "zai",
    "gemini_api", "opencode", "gemini_cli", "copilot", "cursor", "claude_code",
    "codex", "amp", "goose", "hermes", "mux", "droid", "crush", "roocode",
    "kilo_code", "kiro_cli", "zed", "codebuff", "kimi_cli", "openclaw", "pi",
    "qwen_cli", "ollama",
}
EXPECTED_BUILTIN = {"minimax", "step_plan"}
EXPECTED_CATEGORIES = {
    "subscription": {
        "claude_code", "codex", "copilot", "cursor", "gemini_cli", "opencode",
        "kiro_cli", "minimax", "step_plan",
    },
    "local_tool": {
        "amp", "goose", "hermes", "mux", "droid", "crush", "roocode",
        "kilo_code", "zed", "codebuff", "kimi_cli", "openclaw", "pi",
        "qwen_cli", "ollama",
    },
    "api": {
        "openai", "anthropic", "azure_openai", "alibaba_cloud", "openrouter",
        "perplexity", "groq", "mistral", "moonshot", "deepseek", "xai", "zai",
        "gemini_api",
    },
}


class ProviderCatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_provider_catalog()

    def test_manifest_freezes_exact_upstream_and_builtin_boundary(self):
        self.assertEqual(
            set(self.catalog.family_ids), EXPECTED_UPSTREAM | EXPECTED_BUILTIN
        )
        self.assertEqual(len(self.catalog.family_ids), 37)
        self.assertEqual(self.catalog.upstream_version, "0.23.0")
        self.assertEqual(self.catalog.upstream_revision, "3059f1b")
        self.assertEqual(set(self.catalog.upstream_family_ids), EXPECTED_UPSTREAM)
        self.assertEqual(
            set(self.catalog.family_ids) - set(self.catalog.upstream_family_ids),
            EXPECTED_BUILTIN,
        )

    def test_manifest_rejects_upstream_boundary_reclassification(self):
        payload = self._manifest_payload()
        mutations = {
            "upstream version changed": lambda value: value["upstream"].update(
                {"version": "0.24.0"}
            ),
            "upstream revision changed": lambda value: value["upstream"].update(
                {"revision": "different"}
            ),
            "deleted upstream declaration": lambda value: value["upstream"][
                "family_ids"
            ].remove("openai"),
            "builtin declared upstream": lambda value: value["upstream"][
                "family_ids"
            ].insert(23, "minimax"),
            "upstream family removed entirely": lambda value: value[
                "families"
            ].pop(next(
                index
                for index, family in enumerate(value["families"])
                if family["id"] == "openai"
            )),
            "unknown family added": lambda value: value["families"].append(
                {
                    **value["families"][-1],
                    "id": "zfuture",
                    "display_name": "Future",
                }
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label):
                candidate = json.loads(json.dumps(payload))
                mutate(candidate)
                candidate["upstream"]["family_ids"].sort()
                candidate["families"].sort(key=lambda family: family["id"])
                with self.assertRaisesRegex(ValueError, "boundary"):
                    self._load_payload(candidate)

    def test_categories_are_exact_and_exhaustive(self):
        actual = {
            category: {
                family.family_id
                for family in self.catalog.families
                if family.category == category
            }
            for category in PROVIDER_CATEGORIES
        }
        self.assertEqual(actual, EXPECTED_CATEGORIES)

    def test_metric_declarations_are_exact(self):
        expected = {
            "claude_code": {"token_activity", "billing"},
            "codex": {"subscription_quota", "token_activity"},
            "copilot": {"subscription_quota", "token_activity", "billing"},
            "cursor": {"subscription_quota", "token_activity", "billing"},
            "opencode": {"subscription_quota", "token_activity", "billing"},
            "gemini_cli": {"subscription_quota", "token_activity"},
            "kiro_cli": {"subscription_quota", "token_activity"},
            "minimax": {"subscription_quota"},
            "step_plan": {"subscription_quota", "billing"},
            "ollama": {"token_activity"},
        }
        for family_id in {
            "amp", "goose", "hermes", "mux", "droid", "crush", "roocode",
            "kilo_code", "zed", "codebuff", "kimi_cli", "openclaw", "pi",
            "qwen_cli",
        }:
            expected[family_id] = {"token_activity", "billing"}
        expected["openai"] = {"token_activity", "billing"}
        for family_id in {
            "anthropic", "azure_openai", "groq", "gemini_api"
        }:
            expected[family_id] = {"operational"}
        for family_id in {"alibaba_cloud", "zai"}:
            expected[family_id] = {
                "subscription_quota", "token_activity", "billing", "operational"
            }
        expected.update(
            {
                "openrouter": {"token_activity", "billing", "operational"},
                "perplexity": {"subscription_quota", "token_activity", "billing"},
                "mistral": {"subscription_quota", "billing", "operational"},
                "moonshot": {"billing", "operational"},
                "deepseek": {"billing", "operational"},
                "xai": {"billing", "operational"},
            }
        )
        self.assertEqual(set(expected), set(self.catalog.family_ids))
        self.assertEqual(
            {
                family.family_id: set(family.metric_families)
                for family in self.catalog.families
            },
            expected,
        )

    def test_manifest_values_are_normalized_and_immutable(self):
        self.assertEqual(
            tuple(self.catalog.family_ids), tuple(sorted(self.catalog.family_ids))
        )
        for family in self.catalog.families:
            with self.subTest(family=family.family_id):
                self.assertTrue(family.display_name.strip())
                self.assertIsInstance(family.metric_families, frozenset)
                self.assertIsInstance(family.regions, frozenset)
                self.assertIsInstance(family.aliases, frozenset)
                self.assertIsInstance(family.sources, tuple)
                self.assertTrue(family.sources)
                self.assertTrue(family.metric_families <= METRIC_FAMILIES)
                self.assertEqual(
                    len({source.source_id for source in family.sources}),
                    len(family.sources),
                )
                for source in family.sources:
                    self.assertGreater(source.timeout_seconds, 0)
                    self.assertGreater(source.freshness_seconds, 0)
                    self.assertIn(source.credential_type, CREDENTIAL_TYPES)
        with self.assertRaises(FrozenInstanceError):
            self.catalog.require("openai").display_name = "Changed"

    def test_manifest_has_exact_capability_and_source_metadata_fields(self):
        payload = self._manifest_payload()
        family_fields = {
            "id", "display_name", "category", "metric_families", "regions",
            "supports_accounts", "capabilities", "sources",
        }
        capability_fields = {
            "quota_windows", "token_history", "model_breakdown",
            "reset_timestamps", "billing", "credits", "balance", "cost",
            "rate_limits", "service_status",
        }
        source_fields = {
            "source_id", "kind", "timeout_seconds", "freshness_seconds",
            "credential_type", "operating_systems", "stability", "provenance",
        }
        for family in payload["families"]:
            with self.subTest(family=family["id"]):
                expected_family_fields = family_fields | (
                    {"aliases"} if "aliases" in family else set()
                )
                self.assertEqual(set(family), expected_family_fields)
                self.assertEqual(set(family["capabilities"]), capability_fields)
                self.assertEqual(
                    set(family["capabilities"]["quota_windows"]),
                    {"state", "values"},
                )
                for source in family["sources"]:
                    expected = source_fields | (
                        {"credential_scope"} if "credential_scope" in source else set()
                    )
                    self.assertEqual(set(source), expected)

    def test_all_37_families_encode_only_conservative_known_capabilities(self):
        quota_windows = {
            "codex": ["five_hour", "weekly"],
            "kiro_cli": ["billing_cycle"],
            "minimax": ["five_hour", "weekly"],
            "step_plan": ["five_hour", "weekly"],
        }
        reset_providers = set(quota_windows)
        credit_providers = {"kiro_cli", "step_plan"}

        self.assertEqual(len(self.catalog.families), 37)
        for family in self.catalog.families:
            with self.subTest(family=family.family_id):
                capabilities = family.capabilities
                expected_windows = quota_windows.get(family.family_id, [])
                self.assertEqual(capabilities.quota_windows.values, tuple(expected_windows))
                self.assertEqual(
                    capabilities.quota_windows.state,
                    "supported" if expected_windows else "unknown",
                )
                token_state = (
                    "supported"
                    if "token_activity" in family.metric_families
                    else "unknown"
                )
                self.assertEqual(capabilities.token_history, token_state)
                self.assertEqual(capabilities.model_breakdown, token_state)
                self.assertEqual(
                    capabilities.billing,
                    "supported" if "billing" in family.metric_families else "unknown",
                )
                self.assertEqual(
                    capabilities.reset_timestamps,
                    "supported" if family.family_id in reset_providers else "unknown",
                )
                credit_state = (
                    "supported" if family.family_id in credit_providers else "unknown"
                )
                self.assertEqual(capabilities.credits, credit_state)
                self.assertEqual(capabilities.balance, credit_state)
                self.assertEqual(
                    capabilities.cost,
                    "supported" if family.family_id == "openai" else "unknown",
                )
                self.assertEqual(capabilities.rate_limits, "unknown")
                self.assertEqual(capabilities.service_status, "unknown")

    def test_source_platform_stability_and_provenance_are_exact(self):
        expected = {
            "openusage": ("pinned", "openusage_upstream"),
            "openai_admin_api": ("stable", "provider_official"),
            "codex_local_log": ("stable", "provider_local"),
            "kiro_keychain": ("stable", "provider_local"),
            "kiro_codewhisperer_api": ("stable", "provider_official"),
            "minimax_builtin_api": ("stable", "openusage_bar_builtin"),
            "step_plan_browser_session": ("experimental", "user_session"),
            "step_plan_official_api": ("stable", "provider_official"),
        }
        for family in self.catalog.families:
            for source in family.sources:
                with self.subTest(family=family.family_id, source=source.source_id):
                    self.assertEqual(source.operating_systems, frozenset({"macos"}))
                    self.assertEqual(
                        (source.stability, source.provenance), expected[source.source_id]
                    )

    def test_capability_and_source_metadata_are_deeply_immutable(self):
        codex = self.catalog.require("codex")
        with self.assertRaises(FrozenInstanceError):
            codex.capabilities.billing = "supported"
        with self.assertRaises(FrozenInstanceError):
            codex.capabilities.quota_windows.state = "unknown"
        with self.assertRaises(FrozenInstanceError):
            codex.sources[0].stability = "opaque"
        with self.assertRaises(AttributeError):
            codex.capabilities.quota_windows.values.append("monthly")

    def test_capability_schema_rejects_missing_unknown_enum_and_invariant_drift(self):
        valid = self._capability_payload()

        def family(value):
            return value["families"][0]

        cases = {
            "missing capabilities": lambda value: family(value).pop("capabilities"),
            "missing capability state": lambda value: family(value)["capabilities"].pop(
                "billing"
            ),
            "missing quota state": lambda value: family(value)["capabilities"][
                "quota_windows"
            ].pop("state"),
            "unknown capability field": lambda value: family(value)[
                "capabilities"
            ].update({"future": "unknown"}),
            "unknown quota field": lambda value: family(value)["capabilities"][
                "quota_windows"
            ].update({"future": []}),
            "invalid tri-state": lambda value: family(value)["capabilities"].update(
                {"billing": "maybe"}
            ),
            "invalid quota state": lambda value: family(value)["capabilities"][
                "quota_windows"
            ].update({"state": "maybe"}),
            "invalid quota window": lambda value: family(value)["capabilities"][
                "quota_windows"
            ].update({"state": "supported", "values": ["daily"]}),
            "supported quota empty": lambda value: family(value)["capabilities"][
                "quota_windows"
            ].update({"state": "supported", "values": []}),
            "unknown quota nonempty": lambda value: family(value)["capabilities"][
                "quota_windows"
            ].update({"state": "unknown", "values": ["weekly"]}),
            "unsupported quota nonempty": lambda value: family(value)[
                "capabilities"
            ]["quota_windows"].update(
                {"state": "unsupported", "values": ["weekly"]}
            ),
            "unsorted quota values": lambda value: family(value)["capabilities"][
                "quota_windows"
            ].update(
                {"state": "supported", "values": ["weekly", "five_hour"]}
            ),
            "duplicate quota values": lambda value: family(value)["capabilities"][
                "quota_windows"
            ].update(
                {"state": "supported", "values": ["weekly", "weekly"]}
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                candidate = json.loads(json.dumps(valid))
                mutate(candidate)
                with self.assertRaises(ValueError):
                    self._load_payload(candidate)

    def test_source_schema_rejects_missing_unknown_enum_and_platform_drift(self):
        valid = self._capability_payload()

        def source(value):
            return value["families"][0]["sources"][0]

        cases = {
            "missing operating systems": lambda value: source(value).pop(
                "operating_systems"
            ),
            "missing stability": lambda value: source(value).pop("stability"),
            "missing provenance": lambda value: source(value).pop("provenance"),
            "unknown source field": lambda value: source(value).update(
                {"future": "value"}
            ),
            "missing macos": lambda value: source(value).update(
                {"operating_systems": ["linux"]}
            ),
            "invalid operating system": lambda value: source(value).update(
                {"operating_systems": ["macos", "unix"]}
            ),
            "unsorted operating systems": lambda value: source(value).update(
                {"operating_systems": ["macos", "linux"]}
            ),
            "duplicate operating systems": lambda value: source(value).update(
                {"operating_systems": ["macos", "macos"]}
            ),
            "invalid stability": lambda value: source(value).update(
                {"stability": "preview"}
            ),
            "invalid provenance": lambda value: source(value).update(
                {"provenance": "guessed"}
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                candidate = json.loads(json.dumps(valid))
                mutate(candidate)
                with self.assertRaises(ValueError):
                    self._load_payload(candidate)

    def test_catalog_container_cannot_diverge_from_its_lookup_index(self):
        original_ids = self.catalog.family_ids
        mutable_upstream = list(self.catalog.upstream_family_ids)
        mutable_families = list(self.catalog.families)
        copied = ProviderCatalog(
            upstream_version=self.catalog.upstream_version,
            upstream_revision=self.catalog.upstream_revision,
            upstream_family_ids=mutable_upstream,
            families=mutable_families,
        )
        mutable_upstream.clear()
        mutable_families.clear()

        with self.assertRaises(FrozenInstanceError):
            self.catalog.families = ()
        with self.assertRaises(FrozenInstanceError):
            self.catalog.upstream_family_ids = ()
        with self.assertRaises(TypeError):
            self.catalog._families["openai"] = self.catalog.require("anthropic")

        self.assertEqual(self.catalog.family_ids, original_ids)
        self.assertEqual(self.catalog.require("openai").family_id, "openai")
        self.assertIsInstance(copied.upstream_family_ids, tuple)
        self.assertIsInstance(copied.families, tuple)
        self.assertEqual(copied.family_ids, original_ids)

    def test_boolean_values_are_rejected_for_integer_and_boolean_fields(self):
        payload = self._manifest_payload()

        def family(value):
            return value["families"][0]

        mutations = {
            "schema version bool": lambda value: value.update(
                {"schema_version": True}
            ),
            "supports accounts integer": lambda value: family(value).update(
                {"supports_accounts": 1}
            ),
            "timeout bool": lambda value: family(value)["sources"][0].update(
                {"timeout_seconds": True}
            ),
            "freshness bool": lambda value: family(value)["sources"][0].update(
                {"freshness_seconds": False}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label):
                candidate = json.loads(json.dumps(payload))
                mutate(candidate)
                with self.assertRaises(ValueError):
                    self._load_payload(candidate)

    def test_every_upstream_family_contains_openusage_without_credentials(self):
        for family_id in EXPECTED_UPSTREAM:
            with self.subTest(family=family_id):
                openusage = [
                    source
                    for source in self.catalog.require(family_id).sources
                    if source.source_id == "openusage"
                ]
                self.assertEqual(len(openusage), 1)
                self.assertEqual(openusage[0].kind, "openusage")
                self.assertEqual(openusage[0].credential_type, "provider_owned")

    def test_bar_enrichment_precedes_openusage_where_it_already_exists(self):
        self.assertEqual(
            [source.source_id for source in self.catalog.require("codex").sources],
            ["codex_local_log", "openusage"],
        )
        self.assertEqual(
            [source.source_id for source in self.catalog.require("kiro_cli").sources],
            ["kiro_keychain", "kiro_codewhisperer_api", "openusage"],
        )
        self.assertEqual(
            [source.source_id for source in self.catalog.require("minimax").sources],
            ["minimax_builtin_api", "openusage"],
        )
        self.assertEqual(
            [source.source_id for source in self.catalog.require("openai").sources],
            ["openai_admin_api", "openusage"],
        )
        self.assertEqual(
            [source.source_id for source in self.catalog.require("step_plan").sources],
            ["step_plan_browser_session", "step_plan_official_api"],
        )

    def test_source_order_and_credential_scopes_are_an_exact_boundary(self):
        special = {
            "codex": ["codex_local_log", "openusage"],
            "kiro_cli": [
                "kiro_keychain", "kiro_codewhisperer_api", "openusage"
            ],
            "minimax": ["minimax_builtin_api", "openusage"],
            "openai": ["openai_admin_api", "openusage"],
            "step_plan": [
                "step_plan_browser_session", "step_plan_official_api"
            ],
        }
        for family_id in EXPECTED_UPSTREAM - {"codex", "kiro_cli", "openai"}:
            special[family_id] = ["openusage"]
        expected_scopes = {
            ("openai", "openai_admin_api"): "openai_admin_api_key",
            ("kiro_cli", "kiro_keychain"): "kiro",
            ("kiro_cli", "kiro_codewhisperer_api"): "kiro",
            ("minimax", "minimax_builtin_api"): "minimax",
            ("step_plan", "step_plan_browser_session"): "step_plan_session",
            ("step_plan", "step_plan_official_api"): "step_plan_api_key",
        }
        self.assertEqual(set(special), set(self.catalog.family_ids))
        for family in self.catalog.families:
            with self.subTest(family=family.family_id):
                self.assertEqual(
                    [source.source_id for source in family.sources],
                    special[family.family_id],
                )
                self.assertEqual(
                    {
                        source.source_id: source.credential_scope
                        for source in family.sources
                        if source.credential_scope is not None
                    },
                    {
                        source_id: scope
                        for (family_id, source_id), scope in expected_scopes.items()
                        if family_id == family.family_id
                    },
                )

    def test_manifest_rejects_unplanned_sources_and_credential_scopes(self):
        payload = self._manifest_payload()

        def family(value, family_id):
            return next(item for item in value["families"] if item["id"] == family_id)

        mutations = {
            "upstream enrichment injection": lambda value: family(
                value, "anthropic"
            )["sources"].insert(
                0,
                {
                    "source_id": "anthropic_official_api",
                    "kind": "official_api",
                    "timeout_seconds": 12,
                    "freshness_seconds": 300,
                    "credential_type": "api_key",
                    "credential_scope": "anthropic",
                    "operating_systems": ["macos"],
                    "stability": "stable",
                    "provenance": "provider_official",
                },
            ),
            "scope added to codex local log": lambda value: family(
                value, "codex"
            )["sources"][0].update({"credential_scope": "codex"}),
            "wrong known scope": lambda value: family(value, "kiro_cli")[
                "sources"
            ][0].update({"credential_scope": "kiro_other"}),
            "known source reordered": lambda value: family(value, "kiro_cli")[
                "sources"
            ].reverse(),
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label):
                candidate = json.loads(json.dumps(payload))
                mutate(candidate)
                with self.assertRaisesRegex(ValueError, "source boundary"):
                    self._load_payload(candidate)

    def test_unknown_ids_resolve_only_through_generic_fallback(self):
        for family_id in (
            "minimaxevil", "cursorless", "not-codex", "step-planmain"
        ):
            with self.subTest(family=family_id):
                family = self.catalog.resolve(family_id, f"Name {family_id}")
                self.assertEqual(family.family_id, family_id)
                self.assertEqual(family.display_name, f"Name {family_id}")
                self.assertEqual(family.category, "api")
                self.assertEqual(
                    family.metric_families, frozenset({"token_activity", "billing"})
                )
                self.assertEqual(len(family.sources), 1)
                self.assertEqual(family.sources[0].source_id, "openusage")
                self.assertEqual(family.sources[0].timeout_seconds, 12)
                self.assertEqual(family.sources[0].freshness_seconds, 300)
                self.assertEqual(
                    family.sources[0].credential_type, "provider_owned"
                )
                self.assertEqual(
                    family.capabilities.quota_windows.state, "unknown"
                )
                self.assertEqual(family.capabilities.quota_windows.values, ())
                for field in (
                    "token_history", "model_breakdown", "reset_timestamps",
                    "billing", "credits", "balance", "cost", "rate_limits",
                    "service_status",
                ):
                    self.assertEqual(getattr(family.capabilities, field), "unknown")
                self.assertEqual(
                    family.sources[0].operating_systems, frozenset({"macos"})
                )
                self.assertEqual(family.sources[0].stability, "pinned")
                self.assertEqual(
                    family.sources[0].provenance, "openusage_upstream"
                )

    def test_discovery_aliases_find_families_without_changing_stable_identity(self):
        self.assertEqual(
            [family.family_id for family in self.catalog.search("glm")], ["zai"]
        )
        self.assertEqual(
            [family.family_id for family in self.catalog.search("智谱")], ["zai"]
        )
        self.assertEqual(
            [family.family_id for family in self.catalog.search("kimi")],
            ["kimi_cli", "moonshot"],
        )
        self.assertEqual(
            [family.family_id for family in self.catalog.search("claude")],
            ["anthropic", "claude_code"],
        )
        self.assertEqual(
            [family.family_id for family in self.catalog.search("qwen")],
            ["alibaba_cloud", "qwen_cli"],
        )
        self.assertEqual(
            [family.family_id for family in self.catalog.search("opencode")],
            ["opencode"],
        )
        self.assertEqual(self.catalog.resolve("glm", "GLM").family_id, "glm")

    def test_discovery_aliases_are_bounded_sorted_public_labels(self):
        payload = self._manifest_payload()
        mutations = (
            ["Zhipu AI", "GLM"],
            ["GLM", "GLM"],
            [""],
            ["x" * 65],
            ["token=private-value"],
        )
        for aliases in mutations:
            with self.subTest(aliases=aliases):
                candidate = json.loads(json.dumps(payload))
                next(
                    family for family in candidate["families"]
                    if family["id"] == "zai"
                )["aliases"] = aliases
                with self.assertRaises(ValueError):
                    self._load_payload(candidate)

    def test_unknown_top_level_provider_and_source_fields_are_rejected(self):
        payload = self._manifest_payload()
        mutations = (
            lambda value: value.update({"unexpected": True}),
            lambda value: value["families"][0].update({"unexpected": True}),
            lambda value: value["families"][0]["sources"][0].update(
                {"unexpected": True}
            ),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                candidate = json.loads(json.dumps(payload))
                mutate(candidate)
                with self.assertRaisesRegex(ValueError, "unknown field"):
                    self._load_payload(candidate)

    def test_malformed_manifest_invariants_are_rejected(self):
        payload = self._manifest_payload()
        mutations = {
            "duplicate family": lambda value: value["families"].append(
                value["families"][0]
            ),
            "unstable family ID": lambda value: value["families"][0].update(
                {"id": "invalid/id"}
            ),
            "blank display name": lambda value: value["families"][0].update(
                {"display_name": " "}
            ),
            "invalid category": lambda value: value["families"][0].update(
                {"category": "desktop"}
            ),
            "unsorted metrics": lambda value: value["families"][0].update(
                {"metric_families": ["token_activity", "billing"]}
            ),
            "duplicate source": lambda value: value["families"][0]["sources"].append(
                value["families"][0]["sources"][0]
            ),
            "invalid credential": lambda value: value["families"][0]["sources"][0].update(
                {"credential_type": "password"}
            ),
            "zero timeout": lambda value: value["families"][0]["sources"][0].update(
                {"timeout_seconds": 0}
            ),
            "zero freshness": lambda value: value["families"][0]["sources"][0].update(
                {"freshness_seconds": 0}
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label):
                candidate = json.loads(json.dumps(payload))
                mutate(candidate)
                with self.assertRaises(ValueError):
                    self._load_payload(candidate)

    def test_manifest_rejects_source_kinds_without_a_domain_representation(self):
        payload = self._manifest_payload()
        codex = next(
            family for family in payload["families"] if family["id"] == "codex"
        )
        codex["sources"][0]["kind"] = "generic_https"

        with self.assertRaisesRegex(ValueError, "invalid"):
            self._load_payload(payload)

    def test_manifest_enum_boundaries_reject_non_string_values_without_echo(self):
        payload = self._manifest_payload()
        mutations = {
            "category": lambda value, replacement: value["families"][0].update(
                {"category": replacement}
            ),
            "source kind": lambda value, replacement: value["families"][0][
                "sources"
            ][0].update({"kind": replacement}),
            "credential type": lambda value, replacement: value["families"][0][
                "sources"
            ][0].update({"credential_type": replacement}),
        }
        invalid_values = (
            ["private-token"],
            {"private-token": "private-value"},
            True,
        )

        for field, mutate in mutations.items():
            for replacement in invalid_values:
                with self.subTest(field=field, type=type(replacement).__name__):
                    candidate = json.loads(json.dumps(payload))
                    mutate(candidate, replacement)
                    with self.assertRaises(ValueError) as caught:
                        self._load_payload(candidate)
                    self.assertNotIn("private-token", str(caught.exception))
                    self.assertNotIn("private-value", str(caught.exception))

    def test_catalog_source_kind_wire_values_match_the_domain_exactly(self):
        from openusage_bar.capabilities import SourceKind

        self.assertEqual(
            SOURCE_KINDS,
            frozenset(member.value for member in SourceKind),
        )

    def test_catalog_requires_exact_family_id(self):
        with self.assertRaises(KeyError):
            self.catalog.require("MINIMAX")
        with self.assertRaises(ValueError):
            self.catalog.resolve("invalid/provider", "Invalid")

    def _manifest_payload(self):
        resource = Path("openusage_bar/resources/provider-catalog.v1.json")
        return json.loads(resource.read_text(encoding="utf-8"))

    def _capability_payload(self):
        payload = self._manifest_payload()
        quota_windows = {
            "codex": ["five_hour", "weekly"],
            "kiro_cli": ["billing_cycle"],
            "minimax": ["five_hour", "weekly"],
            "step_plan": ["five_hour", "weekly"],
        }
        source_metadata = {
            "openusage": ("pinned", "openusage_upstream"),
            "openai_admin_api": ("stable", "provider_official"),
            "codex_local_log": ("stable", "provider_local"),
            "kiro_keychain": ("stable", "provider_local"),
            "kiro_codewhisperer_api": ("stable", "provider_official"),
            "minimax_builtin_api": ("stable", "openusage_bar_builtin"),
            "step_plan_browser_session": ("experimental", "user_session"),
            "step_plan_official_api": ("stable", "provider_official"),
        }
        for family in payload["families"]:
            windows = quota_windows.get(family["id"], [])
            token_state = (
                "supported"
                if "token_activity" in family["metric_families"]
                else "unknown"
            )
            credit_state = (
                "supported"
                if family["id"] in {"kiro_cli", "step_plan"}
                else "unknown"
            )
            family["capabilities"] = {
                "quota_windows": {
                    "state": "supported" if windows else "unknown",
                    "values": windows,
                },
                "token_history": token_state,
                "model_breakdown": token_state,
                "reset_timestamps": (
                    "supported" if family["id"] in quota_windows else "unknown"
                ),
                "billing": (
                    "supported"
                    if "billing" in family["metric_families"]
                    else "unknown"
                ),
                "credits": credit_state,
                "balance": credit_state,
                "cost": "supported" if family["id"] == "openai" else "unknown",
                "rate_limits": "unknown",
                "service_status": "unknown",
            }
            for source in family["sources"]:
                source["operating_systems"] = ["macos"]
                source["stability"], source["provenance"] = source_metadata[
                    source["source_id"]
                ]
        return payload

    def _load_payload(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_provider_catalog(path)


if __name__ == "__main__":
    unittest.main()
