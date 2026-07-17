import base64
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from openusage_bar.config import StepPlanConfig
from openusage_bar.keychain import KeychainError
from openusage_bar.models import Category, ProviderStatus
from openusage_bar.step_plan import (
    STEP_PLAN_MODELS_ENDPOINT,
    STEP_PLAN_RATE_LIMIT_ENDPOINT,
    STEP_PLAN_STATUS_ENDPOINT,
    STEP_PLAN_TOKEN_SUFFIX,
    STEP_PLAN_WEBID_SUFFIX,
    StepPlanAdapter,
    StepPlanParseError,
    StepPlanSession,
    endpoints_for_site,
)


NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


class StepPlanAdapterTests(unittest.TestCase):
    def test_site_endpoints_never_mix_china_and_international_hosts(self):
        china = endpoints_for_site("china")
        international = endpoints_for_site("international")

        self.assertEqual(china.platform_host, "platform.stepfun.com")
        self.assertEqual(china.api_host, "api.stepfun.com")
        self.assertEqual(international.platform_host, "platform.stepfun.ai")
        self.assertEqual(international.api_host, "api.stepfun.ai")
        self.assertTrue(china.quota.startswith("https://platform.stepfun.com/"))
        self.assertTrue(international.quota.startswith("https://platform.stepfun.ai/"))
        self.assertTrue(china.models.startswith("https://api.stepfun.com/"))
        self.assertTrue(international.models.startswith("https://api.stepfun.ai/"))

    def test_international_web_session_calls_only_international_endpoints(self):
        keychain = Mock()
        keychain.get.side_effect = lambda account: {
            "step-plan-global" + STEP_PLAN_TOKEN_SUFFIX: "access...refresh",
            "step-plan-global" + STEP_PLAN_WEBID_SUFFIX: "web-id",
        }.get(account)
        client = Mock()
        endpoints = endpoints_for_site("international")

        def post_json(endpoint, _headers, _body):
            self.assertIn("stepfun.ai", endpoint)
            self.assertNotIn("stepfun.com", endpoint)
            if endpoint == endpoints.quota:
                return {
                    "status": 1,
                    "five_hour_usage_left_rate": 0.8,
                    "weekly_usage_left_rate": 0.6,
                    "five_hour_usage_reset_time": "0",
                    "weekly_usage_reset_time": "0",
                }
            if endpoint == endpoints.status:
                return {"status": 1, "subscription": {"name": "Global Plus"}}
            self.fail(f"Unexpected endpoint {endpoint}")

        client.post_json.side_effect = post_json
        adapter = StepPlanAdapter(
            StepPlanConfig(
                "step-plan-global", "Step Plan Global", site="international"
            ),
            keychain,
            client,
            lambda: NOW,
        )

        card = adapter.fetch()

        self.assertEqual(card.status, ProviderStatus.OK)
        self.assertEqual(card.primary, "5h 80% remaining")
        self.assertEqual(card.detail, "Weekly 60% remaining · Global Plus")
        self.assertEqual(card.source, "StepFun International Web Session")

    def test_international_api_key_uses_international_models_endpoint(self):
        keychain = Mock()
        keychain.get.side_effect = lambda account: (
            "global-key" if account == "step-plan-global" else None
        )
        client = Mock()
        client.get_json.return_value = {"data": [{"id": "step-3.7-flash"}]}
        config = StepPlanConfig(
            "step-plan-global", "Step Plan Global", site="international"
        )

        card = StepPlanAdapter(config, keychain, client, lambda: NOW).fetch()

        client.get_json.assert_called_once_with(
            endpoints_for_site("international").models,
            {"Authorization": "Bearer global-key"},
        )
        self.assertEqual(card.status, ProviderStatus.OK)
    def test_full_session_cookie_keeps_only_oasis_token_and_webid(self):
        session = StepPlanSession.parse(
            "Oasis-Webid=abc123; __stripe_mid=discard-me; "
            "INGRESSCOOKIE=discard-me-too; Oasis-Token=access...refresh; "
            "_wafdytokenv1=discard-three"
        )

        self.assertEqual(session.token, "access...refresh")
        self.assertEqual(session.webid, "abc123")
        self.assertNotIn("stripe", repr(session).lower())
        self.assertNotIn("access", repr(session).lower())

    def test_bare_oasis_token_derives_webid_from_refresh_jwt(self):
        payload = base64.urlsafe_b64encode(
            json.dumps({"device_id": "derived-web-id"}).encode()
        ).decode().rstrip("=")
        token = f"header.payload.signature...header.{payload}.signature"

        session = StepPlanSession.parse(token)

        self.assertEqual(session.token, token)
        self.assertEqual(session.webid, "derived-web-id")

    def test_fetch_prefers_web_session_and_maps_real_quota_windows(self):
        keychain = Mock()
        values = {
            "step-plan-main" + STEP_PLAN_TOKEN_SUFFIX: "access...refresh",
            "step-plan-main" + STEP_PLAN_WEBID_SUFFIX: "web-id",
            "step-plan-main": "api-key-fallback",
        }
        keychain.get.side_effect = values.get
        client = Mock()
        five_reset = int((NOW + timedelta(hours=5)).timestamp())
        weekly_reset = int((NOW + timedelta(days=6)).timestamp())

        def post_json(endpoint, headers, body):
            self.assertEqual(body, {})
            self.assertEqual(headers["Oasis-Webid"], "web-id")
            self.assertEqual(headers["Cookie"], "Oasis-Token=access...refresh")
            if endpoint == STEP_PLAN_RATE_LIMIT_ENDPOINT:
                return {
                    "status": 1,
                    "five_hour_usage_left_rate": 0.75,
                    "weekly_usage_left_rate": 0.5,
                    "five_hour_usage_reset_time": str(five_reset),
                    "weekly_usage_reset_time": weekly_reset,
                }
            if endpoint == STEP_PLAN_STATUS_ENDPOINT:
                return {"status": 1, "subscription": {"name": "Plus"}}
            self.fail(f"Unexpected endpoint {endpoint}")

        client.post_json.side_effect = post_json
        adapter = StepPlanAdapter(
            StepPlanConfig("step-plan-main", "Step Plan"), keychain, client, lambda: NOW
        )

        card = adapter.fetch()

        self.assertEqual(card.status, ProviderStatus.OK)
        self.assertEqual(card.primary, "5h 75% remaining")
        self.assertEqual(card.detail, "Weekly 50% remaining · resets Jul 20 · Plus")
        self.assertEqual(card.remaining_percent, 75.0)
        self.assertEqual(card.resets_at, NOW + timedelta(hours=5))
        self.assertEqual(card.source, "StepFun China Web Session")
        self.assertEqual(card.family_id, "step_plan")
        self.assertEqual(card.credential_source, "step_plan_browser_session")
        self.assertEqual(card.source_kind, "browser_session")
        client.get_json.assert_not_called()

    def test_live_zero_reset_timestamps_do_not_hide_valid_quota(self):
        card = StepPlanAdapter.parse_quota(
            StepPlanConfig("step-plan-main", "Step Plan"),
            {
                "status": 1,
                "five_hour_usage_left_rate": 1,
                "weekly_usage_left_rate": 1,
                "five_hour_usage_reset_time": "0",
                "weekly_usage_reset_time": "0",
            },
            {"status": 1, "subscription": {"name": "Plus"}},
            NOW,
        )

        self.assertEqual(card.status, ProviderStatus.OK)
        self.assertEqual(card.primary, "5h 100% remaining")
        self.assertEqual(card.detail, "Weekly 100% remaining · Plus")
        self.assertIsNone(card.resets_at)

    def test_credit_plan_fields_override_legacy_zero_windows(self):
        card = StepPlanAdapter.parse_quota(
            StepPlanConfig("step-plan-main", "Step Plan", site="china"),
            {
                "status": 1,
                "plan_family": 2,
                "five_hour_usage_left_rate": 0,
                "weekly_usage_left_rate": 0,
                "five_hour_usage_reset_time": "0",
                "weekly_usage_reset_time": "0",
                "plan_credit_rate_limit": {
                    "subscription_credit_left_rate": 1,
                    "subscription_credit_reset_time": "0",
                    "topup_credit_left_rate": 0,
                    "credit_buckets": [
                        {
                            "type": 1,
                            "credit_total": "400000000",
                            "credit_residual": "400000000",
                            "expire_at": "1784867403",
                            "next_reset_at": "0",
                        }
                    ],
                },
            },
            {"status": 1, "subscription": {"name": "Mini"}},
            NOW,
        )

        self.assertEqual(card.status, ProviderStatus.OK)
        self.assertEqual(card.primary, "100% remaining")
        self.assertEqual(
            card.detail,
            "400M / 400M credits · expires Jul 24 · Mini",
        )
        self.assertEqual(card.remaining_percent, 100.0)
        self.assertIsNone(card.resets_at)

    def test_invalid_credit_block_falls_back_to_legacy_windows(self):
        five_reset = int((NOW + timedelta(hours=5)).timestamp())
        weekly_reset = int((NOW + timedelta(days=6)).timestamp())

        card = StepPlanAdapter.parse_quota(
            StepPlanConfig("step-plan-main", "Step Plan"),
            {
                "status": 1,
                "plan_credit_rate_limit": {
                    "subscription_credit_left_rate": "not-a-rate"
                },
                "five_hour_usage_left_rate": 0.75,
                "weekly_usage_left_rate": 0.5,
                "five_hour_usage_reset_time": five_reset,
                "weekly_usage_reset_time": weekly_reset,
            },
            None,
            NOW,
        )

        self.assertEqual(card.primary, "5h 75% remaining")
        self.assertEqual(card.detail, "Weekly 50% remaining · resets Jul 20")

    def test_expired_session_refreshes_and_persists_only_new_oasis_token(self):
        from openusage_bar.network import AuthenticationRequired

        keychain = Mock()
        keychain.get.side_effect = lambda account: {
            "step-plan-main" + STEP_PLAN_TOKEN_SUFFIX: "old-access...old-refresh",
            "step-plan-main" + STEP_PLAN_WEBID_SUFFIX: "web-id",
        }.get(account)
        client = Mock()
        calls = {"quota": 0}

        def post_json(endpoint, _headers, _body):
            if endpoint == STEP_PLAN_RATE_LIMIT_ENDPOINT:
                calls["quota"] += 1
                if calls["quota"] == 1:
                    raise AuthenticationRequired("expired")
                return {
                    "status": 1,
                    "five_hour_usage_left_rate": 1,
                    "weekly_usage_left_rate": 0.9,
                    "five_hour_usage_reset_time": int((NOW + timedelta(hours=5)).timestamp()),
                    "weekly_usage_reset_time": int((NOW + timedelta(days=7)).timestamp()),
                }
            if endpoint.endswith("/RefreshToken"):
                return {
                    "accessToken": {"raw": "new-access"},
                    "refreshToken": {"raw": "new-refresh"},
                }
            if endpoint == STEP_PLAN_STATUS_ENDPOINT:
                raise AuthenticationRequired("status optional")
            self.fail(f"Unexpected endpoint {endpoint}")

        client.post_json.side_effect = post_json
        adapter = StepPlanAdapter(
            StepPlanConfig("step-plan-main", "Step Plan"), keychain, client, lambda: NOW
        )

        card = adapter.fetch()

        self.assertEqual(card.status, ProviderStatus.OK)
        keychain.set.assert_called_once_with(
            "step-plan-main" + STEP_PLAN_TOKEN_SUFFIX,
            "new-access...new-refresh",
        )

    def test_expired_session_keychain_write_failure_is_sanitized(self):
        from openusage_bar.network import AuthenticationRequired

        keychain = Mock()
        keychain.get.side_effect = lambda account: {
            "step-plan-main" + STEP_PLAN_TOKEN_SUFFIX: "old-access...old-refresh",
            "step-plan-main" + STEP_PLAN_WEBID_SUFFIX: "web-id",
        }.get(account)
        keychain.set.side_effect = KeychainError(
            "Keychain update failed for new-access...new-refresh"
        )
        client = Mock()
        calls = {"quota": 0}

        def post_json(endpoint, _headers, _body):
            if endpoint == STEP_PLAN_RATE_LIMIT_ENDPOINT:
                calls["quota"] += 1
                raise AuthenticationRequired("expired old-access...old-refresh")
            if endpoint.endswith("/RefreshToken"):
                return {
                    "accessToken": {"raw": "new-access"},
                    "refreshToken": {"raw": "new-refresh"},
                }
            self.fail(f"Unexpected endpoint {endpoint}")

        client.post_json.side_effect = post_json
        adapter = StepPlanAdapter(
            StepPlanConfig("step-plan-main", "Step Plan"),
            keychain,
            client,
            lambda: NOW,
        )

        card = adapter.fetch()

        self.assertEqual(card.status, ProviderStatus.ERROR)
        self.assertEqual(card.detail, "Step Plan refresh failed")
        self.assertEqual(card.last_error, "Step Plan refresh failed")
        self.assertEqual(calls["quota"], 1)
        serialized = f"{card.detail} {card.last_error}"
        self.assertNotIn("old-access", serialized)
        self.assertNotIn("new-access", serialized)
        self.assertNotIn("new-refresh", serialized)

    def test_fetch_uses_official_plan_endpoint_without_consuming_quota(self):
        keychain = Mock()
        keychain.get.side_effect = lambda account: (
            "step-plan-key" if account == "step-plan-main" else None
        )
        client = Mock()
        client.get_json.return_value = {
            "object": "list",
            "data": [{"id": "step-3.7-flash"}, {"id": "step-router-v1"}],
        }
        adapter = StepPlanAdapter(
            StepPlanConfig("step-plan-main", "Step Plan"), keychain, client, lambda: NOW
        )

        card = adapter.fetch()

        client.get_json.assert_called_once_with(
            STEP_PLAN_MODELS_ENDPOINT,
            {"Authorization": "Bearer step-plan-key"},
        )
        self.assertEqual(card.status, ProviderStatus.OK)
        self.assertEqual(card.category, Category.SUBSCRIPTION)
        self.assertEqual(card.primary, "Plan connected")
        self.assertEqual(card.detail, "2 models · add a web session for 5h/week quota")
        self.assertIsNone(card.remaining_percent)

    def test_parse_rejects_a_models_response_without_valid_model_ids(self):
        with self.assertRaises(StepPlanParseError):
            StepPlanAdapter.parse(
                StepPlanConfig("step-plan-main", "Step Plan"),
                {"object": "list", "data": [{"name": "missing-id"}]},
                NOW,
            )

    def test_missing_key_returns_auth_without_network_request(self):
        keychain = Mock()
        keychain.get.return_value = None
        client = Mock()
        adapter = StepPlanAdapter(
            StepPlanConfig("step-plan-main", "Step Plan"), keychain, client, lambda: NOW
        )

        card = adapter.fetch()

        self.assertEqual(card.status, ProviderStatus.AUTH)
        client.get_json.assert_not_called()

    def test_auth_failure_is_sanitized(self):
        from openusage_bar.network import AuthenticationRequired

        keychain = Mock()
        keychain.get.side_effect = lambda account: (
            "step-plan-key" if account == "step-plan-main" else None
        )
        client = Mock()
        client.get_json.side_effect = AuthenticationRequired("rejected")
        adapter = StepPlanAdapter(
            StepPlanConfig("step-plan-main", "Step Plan"), keychain, client, lambda: NOW
        )

        card = adapter.fetch()

        self.assertEqual(card.status, ProviderStatus.AUTH)
        self.assertEqual(card.detail, "Credential rejected")
        self.assertNotIn("step-plan-key", card.detail)


if __name__ == "__main__":
    unittest.main()
