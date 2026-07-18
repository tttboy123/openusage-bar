import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from openusage_bar.activity_store import ActivityStore
from openusage_bar.config import GenericProviderConfig
from openusage_bar.daily_history import ActivityCollector, DailyImportResult
from openusage_bar.generic import GenericHTTPSAdapter, MissingField, extract_path
from openusage_bar.models import Overview, ProviderStatus
from openusage_bar.network import NetworkError
from openusage_bar.providers.contracts import QuotaFetchSuccess


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def config(**overrides):
    values = {
        "provider_id": "demo",
        "name": "Demo",
        "endpoint": "https://api.example.com/usage",
        "header_name": "Authorization",
        "auth_prefix": "Bearer",
        "primary_path": "data.remaining",
        "remaining_percent_path": "data.percent",
        "reset_path": "data.reset_at",
        "detail_path": "data.plan",
        "family_id": "demo",
        "quota_window": "weekly",
        "quota_name": "Weekly Plan",
        "unit": "percent",
    }
    values.update(overrides)
    return GenericProviderConfig(**values)


class GenericProviderTests(unittest.TestCase):
    def assert_collector_publishes_generic_identity(self, card):
        importer = Mock()
        importer.fetch.return_value = DailyImportResult(True, ())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.sqlite3"
            with ActivityStore(path) as store:
                ActivityCollector(store, importer, clock=lambda: NOW).refresh(
                    Overview([card])
                )
                instances = store.provider_instances()
                self.assertEqual(len(instances), 1)
                instance = instances[0]
                self.assertEqual(instance.provider_id, "demo")
                self.assertEqual(instance.family_id, "demo")
                self.assertEqual(instance.credential_source, "api_key")
                self.assertEqual(instance.source_kind, "generic_https")
                self.assertNotIn("endpoint", repr(instance).lower())
            return path.read_bytes().decode("utf-8", errors="ignore")

    def test_extracts_dictionary_path(self):
        self.assertEqual(extract_path({"data": {"remaining": 73}}, "data.remaining"), 73)

    def test_rejects_missing_or_list_path(self):
        with self.assertRaises(MissingField):
            extract_path({"data": []}, "data.remaining")

    def test_maps_configured_fields(self):
        card = GenericHTTPSAdapter.parse(
            config(),
            {"data": {"remaining": 73, "percent": 73, "reset_at": "2026-07-15T00:00:00Z", "plan": "Pro"}},
            NOW,
        )

        self.assertEqual(card.primary, "73")
        self.assertEqual(card.remaining_percent, 73)
        self.assertEqual(card.detail, "Pro")
        self.assertEqual(card.resets_at, datetime(2026, 7, 15, tzinfo=timezone.utc))
        self.assertEqual(card.family_id, "demo")

    def test_success_card_publishes_sanitized_generic_identity(self):
        endpoint = "https://api.example.com/private-usage"
        api_key = "sk-sanitized-private-api-key-value"
        keychain = Mock()
        keychain.get.return_value = api_key
        client = Mock()
        client.get_json.return_value = {
            "data": {
                "remaining": 73,
                "percent": 73,
                "reset_at": "2026-07-15T00:00:00Z",
                "plan": "Pro",
            }
        }
        adapter = GenericHTTPSAdapter(
            config(endpoint=endpoint), keychain, client, lambda: NOW
        )

        with self.assertNoLogs("openusage_bar.generic", level="WARNING"):
            card = adapter.fetch()

        ledger = self.assert_collector_publishes_generic_identity(card)
        self.assertIsInstance(adapter.last_quota_result, QuotaFetchSuccess)
        quota = adapter.last_quota_result.observations[0]
        self.assertEqual((quota.quota_window, quota.quota_name), ("weekly", "Weekly Plan"))
        for private in (endpoint, api_key):
            self.assertNotIn(private, repr(card))
            self.assertNotIn(private, ledger)

    def test_error_card_still_publishes_identity_without_error_secrets(self):
        endpoint = "https://api.example.com/private-error"
        api_key = "sk-sanitized-private-error-key"
        keychain = Mock()
        keychain.get.return_value = api_key
        client = Mock()
        client.get_json.side_effect = NetworkError(
            f"request failed at {endpoint} Authorization Bearer {api_key}"
        )
        adapter = GenericHTTPSAdapter(
            config(endpoint=endpoint), keychain, client, lambda: NOW
        )

        with self.assertNoLogs("openusage_bar.generic", level="WARNING"):
            card = adapter.fetch()

        self.assertEqual(card.status, ProviderStatus.ERROR)
        ledger = self.assert_collector_publishes_generic_identity(card)
        for private in (endpoint, api_key):
            self.assertNotIn(private, repr(card))
            self.assertNotIn(private, ledger)

    def test_quota_card_publishes_subscription_generic_identity(self):
        card = GenericHTTPSAdapter.parse(
            config(name="MiniMax Foo"),
            {
                "data": {
                    "remaining": 73,
                    "percent": 73,
                    "reset_at": "2026-07-15T00:00:00Z",
                    "plan": "Pro",
                }
            },
            NOW,
        )

        self.assertEqual(card.family_id, "demo")
        self.assertEqual(card.name, "MiniMax Foo")
        self.assert_collector_publishes_generic_identity(card)

    def test_missing_key_returns_auth_card_without_network(self):
        keychain = Mock()
        keychain.get.return_value = None
        client = Mock()

        card = GenericHTTPSAdapter(config(), keychain, client, lambda: NOW).fetch()

        self.assertEqual(card.status, ProviderStatus.AUTH)
        client.get_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
