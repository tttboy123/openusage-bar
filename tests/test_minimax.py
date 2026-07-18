import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from openusage_bar.config import MiniMaxConfig
from openusage_bar.minimax import (
    MINIMAX_BILLING_SOURCE_ID,
    MiniMaxBillingImporter,
    MiniMaxCodingPlanAdapter,
    MiniMaxParseError,
    parse_minimax_quota_observations,
)
from openusage_bar.models import ProviderStatus
from openusage_bar.openai_organization import ImportFailure, UsageImportSuccess
from openusage_bar.providers.contracts import QuotaFetchSuccess


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)
BILLING_NOW = datetime(2026, 7, 17, 4, tzinfo=timezone.utc)
SECRET = "subscription-key-that-must-not-appear"


def billing_record(
    when: datetime,
    *,
    model: str = "MiniMax-M2.5",
    input_tokens: str = "100",
    output_tokens: str = "40",
    total_tokens: str = "140",
):
    return {
        "created_at": int(when.timestamp()),
        "model": model,
        "consume_input_token": input_tokens,
        "consume_output_token": output_tokens,
        "consume_token": total_tokens,
        # These private account labels are deliberately ignored by the importer.
        "mail": "must-not-be-stored@example.test",
        "creator_name": "Must Not Be Stored",
        "api_token_name": "Must Not Be Stored",
    }


def billing_page(records, *, total=None, status_code=0):
    return {
        "charge_records": records,
        "total_cnt": len(records) if total is None else total,
        "base_resp": {"status_code": status_code, "status_msg": "ok"},
    }


class MiniMaxBillingImporterTests(unittest.TestCase):
    def importer(self, responses, *, secret=SECRET):
        keychain = Mock()
        keychain.get.return_value = secret
        client = Mock()
        client.get_json.side_effect = responses
        importer = MiniMaxBillingImporter(
            MiniMaxConfig("minimax-main", "MiniMax"),
            keychain,
            client,
            lambda: BILLING_NOW,
        )
        return importer, keychain, client

    def test_imports_all_pages_and_aggregates_daily_model_tokens(self):
        page_one = [
            billing_record(datetime(2026, 7, 16, 9, tzinfo=timezone.utc)),
            billing_record(
                datetime(2026, 7, 16, 8, tzinfo=timezone.utc),
                input_tokens="10",
                output_tokens="5",
                total_tokens="15",
            ),
        ]
        page_two = [
            billing_record(
                datetime(2026, 7, 15, 16, tzinfo=timezone.utc),
                model="MiniMax M3 / Coding",
                input_tokens="20",
                output_tokens="7",
                total_tokens="27",
            )
        ]
        importer, keychain, client = self.importer(
            [billing_page(page_one, total=3), billing_page(page_two, total=3)]
        )

        with patch("openusage_bar.minimax.MAX_BILLING_PAGE_SIZE", 2):
            result = importer.fetch_usage(date(2026, 7, 15), date(2026, 7, 17))

        self.assertIsInstance(result, UsageImportSuccess)
        self.assertEqual((result.since, result.until), (date(2026, 7, 15), date(2026, 7, 16)))
        self.assertEqual(len(result.rows), 2)
        first = next(row for row in result.rows if row.day == "2026-07-16")
        self.assertEqual(first.model_id, "MiniMax-M2.5")
        self.assertEqual((first.input_tokens, first.output_tokens, first.total_tokens), (110, 45, 155))
        self.assertEqual(first.quality, "direct")
        self.assertNotIn("must-not-be-stored", repr(result).lower())
        second = next(row for row in result.rows if row.day == "2026-07-16" and row.model_id != "MiniMax-M2.5")
        self.assertRegex(second.model_id, r"^minimax-m3-coding-[0-9a-f]{12}$")
        keychain.get.assert_called_once_with("minimax-main")
        self.assertEqual(len(client.get_json.call_args_list), 2)
        for index, call in enumerate(client.get_json.call_args_list, start=1):
            query = parse_qs(urlsplit(call.args[0]).query)
            self.assertEqual(query, {"page": [str(index)], "limit": ["2"], "aggregate": ["false"]})
            self.assertEqual(call.args[1], {"Authorization": f"Bearer {SECRET}"})

    def test_uses_china_calendar_and_excludes_delayed_current_day(self):
        records = [
            billing_record(datetime(2026, 7, 16, 16, 30, tzinfo=timezone.utc)),
            billing_record(datetime(2026, 7, 16, 15, 30, tzinfo=timezone.utc)),
        ]
        importer, _, _ = self.importer([billing_page(records)])

        result = importer.fetch_usage(date(2026, 7, 16), date(2026, 7, 17))

        self.assertIsInstance(result, UsageImportSuccess)
        self.assertEqual(result.until, date(2026, 7, 16))
        self.assertEqual([row.day for row in result.rows], ["2026-07-16"])
        self.assertEqual(result.rows[0].total_tokens, 140)

    def test_missing_key_and_network_errors_are_sanitized(self):
        missing, _, client = self.importer([], secret=None)
        result = missing.fetch_usage(date(2026, 7, 1), date(2026, 7, 16))
        self.assertIsInstance(result, ImportFailure)
        self.assertEqual(result.error_code, "auth_required")
        client.get_json.assert_not_called()

        from openusage_bar.network import AuthenticationRequired, RateLimited

        for error, expected in (
            (AuthenticationRequired(SECRET), "auth_rejected"),
            (RateLimited(SECRET), "rate_limited"),
        ):
            with self.subTest(expected=expected):
                importer, _, _ = self.importer([error])
                result = importer.fetch_usage(date(2026, 7, 1), date(2026, 7, 16))
                self.assertIsInstance(result, ImportFailure)
                self.assertEqual(result.error_code, expected)
                self.assertNotIn(SECRET, repr(result))

    def test_rejects_malformed_or_incomplete_billing_data(self):
        valid = billing_record(datetime(2026, 7, 16, 9, tzinfo=timezone.utc))
        malformed = []
        for mutation in (
            {**valid, "consume_token": "141"},
            {**valid, "consume_input_token": "-1"},
            {**valid, "model": ""},
            {**valid, "created_at": True},
        ):
            malformed.append([billing_page([mutation])])
        malformed.extend(
            [
                [{"base_resp": {"status_code": 0}, "total_cnt": 0}],
                [billing_page([], status_code=1004)],
                [billing_page([valid], total=-1)],
            ]
        )
        for responses in malformed:
            with self.subTest(responses=responses):
                importer, _, _ = self.importer(responses)
                result = importer.fetch_usage(date(2026, 7, 1), date(2026, 7, 16))
                self.assertIsInstance(result, ImportFailure)
                self.assertEqual(result.error_code, "invalid_response")

    def test_rejects_invalid_ranges_and_current_day_only_without_network(self):
        importer, keychain, client = self.importer([])
        for since, until, expected in (
            (date(2026, 7, 2), date(2026, 7, 1), "invalid_request"),
            (date(2025, 1, 1), date(2026, 7, 16), "invalid_request"),
            (date(2026, 7, 17), date(2026, 7, 17), "not_available_yet"),
        ):
            with self.subTest(since=since, until=until):
                result = importer.fetch_usage(since, until)
                self.assertIsInstance(result, ImportFailure)
                self.assertEqual(result.error_code, expected)
        keychain.get.assert_not_called()
        client.get_json.assert_not_called()

    def test_declares_experimental_source_and_no_cost_surface(self):
        importer, _, _ = self.importer([])
        self.assertEqual(importer.usage_source_id, MINIMAX_BILLING_SOURCE_ID)
        self.assertIsNone(importer.cost_source_id)
        self.assertFalse(hasattr(importer, "fetch_costs"))


class MiniMaxAdapterTests(unittest.TestCase):
    def test_emits_subscription_and_model_specific_quota_windows(self):
        payload = {
            "model_remains": [
                {
                    "model_name": "general",
                    "current_interval_remaining_percent": 93,
                    "current_weekly_remaining_percent": 97,
                    "end_time": int((NOW + timedelta(hours=2)).timestamp() * 1000),
                },
                {
                    "model_name": "MiniMax-M2.5",
                    "current_interval_total_count": 500,
                    "current_interval_usage_count": 320,
                },
            ],
            "base_resp": {"status_code": 0},
        }

        result = parse_minimax_quota_observations(
            MiniMaxConfig("minimax-main", "MiniMax"), payload, NOW
        )

        self.assertIsInstance(result, QuotaFetchSuccess)
        self.assertEqual(len(result.observations), 3)
        subscription = [
            item for item in result.observations
            if item.applies_to_kind == "subscription"
        ]
        model = [
            item for item in result.observations if item.applies_to_kind == "model"
        ]
        self.assertEqual(
            {item.quota_window for item in subscription}, {"five_hour", "weekly"}
        )
        self.assertEqual(model[0].applies_to_model_ids, ("MiniMax-M2.5",))
        self.assertEqual(model[0].remaining_ratio, 0.64)

    def test_fetch_uses_official_token_plan_endpoint(self):
        keychain = Mock()
        keychain.get.return_value = "subscription-key"
        client = Mock()
        client.get_json.return_value = {
            "model_remains": [
                {"current_interval_total_count": 100, "current_interval_usage_count": 75}
            ],
            "base_resp": {"status_code": 0},
        }
        adapter = MiniMaxCodingPlanAdapter(MiniMaxConfig("m", "MiniMax"), keychain, client, lambda: NOW)

        card = adapter.fetch()

        self.assertEqual(card.status, ProviderStatus.OK)
        self.assertEqual(card.family_id, "minimax")
        self.assertEqual(card.credential_source, "minimax_builtin_api")
        self.assertEqual(card.source_kind, "builtin_api")
        client.get_json.assert_called_once_with(
            "https://www.minimaxi.com/v1/token_plan/remains",
            {
                "Authorization": "Bearer subscription-key",
                "Content-Type": "application/json",
            },
        )

    def test_parses_remaining_quota_and_absolute_reset(self):
        end_time = int((NOW + timedelta(hours=5)).timestamp() * 1000)
        payload = {
            "model_remains": [
                {
                    "current_interval_total_count": 4500,
                    "current_interval_usage_count": 255,
                    "remains_time": 17_452_643,
                    "end_time": end_time,
                    "model_name": "MiniMax-M2",
                }
            ],
            "base_resp": {"status_code": 0},
        }

        card = MiniMaxCodingPlanAdapter.parse(MiniMaxConfig("minimax-main", "MiniMax"), payload, NOW)

        self.assertAlmostEqual(card.remaining_percent, 5.67, places=2)
        self.assertEqual(card.primary, "255 / 4500 remaining")
        self.assertEqual(card.detail, "MiniMax-M2")
        self.assertEqual(card.resets_at, NOW + timedelta(hours=5))
        self.assertEqual(card.family_id, "minimax")

    def test_uses_remaining_duration_when_end_time_is_absent(self):
        payload = {
            "model_remains": [{"current_interval_total_count": 100, "current_interval_usage_count": 50, "remains_time": 60_000}],
            "base_resp": {"status_code": 0},
        }
        card = MiniMaxCodingPlanAdapter.parse(MiniMaxConfig("m", "MiniMax"), payload, NOW)
        self.assertEqual(card.resets_at, NOW + timedelta(minutes=1))

    def test_skips_zero_quota_models_and_uses_largest_active_model(self):
        payload = {
            "model_remains": [
                {
                    "current_interval_total_count": 0,
                    "current_interval_usage_count": 0,
                    "model_name": "Inactive model",
                },
                {
                    "current_interval_total_count": 3,
                    "current_interval_usage_count": 0,
                    "model_name": "video",
                },
                {
                    "current_interval_total_count": 500,
                    "current_interval_usage_count": 320,
                    "model_name": "MiniMax-M2.5",
                },
            ],
            "base_resp": {"status_code": 0},
        }

        card = MiniMaxCodingPlanAdapter.parse(MiniMaxConfig("m", "MiniMax"), payload, NOW)

        self.assertEqual(card.primary, "320 / 500 remaining")
        self.assertEqual(card.detail, "MiniMax-M2.5")

    def test_prefers_general_percentage_even_when_count_total_is_zero(self):
        payload = {
            "model_remains": [
                {
                    "current_interval_total_count": 0,
                    "current_interval_usage_count": 0,
                    "current_interval_remaining_percent": 93,
                    "current_weekly_total_count": 0,
                    "current_weekly_usage_count": 0,
                    "current_weekly_remaining_percent": 97,
                    "model_name": "general",
                    "end_time": int((NOW + timedelta(hours=2)).timestamp() * 1000),
                },
                {
                    "current_interval_total_count": 3,
                    "current_interval_usage_count": 0,
                    "model_name": "video",
                },
            ],
            "base_resp": {"status_code": 0},
        }

        card = MiniMaxCodingPlanAdapter.parse(MiniMaxConfig("m", "MiniMax"), payload, NOW)

        self.assertEqual(card.status, ProviderStatus.OK)
        self.assertEqual(card.primary, "5h 93% remaining")
        self.assertEqual(card.detail, "general · Weekly 97% remaining")
        self.assertEqual(card.remaining_percent, 93.0)
        self.assertEqual(card.resets_at, NOW + timedelta(hours=2))

    def test_remaining_percentage_overrides_unreliable_count_field(self):
        payload = {
            "model_remains": [
                {
                    "current_interval_total_count": 3,
                    "current_interval_usage_count": 0,
                    "current_interval_remaining_percent": 100,
                    "model_name": "video",
                }
            ],
            "base_resp": {"status_code": 0},
        }

        card = MiniMaxCodingPlanAdapter.parse(MiniMaxConfig("m", "MiniMax"), payload, NOW)

        self.assertEqual(card.primary, "3 / 3 remaining")
        self.assertEqual(card.remaining_percent, 100.0)

    def test_rejects_zero_total_or_error_response(self):
        with self.assertRaises(MiniMaxParseError):
            MiniMaxCodingPlanAdapter.parse(
                MiniMaxConfig("m", "MiniMax"),
                {"model_remains": [{"current_interval_total_count": 0, "current_interval_usage_count": 0}], "base_resp": {"status_code": 0}},
                NOW,
            )
        with self.assertRaises(MiniMaxParseError):
            MiniMaxCodingPlanAdapter.parse(MiniMaxConfig("m", "MiniMax"), {"base_resp": {"status_code": 1001}}, NOW)

    def test_surfaces_sanitized_minimax_business_error(self):
        keychain = Mock()
        keychain.get.return_value = "subscription-key"
        client = Mock()
        client.get_json.return_value = {
            "base_resp": {
                "status_code": 1004,
                "status_msg": "invalid api key\nplease retry",
            }
        }
        adapter = MiniMaxCodingPlanAdapter(MiniMaxConfig("m", "MiniMax"), keychain, client, lambda: NOW)

        card = adapter.fetch()

        self.assertEqual(card.status, ProviderStatus.ERROR)
        self.assertEqual(card.detail, "MiniMax 1004: invalid api key please retry")
        self.assertNotIn("subscription-key", card.detail)

    def test_missing_key_returns_auth_card(self):
        keychain = Mock()
        keychain.get.return_value = None
        client = Mock()
        adapter = MiniMaxCodingPlanAdapter(MiniMaxConfig("m", "MiniMax"), keychain, client, lambda: NOW)

        card = adapter.fetch()

        self.assertEqual(card.status, ProviderStatus.AUTH)
        client.get_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
