import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from openusage_bar.config import DailyCostFeedConfig
from openusage_bar.cost_feed import CUSTOM_COST_FEED_SOURCE_ID, DailyCostFeedImporter
from openusage_bar.activity_store import ActivityStore
from openusage_bar.daily_history import ActivityCollector, DailyImportResult
from openusage_bar.models import Overview
from openusage_bar.providers.contracts import CostImportSuccess, ImportFailure


NOW = datetime(2026, 7, 18, 8, tzinfo=timezone.utc)


def config(**overrides):
    values = {
        "provider_id": "cost-work", "name": "Cost Work", "family_id": "openai",
        "endpoint": "https://api.example.com/costs?scope=team", "method": "GET",
        "header_name": "Authorization", "auth_prefix": "Bearer",
        "items_path": "data.items", "date_path": "day",
        "amount_path": "cost.amount", "currency_path": "cost.currency",
        "since_parameter": "from", "until_parameter": "to",
        "timezone": "Asia/Shanghai", "account_ref": "work",
    }
    values.update(overrides)
    return DailyCostFeedConfig(**values)


class DailyCostFeedImporterTests(unittest.TestCase):
    def importer(self, responses, *, configured=None, secret="safe-test-value"):
        keychain = Mock()
        keychain.get.return_value = secret
        client = Mock()
        client.get_json.side_effect = responses
        importer = DailyCostFeedImporter(
            configured or config(), keychain, client, lambda: NOW
        )
        return importer, keychain, client

    def test_range_feed_aggregates_currency_without_creating_token_rows(self):
        payload = {"data": {"items": [
            {"day": "2026-07-17", "cost": {"amount": "1.25", "currency": "usd"}},
            {"day": "2026-07-17", "cost": {"amount": "2.75", "currency": "USD"}},
        ]}}
        importer, keychain, client = self.importer([payload])

        result = importer.fetch_costs(date(2026, 7, 17), date(2026, 7, 17))

        self.assertIsInstance(result, CostImportSuccess)
        self.assertEqual(len(result.rows), 1)
        row = result.rows[0]
        self.assertEqual(
            (row.provider_id, row.account_ref, row.amount, row.currency),
            ("cost-work", "work", "4", "USD"),
        )
        self.assertFalse(hasattr(row, "total_tokens"))
        self.assertEqual(importer.cost_source_id, CUSTOM_COST_FEED_SOURCE_ID)
        keychain.get.assert_called_once_with("cost-work")
        query = parse_qs(urlsplit(client.get_json.call_args.args[0]).query)
        self.assertEqual((query["from"], query["to"]), (["2026-07-17"], ["2026-07-17"]))

    def test_invalid_amount_currency_and_auth_fail_closed(self):
        cases = (
            (None, [], "auth_required"),
            ("safe-test-value", [{"data": {"items": [
                {"day": "2026-07-17", "cost": {"amount": -1, "currency": "USD"}}
            ]}}], "invalid_response"),
            ("safe-test-value", [{"data": {"items": [
                {"day": "2026-07-17", "cost": {"amount": 1, "currency": "bad/id"}}
            ]}}], "invalid_response"),
            ("safe-test-value", [{"data": {"items": [
                {"day": "2026-07-17", "cost": {"amount": "1" * 129, "currency": "USD"}}
            ]}}], "invalid_response"),
        )
        for secret, responses, expected in cases:
            with self.subTest(expected=expected):
                result = self.importer(responses, secret=secret)[0].fetch_costs(
                    date(2026, 7, 17), date(2026, 7, 17)
                )
                self.assertIsInstance(result, ImportFailure)
                self.assertEqual(result.error_code, expected)

    def test_collector_persists_cost_feed_separately_from_token_activity(self):
        import tempfile
        from pathlib import Path

        importer = self.importer([{"data": {"items": [
            {"day": "2026-07-17", "cost": {"amount": "2.5", "currency": "USD"}}
        ]}}])[0]
        openusage = Mock()
        openusage.fetch.return_value = DailyImportResult(True, ())
        with tempfile.TemporaryDirectory() as directory:
            with ActivityStore(Path(directory) / "ledger.sqlite3") as store:
                ActivityCollector(
                    store, openusage,
                    official_importers={"cost-work": importer},
                    clock=lambda: NOW,
                ).refresh(Overview([]))

                costs = store.snapshot_daily_costs("2026-07-17", "2026-07-17")
                usage = store.snapshot_daily_usage("2026-07-17", "2026-07-17")

        self.assertEqual([(row.amount, row.currency) for row in costs.rows], [("2.5", "USD")])
        self.assertEqual(usage.rows, ())


if __name__ == "__main__":
    unittest.main()
