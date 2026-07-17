import unittest
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from openusage_bar.config import DailyUsageFeedConfig
from openusage_bar.activity_store import ActivityStore
from openusage_bar.daily_history import ActivityCollector
from openusage_bar.daily_feed import (
    CUSTOM_DAILY_FEED_SOURCE_ID,
    DailyUsageFeedCardAdapter,
    DailyUsageFeedImporter,
)
from openusage_bar.openai_organization import ImportFailure, UsageImportSuccess
from openusage_bar.models import Overview


NOW = datetime(2026, 7, 17, 8, tzinfo=timezone.utc)
SECRET = "custom-feed-key-that-must-not-appear"


def config(**overrides):
    values = {
        "provider_id": "glm-work",
        "name": "GLM Work",
        "family_id": "zai",
        "endpoint": "https://api.example.com/v1/usage?scope=team",
        "method": "GET",
        "header_name": "Authorization",
        "auth_prefix": "Bearer",
        "items_path": "data.items",
        "date_path": "day",
        "model_path": "model",
        "input_tokens_path": "tokens.input",
        "output_tokens_path": "tokens.output",
        "total_tokens_path": "tokens.total",
        "cache_read_tokens_path": "tokens.cache_read",
        "reasoning_tokens_path": "tokens.reasoning",
        "cost_amount_path": "cost.amount",
        "cost_currency": "CNY",
        "pagination": "none",
        "timestamp_format": "date",
        "timezone": "Asia/Shanghai",
        "since_parameter": "from",
        "until_parameter": "to",
    }
    values.update(overrides)
    return DailyUsageFeedConfig(**values)


def record(day="2026-07-16", model="glm-5 / coding", *, total=155):
    return {
        "day": day,
        "model": model,
        "tokens": {
            "input": 100,
            "output": 40,
            "cache_read": 10,
            "reasoning": 5,
            "total": total,
        },
        "cost": {"amount": "1.25"},
        "email": "must-not-be-stored@example.test",
        "raw_response": "must-not-be-stored",
    }


class DailyUsageFeedImporterTests(unittest.TestCase):
    def importer(self, responses, *, configured=None, secret=SECRET):
        keychain = Mock()
        keychain.get.return_value = secret
        client = Mock()
        client.get_json.side_effect = responses
        importer = DailyUsageFeedImporter(
            configured or config(), keychain, client, lambda: NOW
        )
        return importer, keychain, client

    def test_get_maps_only_allowlisted_fields_and_aggregates_rows(self):
        payload = {"data": {"items": [record(), record(model="glm-5 / coding")]}}
        importer, keychain, client = self.importer([payload])

        result = importer.fetch_usage(date(2026, 7, 15), date(2026, 7, 16))

        self.assertIsInstance(result, UsageImportSuccess)
        self.assertEqual((result.since, result.until), (date(2026, 7, 15), date(2026, 7, 16)))
        self.assertEqual(len(result.rows), 1)
        row = result.rows[0]
        self.assertRegex(row.model_id, r"^glm-5-coding-[0-9a-f]{12}$")
        self.assertEqual(
            (row.input_tokens, row.output_tokens, row.cache_read_tokens, row.reasoning_tokens, row.total_tokens),
            (200, 80, 20, 10, 310),
        )
        self.assertEqual((row.cost_amount, row.cost_currency, row.cost_basis), ("2.5", "CNY", "provider_reported"))
        self.assertNotIn("must-not-be-stored", repr(result).lower())
        keychain.get.assert_called_once_with("glm-work")
        url, headers = client.get_json.call_args.args
        self.assertEqual(
            parse_qs(urlsplit(url).query),
            {"scope": ["team"], "from": ["2026-07-15"], "to": ["2026-07-16"]},
        )
        self.assertEqual(headers, {"Authorization": f"Bearer {SECRET}"})

    def test_page_pagination_and_date_parameters_are_bounded(self):
        configured = config(
            pagination="page", page_parameter="page", limit_parameter="size",
            page_size=2, since_parameter="from", until_parameter="to",
            cost_amount_path=None, cost_currency=None,
        )
        importer, _, client = self.importer(
            [
                {"data": {"items": [record(), record(model="glm-4.5")]}},
                {"data": {"items": [record(day="2026-07-15", model="glm-4")]}},
            ],
            configured=configured,
        )

        result = importer.fetch_usage(date(2026, 7, 15), date(2026, 7, 16))

        self.assertIsInstance(result, UsageImportSuccess)
        self.assertEqual(len(client.get_json.call_args_list), 2)
        first = parse_qs(urlsplit(client.get_json.call_args_list[0].args[0]).query)
        second = parse_qs(urlsplit(client.get_json.call_args_list[1].args[0]).query)
        self.assertEqual(first["page"], ["1"])
        self.assertEqual(second["page"], ["2"])
        self.assertEqual(first["size"], ["2"])
        self.assertEqual((first["from"], first["to"]), (["2026-07-15"], ["2026-07-16"]))

    def test_cursor_post_uses_fixed_json_body_and_detects_cursor_completion(self):
        configured = config(
            method="POST", pagination="cursor", cursor_parameter="cursor",
            next_cursor_path="data.next", request_body={"scope": "team"},
            cost_amount_path=None, cost_currency=None,
        )
        keychain = Mock()
        keychain.get.return_value = SECRET
        client = Mock()
        client.post_json.side_effect = [
            {"data": {"items": [record()], "next": "cursor-two"}},
            {"data": {"items": [], "next": None}},
        ]
        importer = DailyUsageFeedImporter(configured, keychain, client, lambda: NOW)

        result = importer.fetch_usage(date(2026, 7, 16), date(2026, 7, 16))

        self.assertIsInstance(result, UsageImportSuccess)
        self.assertEqual(client.post_json.call_count, 2)
        self.assertEqual(
            client.post_json.call_args_list[0].args[2],
            {"scope": "team", "from": "2026-07-16", "to": "2026-07-16"},
        )
        self.assertEqual(
            client.post_json.call_args_list[1].args[2],
            {
                "scope": "team", "from": "2026-07-16", "to": "2026-07-16",
                "cursor": "cursor-two",
            },
        )

    def test_iso_timestamp_uses_configured_calendar_timezone(self):
        configured = config(
            timestamp_format="iso8601", cost_amount_path=None, cost_currency=None
        )
        item = record(day="2026-07-15T16:30:00Z")
        importer, _, _ = self.importer(
            [{"data": {"items": [item]}}], configured=configured
        )

        result = importer.fetch_usage(date(2026, 7, 16), date(2026, 7, 16))

        self.assertIsInstance(result, UsageImportSuccess)
        self.assertEqual(result.rows[0].day, "2026-07-16")

    def test_repeated_cursor_fails_without_returning_partial_rows(self):
        configured = config(
            pagination="cursor", next_cursor_path="data.next",
            cost_amount_path=None, cost_currency=None,
        )
        importer, _, _ = self.importer(
            [
                {"data": {"items": [record()], "next": "repeat"}},
                {"data": {"items": [record()], "next": "repeat"}},
            ],
            configured=configured,
        )

        result = importer.fetch_usage(date(2026, 7, 16), date(2026, 7, 16))

        self.assertIsInstance(result, ImportFailure)
        self.assertEqual(result.error_code, "invalid_response")
        self.assertFalse(hasattr(result, "rows"))

    def test_auth_network_and_payload_failures_are_sanitized(self):
        from openusage_bar.network import AuthenticationRequired, NetworkError

        cases = (
            (None, [], "auth_required"),
            (SECRET, [AuthenticationRequired(SECRET)], "auth_rejected"),
            (SECRET, [NetworkError(SECRET)], "network_error"),
            (SECRET, [{"data": {"items": [record(total=999)]}}], "invalid_response"),
        )
        for secret, responses, expected in cases:
            with self.subTest(expected=expected):
                importer, _, _ = self.importer(responses, secret=secret)
                result = importer.fetch_usage(date(2026, 7, 16), date(2026, 7, 16))
                self.assertIsInstance(result, ImportFailure)
                self.assertEqual(result.error_code, expected)
                self.assertNotIn(SECRET, repr(result))

    def test_card_and_importer_publish_custom_source_without_endpoint(self):
        keychain = Mock()
        keychain.get.return_value = SECRET
        configured = config(endpoint="https://private.example.test/usage")
        card = DailyUsageFeedCardAdapter(configured, keychain, lambda: NOW).fetch()
        importer = DailyUsageFeedImporter(configured, keychain, Mock(), lambda: NOW)

        self.assertEqual(card.family_id, "zai")
        self.assertEqual(card.credential_source, "api_key")
        self.assertEqual(card.source_kind, "generic_https")
        self.assertNotIn(configured.endpoint, repr(card))
        self.assertEqual(importer.usage_source_id, CUSTOM_DAILY_FEED_SOURCE_ID)
        self.assertIsNone(importer.cost_source_id)

    def test_custom_known_family_persists_instance_and_source_provenance(self):
        keychain = Mock()
        keychain.get.return_value = SECRET
        client = Mock()
        client.get_json.return_value = {"data": {"items": [record()]}}
        configured = config(cost_amount_path=None, cost_currency=None)
        card = DailyUsageFeedCardAdapter(configured, keychain, lambda: NOW).fetch()
        importer = DailyUsageFeedImporter(configured, keychain, client, lambda: NOW)

        with tempfile.TemporaryDirectory() as directory:
            with ActivityStore(Path(directory) / "ledger.sqlite3") as store:
                collector = ActivityCollector(
                    store,
                    Mock(),
                    official_importers={"glm-work": importer},
                    clock=lambda: NOW,
                    local_timezone=timezone.utc,
                )
                collector.refresh(Overview([card]))

                instances = store.provider_instances()
                rows = store.snapshot_daily_usage("2026-07-01", "2026-07-31").rows

        self.assertEqual(len(instances), 1)
        self.assertEqual((instances[0].provider_id, instances[0].family_id), ("glm-work", "zai"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source_id, CUSTOM_DAILY_FEED_SOURCE_ID)


if __name__ == "__main__":
    unittest.main()
