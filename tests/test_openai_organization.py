import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from openusage_bar.config import OpenAIOrganizationConfig
from openusage_bar.network import AuthenticationRequired, RateLimited
from openusage_bar.openai_organization import (
    CostImportSuccess,
    ImportFailure,
    OpenAIOrganizationCardAdapter,
    OpenAIOrganizationImporter,
    UsageImportSuccess,
)


FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
SECRET = "admin-key-that-must-never-appear"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class OpenAIOrganizationImporterTests(unittest.TestCase):
    def importer(self, responses, *, secret=SECRET):
        keychain = Mock()
        keychain.get.return_value = secret
        client = Mock()
        client.get_json.side_effect = responses
        importer = OpenAIOrganizationImporter(
            OpenAIOrganizationConfig("openai", "OpenAI Organization"),
            keychain,
            client,
            lambda: NOW,
        )
        return importer, keychain, client

    def test_usage_reads_all_pages_and_does_not_double_count_cached_input(self):
        importer, keychain, client = self.importer(
            [
                fixture("openai_organization_usage_page_1.json"),
                fixture("openai_organization_usage_page_2.json"),
            ]
        )

        result = importer.fetch_usage(date(2026, 7, 1), date(2026, 7, 2))

        self.assertIsInstance(result, UsageImportSuccess)
        self.assertEqual(len(result.rows), 3)
        first = next(row for row in result.rows if row.day == "2026-07-01" and row.model_id == "gpt-5.5")
        self.assertEqual((first.input_tokens, first.output_tokens), (100, 40))
        self.assertEqual(first.cache_read_tokens, 25)
        self.assertEqual(first.total_tokens, 140)
        self.assertIsNone(first.cost_amount)
        canonical = next(row.model_id for row in result.rows if row.day == "2026-07-01" and row.model_id != "gpt-5.5")
        self.assertRegex(canonical, r"^gpt-unsafe-name-[0-9a-f]{12}$")
        keychain.get.assert_called_once_with("openai")

        calls = client.get_json.call_args_list
        self.assertEqual(len(calls), 2)
        first_url, first_headers = calls[0].args
        parsed = urlsplit(first_url)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), ("https", "api.openai.com", "/v1/organization/usage/completions"))
        query = parse_qs(parsed.query)
        self.assertEqual(query["bucket_width"], ["1d"])
        self.assertEqual(query["group_by"], ["model"])
        self.assertEqual(query["limit"], ["31"])
        self.assertNotIn("page", query)
        self.assertEqual(first_headers, {"Authorization": f"Bearer {SECRET}"})
        second_query = parse_qs(urlsplit(calls[1].args[0]).query)
        self.assertEqual(second_query["page"], ["cursor-two"])

    def test_costs_sum_each_utc_day_by_currency(self):
        importer, _, _ = self.importer([fixture("openai_organization_costs.json")])

        result = importer.fetch_costs(date(2026, 7, 1), date(2026, 7, 1))

        self.assertIsInstance(result, CostImportSuccess)
        self.assertEqual(
            [(row.day, row.currency, row.amount, row.basis, row.quality) for row in result.rows],
            [
                ("2026-07-01", "EUR", "2", "provider_reported", "direct"),
                ("2026-07-01", "USD", "2", "provider_reported", "direct"),
            ],
        )

    def test_missing_key_and_network_errors_are_sanitized(self):
        missing, _, missing_client = self.importer([], secret=None)
        missing_result = missing.fetch_usage(date(2026, 7, 1), date(2026, 7, 1))
        self.assertIsInstance(missing_result, ImportFailure)
        self.assertEqual(missing_result.error_code, "auth_required")
        missing_client.get_json.assert_not_called()

        for error, expected in ((AuthenticationRequired("bad"), "auth_rejected"), (RateLimited("slow"), "rate_limited")):
            with self.subTest(expected=expected):
                importer, _, _ = self.importer([error])
                result = importer.fetch_costs(date(2026, 7, 1), date(2026, 7, 1))
                self.assertIsInstance(result, ImportFailure)
                self.assertEqual(result.error_code, expected)
                self.assertNotIn(SECRET, repr(result))

    def test_incomplete_or_malformed_pagination_fails_the_whole_operation(self):
        first = fixture("openai_organization_usage_page_1.json")
        repeated = fixture("openai_organization_usage_page_1.json")
        for responses in (
            [first, repeated],
            [{**first, "next_page": None}],
            [{**first, "has_more": False, "next_page": None, "data": [{**first["data"][0], "end_time": 1783036800}]}],
            [{**first, "has_more": False, "next_page": None, "data": [{**first["data"][0], "start_time": 1783123200, "end_time": 1783209600}]}],
        ):
            with self.subTest(responses=responses):
                importer, _, _ = self.importer(responses)
                result = importer.fetch_usage(date(2026, 7, 1), date(2026, 7, 2))
                self.assertIsInstance(result, ImportFailure)
                self.assertEqual(result.error_code, "invalid_response")

    def test_rejects_invalid_ranges_before_keychain_or_network(self):
        importer, keychain, client = self.importer([])
        for since, until in (
            (date(2026, 7, 2), date(2026, 7, 1)),
            (date(2025, 1, 1), date(2026, 7, 1)),
        ):
            with self.subTest(since=since, until=until):
                result = importer.fetch_usage(since, until)
                self.assertIsInstance(result, ImportFailure)
                self.assertEqual(result.error_code, "invalid_request")
        keychain.get.assert_not_called()
        client.get_json.assert_not_called()

    def test_uses_exact_utc_exclusive_end_at_the_365_day_boundary(self):
        empty = {"object": "page", "data": [], "has_more": False, "next_page": None}
        importer, _, client = self.importer([empty])

        result = importer.fetch_usage(date(2025, 7, 3), date(2026, 7, 2))

        self.assertIsInstance(result, UsageImportSuccess)
        query = parse_qs(urlsplit(client.get_json.call_args.args[0]).query)
        self.assertEqual(query["start_time"], ["1751500800"])
        self.assertEqual(query["end_time"], ["1783036800"])

    def test_later_page_failure_never_exposes_partial_rows(self):
        importer, _, _ = self.importer(
            [fixture("openai_organization_usage_page_1.json"), RateLimited("stop")]
        )

        result = importer.fetch_usage(date(2026, 7, 1), date(2026, 7, 2))

        self.assertIsInstance(result, ImportFailure)
        self.assertEqual(result.error_code, "rate_limited")
        self.assertFalse(hasattr(result, "rows"))

    def test_rejects_duplicate_bucket_and_ambiguous_final_cursor(self):
        first = fixture("openai_organization_usage_page_1.json")
        duplicate = {
            **fixture("openai_organization_usage_page_2.json"),
            "data": [first["data"][0]],
        }
        ambiguous = {
            **fixture("openai_organization_usage_page_2.json"),
            "next_page": "must-not-be-accepted",
        }
        for responses in ([first, duplicate], [ambiguous]):
            with self.subTest(responses=responses):
                importer, _, _ = self.importer(responses)
                result = importer.fetch_usage(date(2026, 7, 1), date(2026, 7, 2))
                self.assertIsInstance(result, ImportFailure)
                self.assertEqual(result.error_code, "invalid_response")

    def test_rejects_unsafe_numbers_and_preserves_decimal_strings(self):
        costs = fixture("openai_organization_costs.json")
        costs["data"][0]["results"] = [
            {
                "object": "organization.costs.result",
                "amount": {"value": "0.1234567890123456789", "currency": "usd"},
            }
        ]
        importer, _, _ = self.importer([costs])
        result = importer.fetch_costs(date(2026, 7, 1), date(2026, 7, 1))
        self.assertIsInstance(result, CostImportSuccess)
        self.assertEqual(result.rows[0].amount, "0.1234567890123456789")

        invalid_usage = fixture("openai_organization_usage_page_2.json")
        invalid_usage["data"][0]["results"][0]["input_tokens"] = True
        importer, _, _ = self.importer([invalid_usage])
        result = importer.fetch_usage(date(2026, 7, 2), date(2026, 7, 2))
        self.assertIsInstance(result, ImportFailure)
        self.assertEqual(result.error_code, "invalid_response")

    def test_configured_card_exposes_no_fabricated_quota(self):
        keychain = Mock()
        keychain.get.return_value = SECRET
        card = OpenAIOrganizationCardAdapter(
            OpenAIOrganizationConfig("openai", "OpenAI Org"), keychain, lambda: NOW
        ).fetch()

        self.assertEqual(card.primary, "Configured")
        self.assertIsNone(card.remaining_percent)
        self.assertIsNone(card.resets_at)
        self.assertEqual(card.family_id, "openai")
        self.assertEqual(card.credential_source, "openai_admin_api")
        self.assertEqual(card.source_kind, "official_api")


if __name__ == "__main__":
    unittest.main()
