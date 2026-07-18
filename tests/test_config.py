import json
import os
import tempfile
import unittest
from pathlib import Path

from openusage_bar.config import (
    DailyCostFeedConfig,
    DailyUsageFeedConfig,
    GenericProviderConfig,
    MiniMaxConfig,
    OpenAIOrganizationConfig,
    ProviderConfigStore,
    StepPlanConfig,
)


def daily_feed_config(**overrides):
    values = {
        "provider_id": "glm-work",
        "name": "GLM Work",
        "family_id": "zai",
        "endpoint": "https://api.example.com/v1/usage",
        "method": "GET",
        "header_name": "Authorization",
        "auth_prefix": "Bearer",
        "items_path": "data.items",
        "date_path": "day",
        "model_path": "model",
        "input_tokens_path": "usage.input",
        "output_tokens_path": "usage.output",
        "total_tokens_path": "usage.total",
        "since_parameter": "from",
        "until_parameter": "to",
    }
    values.update(overrides)
    return DailyUsageFeedConfig(**values)


class ProviderConfigTests(unittest.TestCase):
    def test_v1_custom_feeds_migrate_without_changing_identity_or_mappings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "providers.json"
            legacy = {
                "version": 1,
                "providers": [
                    {
                        "type": "generic", "provider_id": "quota-work",
                        "name": "Quota Work", "endpoint": "https://api.example.com/quota",
                        "header_name": "Authorization", "auth_prefix": "Bearer",
                        "primary_path": "data.remaining",
                        "remaining_percent_path": "data.percent",
                        "reset_path": "data.reset", "detail_path": None,
                    },
                    {
                        "type": "daily_usage_feed", "provider_id": "usage-work",
                        "name": "Usage Work", "family_id": "zai",
                        "endpoint": "https://api.example.com/usage", "method": "GET",
                        "header_name": "Authorization", "auth_prefix": "Bearer",
                        "items_path": "data.items", "date_path": "day",
                        "model_path": "model", "input_tokens_path": "input",
                        "output_tokens_path": "output", "total_tokens_path": "total",
                        "since_parameter": "from", "until_parameter": "to",
                    },
                ],
            }
            path.write_text(json.dumps(legacy))

            loaded = ProviderConfigStore(path).load()
            ProviderConfigStore(path).save(loaded)
            reloaded = ProviderConfigStore(path).load()

            self.assertEqual(reloaded, loaded)
            self.assertEqual(json.loads(path.read_text())["version"], 2)
            self.assertEqual(
                [(item.provider_id, item.endpoint) for item in reloaded],
                [("quota-work", "https://api.example.com/quota"),
                 ("usage-work", "https://api.example.com/usage")],
            )

    def test_generic_quota_declaration_is_explicit_and_consistent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProviderConfigStore(Path(directory) / "providers.json")
            base = GenericProviderConfig(
                provider_id="quota-work", name="Quota Work", family_id="zai",
                endpoint="https://api.example.com/quota",
                header_name="Authorization", auth_prefix="Bearer",
                primary_path="data.remaining", remaining_percent_path="data.percent",
                reset_path="data.reset", quota_window="weekly",
                quota_name="Weekly Plan", unit="percent",
            )
            store.save([base])
            self.assertEqual(store.load(), [base])
            for invalid in (
                GenericProviderConfig(**(base.__dict__ | {"unit": "credits"})),
                GenericProviderConfig(**(base.__dict__ | {"quota_window": None})),
            ):
                with self.assertRaises(ValueError):
                    store.save([invalid])

    def test_daily_cost_feed_requires_bounded_monetary_mappings(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProviderConfigStore(Path(directory) / "providers.json")
            config = DailyCostFeedConfig(
                provider_id="cost-work", name="Cost Work", family_id="openai",
                endpoint="https://api.example.com/costs", method="GET",
                header_name="Authorization", auth_prefix="Bearer",
                items_path="data.items", date_path="day",
                amount_path="amount", currency_path="currency",
                since_parameter="from", until_parameter="to",
            )
            store.save([config])
            self.assertEqual(store.load(), [config])
    def test_daily_feed_round_trip_is_secret_free(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "providers.json"
            config = daily_feed_config(
                pagination="cursor",
                cursor_parameter="cursor",
                next_cursor_path="data.next_cursor",
                request_body={"scope": "organization"},
            )

            ProviderConfigStore(path).save([config])

            self.assertEqual(ProviderConfigStore(path).load(), [config])
            text = path.read_text()
            self.assertNotIn("api_key", text.lower())
            self.assertNotIn("secret", text.lower())

    def test_daily_feed_rejects_executable_or_unsafe_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProviderConfigStore(Path(directory) / "providers.json")
            invalid = (
                daily_feed_config(endpoint="http://api.example.com/usage"),
                daily_feed_config(method="DELETE"),
                daily_feed_config(pagination="cursor", next_cursor_path=None),
                daily_feed_config(page_size=0),
                daily_feed_config(items_path="data[0].items"),
                daily_feed_config(since_parameter=None),
                daily_feed_config(header_name="Cookie"),
                daily_feed_config(request_body={"token": "must-not-live-here"}),
            )
            for config in invalid:
                with self.subTest(config=config):
                    with self.assertRaises(ValueError):
                        store.save([config])
    def test_legacy_step_plan_without_site_migrates_to_china(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "providers.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "providers": [
                            {
                                "type": "step_plan",
                                "provider_id": "step-plan-main",
                                "name": "Step Plan",
                            }
                        ],
                    }
                )
            )

            config = ProviderConfigStore(path).load()[0]

            self.assertEqual(config.site, "china")

    def test_rejects_unknown_step_plan_site(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProviderConfigStore(Path(directory) / "providers.json")

            with self.assertRaises(ValueError):
                store.save(
                    [StepPlanConfig("step-plan-main", "Step Plan", site="unknown")]
                )

    def test_serialization_omits_secrets_and_uses_private_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "providers.json"
            store = ProviderConfigStore(path)
            store.save(
                [
                    GenericProviderConfig(
                        provider_id="demo",
                        name="Demo",
                        endpoint="https://api.example.com/usage",
                        header_name="Authorization",
                        auth_prefix="Bearer",
                        primary_path="quota.remaining",
                        remaining_percent_path="quota.percent",
                        reset_path="quota.reset_at",
                    ),
                    MiniMaxConfig(provider_id="minimax-main", name="MiniMax Main"),
                    StepPlanConfig(provider_id="step-plan-main", name="Step Plan"),
                    OpenAIOrganizationConfig(
                        provider_id="openai", name="OpenAI Organization"
                    ),
                ]
            )

            text = path.read_text()
            self.assertNotIn("api_key", text.lower())
            self.assertNotIn("secret", text.lower())
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(
                [item.provider_id for item in store.load()],
                ["demo", "minimax-main", "step-plan-main", "openai"],
            )

    def test_openai_organization_connections_use_independent_stable_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "providers.json"
            store = ProviderConfigStore(path)
            configs = [
                OpenAIOrganizationConfig(
                    "openai-personal", "OpenAI Personal", account_ref="personal"
                ),
                OpenAIOrganizationConfig(
                    "openai-work", "OpenAI Work", account_ref="work"
                ),
            ]
            store.save(configs)

            payload = json.loads(path.read_text())
            self.assertEqual(store.load(), configs)
            self.assertEqual(
                [item["provider_id"] for item in payload["providers"]],
                ["openai-personal", "openai-work"],
            )
            self.assertEqual(
                [item["account_ref"] for item in payload["providers"]],
                ["personal", "work"],
            )

    def test_account_ref_is_optional_opaque_and_not_a_display_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProviderConfigStore(Path(directory) / "providers.json")
            store.save([MiniMaxConfig("minimax-main", "MiniMax Main")])

            for account_ref in (
                "owner@example.com",
                "work account",
                "organizations/work",
                "OpenAI Work",
            ):
                with self.subTest(account_ref=account_ref):
                    with self.assertRaises(ValueError):
                        store.save([
                            OpenAIOrganizationConfig(
                                "openai-work", "OpenAI Work", account_ref=account_ref
                            )
                        ])

    def test_rejects_duplicate_provider_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ProviderConfigStore(Path(directory) / "providers.json")
            with self.assertRaises(ValueError):
                store.save([MiniMaxConfig("same", "One"), MiniMaxConfig("same", "Two")])

    def test_rejects_secret_shaped_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "providers.json"
            path.write_text(json.dumps({"version": 1, "providers": [{"type": "generic", "id": "x", "api_key": "bad"}]}))
            with self.assertRaises(ValueError):
                ProviderConfigStore(path).load()


if __name__ == "__main__":
    unittest.main()
