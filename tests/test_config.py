import json
import os
import tempfile
import unittest
from pathlib import Path

from openusage_bar.config import (
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

    def test_openai_organization_config_is_secret_free_and_uses_canonical_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "providers.json"
            store = ProviderConfigStore(path)
            store.save([OpenAIOrganizationConfig("openai", "OpenAI Org")])

            payload = json.loads(path.read_text())
            self.assertEqual(
                payload["providers"],
                [{"name": "OpenAI Org", "provider_id": "openai", "type": "openai_organization"}],
            )
            self.assertEqual(
                store.load(), [OpenAIOrganizationConfig("openai", "OpenAI Org")]
            )

            with self.assertRaisesRegex(ValueError, "canonical provider ID"):
                store.save([OpenAIOrganizationConfig("openai-work", "OpenAI Org")])

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
