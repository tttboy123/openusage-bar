import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from openusage_bar.config import (
    DailyCostFeedConfig,
    DailyUsageFeedConfig,
    GenericProviderConfig,
    MiniMaxConfig,
    OpenAIOrganizationConfig,
    StepPlanConfig,
)
from openusage_bar.provider_catalog import catalog
from openusage_bar.providers.builtins import default_registry
from tests.provider_conformance import (
    REQUIRED_CASES,
    assert_registry_catalog_agreement,
    load_provider_fixtures,
    runtime_inventory,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "providers"
NOW = datetime(2026, 7, 18, 8, tzinfo=timezone.utc)


def configured_sources():
    return (
        MiniMaxConfig("minimax-work", "MiniMax", account_ref="fixture-a"),
        StepPlanConfig("step-work", "Step Plan", account_ref="fixture-a"),
        OpenAIOrganizationConfig("openai-work", "OpenAI", account_ref="fixture-a"),
        GenericProviderConfig(
            "generic-work", "Generic", "https://api.example.invalid/quota",
            "Authorization", "Bearer", "data.primary", family_id="openai",
            account_ref="fixture-a",
        ),
        DailyUsageFeedConfig(
            "usage-work", "Usage", "openai",
            "https://api.example.invalid/usage", "GET", "Authorization", "Bearer",
            "data.items", "day", "model", "input", "output", "total",
            account_ref="fixture-a",
        ),
        DailyCostFeedConfig(
            "cost-work", "Cost", "openai",
            "https://api.example.invalid/cost", "GET", "Authorization", "Bearer",
            "data.items", "day", "amount", "currency", account_ref="fixture-a",
        ),
    )


class ProviderConformanceTests(unittest.TestCase):
    def fixtures(self):
        return load_provider_fixtures(FIXTURE_ROOT)

    def bindings(self):
        keychain = Mock()
        with patch("openusage_bar.keychain.MacOSKeychain", return_value=keychain):
            return default_registry(clock=lambda: NOW, keychain=keychain).build(
                configured_sources()
            )

    def test_all_fixture_groups_cover_the_reusable_matrix(self):
        fixtures = self.fixtures()

        self.assertEqual(
            {fixture.fixture_id for fixture in fixtures},
            {"codex", "kiro", "minimax", "step_plan", "openusage", "custom"},
        )
        for fixture in fixtures:
            self.assertEqual(fixture.cases, REQUIRED_CASES)
            self.assertIsNone(fixture.unknown_value)
            self.assertEqual(len(fixture.fake_account_refs), 2)
            self.assertNotEqual(*fixture.fake_account_refs)

    def test_runtime_registry_and_catalog_have_fixture_provenance(self):
        bindings = self.bindings()

        assert_registry_catalog_agreement(bindings, catalog, self.fixtures())

        inventory = runtime_inventory(bindings)
        self.assertIn(("codex", "quota", "codex.local_rate_limits"), inventory)
        self.assertIn(("openai", "cost", "custom.cost_feed"), inventory)
        self.assertIn(("openai", "usage", "openai.organization.usage"), inventory)

    def test_loader_rejects_credentials_identity_and_user_content(self):
        unsafe_values = (
            {"note": "eyJabcdefghijk.abcdefghijk.abcdefghijk"},
            {"note": "sk-abcdefghijk"},
            {"note": "person@example.com"},
            {"note": "/Users/example/private.json"},
            {"note": "Oasis-Token"},
            {"response": "copied text"},
            {"raw_metadata": {"safe": "no"}},
        )
        for unsafe in unsafe_values:
            with self.subTest(unsafe=unsafe):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    target = root / "bad"
                    target.mkdir()
                    payload = {
                        "schema_version": 1,
                        "fixture_id": "bad",
                        "families": [],
                        "runtime_sources": [],
                        "catalog_source_ids": [],
                        "catalog_provenances": [],
                        "cases": sorted(REQUIRED_CASES),
                        "unknown_value": None,
                        "fake_account_refs": ["fixture-a", "fixture-b"],
                        **unsafe,
                    }
                    (target / "manifest.json").write_text(
                        json.dumps(payload), encoding="utf-8"
                    )
                    with self.assertRaises(ValueError):
                        load_provider_fixtures(root)

    def test_loader_rejects_missing_cases_zero_unknown_and_shared_accounts(self):
        invalid_fields = (
            {"cases": ["success"]},
            {"unknown_value": 0},
            {"fake_account_refs": ["fixture-a", "fixture-a"]},
        )
        for overrides in invalid_fields:
            with self.subTest(overrides=overrides):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    target = root / "bad"
                    target.mkdir()
                    payload = {
                        "schema_version": 1,
                        "fixture_id": "bad",
                        "families": [],
                        "runtime_sources": [],
                        "catalog_source_ids": [],
                        "catalog_provenances": [],
                        "cases": sorted(REQUIRED_CASES),
                        "unknown_value": None,
                        "fake_account_refs": ["fixture-a", "fixture-b"],
                        **overrides,
                    }
                    (target / "manifest.json").write_text(
                        json.dumps(payload), encoding="utf-8"
                    )
                    with self.assertRaises(ValueError):
                        load_provider_fixtures(root)

    def test_agreement_fails_closed_for_unregistered_runtime_source(self):
        bindings = list(self.bindings())
        source = bindings[0].quota_sources[0]
        source.source_id = "fixture.unregistered"

        with self.assertRaisesRegex(AssertionError, "lack conformance provenance"):
            assert_registry_catalog_agreement(bindings, catalog, self.fixtures())


if __name__ == "__main__":
    unittest.main()
