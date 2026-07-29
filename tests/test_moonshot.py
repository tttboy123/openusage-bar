from __future__ import annotations

import unittest
from datetime import datetime, timezone

from openusage_bar.activity_records import BalanceObservation
from openusage_bar.config import MoonshotConfig
from openusage_bar.models import Category, ProviderStatus
from openusage_bar.moonshot import MoonshotBalanceAdapter, endpoint_for_site
from openusage_bar.providers.contracts import BalanceFetchFailure, BalanceFetchSuccess


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


class FakeKeychain:
    def __init__(self, value: str | None = "moonshot-key") -> None:
        self.value = value
        self.accounts: list[str] = []

    def get(self, account: str) -> str | None:
        self.accounts.append(account)
        return self.value


class FakeClient:
    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.requests: list[tuple[str, dict[str, str]]] = []

    def get_json(self, endpoint: str, headers: dict[str, str]):
        self.requests.append((endpoint, headers))
        if self.error is not None:
            raise self.error
        return self.payload


class MoonshotBalanceAdapterTests(unittest.TestCase):
    def adapter(
        self,
        *,
        site: str = "china",
        payload=None,
        key: str | None = "moonshot-key",
        error: Exception | None = None,
    ) -> tuple[MoonshotBalanceAdapter, FakeKeychain, FakeClient]:
        keychain = FakeKeychain(key)
        client = FakeClient(
            payload
            or {
                "code": 0,
                "data": {
                    "available_balance": "123.4500",
                    "voucher_balance": "20",
                    "cash_balance": "103.45",
                },
            },
            error,
        )
        adapter = MoonshotBalanceAdapter(
            MoonshotConfig(
                provider_id=f"moonshot-{site}",
                name=f"Kimi {site}",
                site=site,
                account_ref=site,
            ),
            keychain,
            client,
            lambda: NOW,
        )
        return adapter, keychain, client

    def test_sites_are_fixed_and_isolated(self):
        self.assertEqual(
            endpoint_for_site("china"),
            "https://api.moonshot.cn/v1/users/me/balance",
        )
        self.assertEqual(
            endpoint_for_site("international"),
            "https://api.moonshot.ai/v1/users/me/balance",
        )
        with self.assertRaises(ValueError):
            endpoint_for_site("other")

    def test_fetch_publishes_api_balance_without_capacity_percentage(self):
        adapter, keychain, client = self.adapter(site="china")

        card = adapter.fetch()

        self.assertEqual(keychain.accounts, ["moonshot-china"])
        self.assertEqual(client.requests, [(
            endpoint_for_site("china"),
            {"Authorization": "Bearer moonshot-key"},
        )])
        self.assertEqual(card.category, Category.API)
        self.assertEqual(card.status, ProviderStatus.OK)
        self.assertEqual(card.primary, "CNY 123.45")
        self.assertIsNone(card.remaining_percent)
        self.assertEqual(card.family_id, "moonshot")
        self.assertEqual(card.account_ref, "china")
        self.assertIsInstance(adapter.last_balance_result, BalanceFetchSuccess)
        observation = adapter.last_balance_result.observations[0]
        self.assertEqual(
            observation,
            BalanceObservation(
                record_id="moonshot-china.balance",
                observed_at="2026-07-29T12:00:00.000000Z",
                provider_id="moonshot-china",
                account_ref="china",
                currency="CNY",
                available="123.45",
                voucher="20",
                cash="103.45",
                state="ok",
                quality="direct",
                stale=False,
                source_id="moonshot.balance",
            ),
        )

    def test_international_site_uses_usd(self):
        adapter, _, _ = self.adapter(site="international")

        card = adapter.fetch()

        self.assertEqual(card.primary, "USD 123.45")
        self.assertEqual(
            adapter.last_balance_result.observations[0].currency, "USD"
        )

    def test_missing_key_and_bad_payload_are_sanitized(self):
        missing, _, _ = self.adapter(key=None)
        missing_card = missing.fetch()
        self.assertEqual(missing_card.status, ProviderStatus.AUTH)
        self.assertEqual(
            missing.last_balance_result, BalanceFetchFailure("authentication_required")
        )

        malformed, _, _ = self.adapter(payload={
            "code": 0,
            "data": {"available_balance": "-1"},
        })
        malformed_card = malformed.fetch()
        self.assertEqual(malformed_card.status, ProviderStatus.ERROR)
        self.assertEqual(
            malformed.last_balance_result, BalanceFetchFailure("invalid_response")
        )
        self.assertNotIn("-1", malformed_card.detail or "")

        missing_code, _, _ = self.adapter(payload={
            "data": {"available_balance": "10"},
        })
        missing_code_card = missing_code.fetch()
        self.assertEqual(missing_code_card.status, ProviderStatus.ERROR)
        self.assertEqual(
            missing_code.last_balance_result,
            BalanceFetchFailure("invalid_response"),
        )

    def test_auth_and_rate_limit_failures_never_include_secret_or_payload(self):
        from openusage_bar.network import AuthenticationRequired, RateLimited

        for error, code, status in (
            (
                AuthenticationRequired("credential moonshot-key rejected"),
                "authentication_required",
                ProviderStatus.AUTH,
            ),
            (
                RateLimited("moonshot-key quota"),
                "rate_limited",
                ProviderStatus.RATE_LIMITED,
            ),
        ):
            with self.subTest(code=code):
                adapter, _, _ = self.adapter(error=error)
                card = adapter.fetch()
                self.assertEqual(card.status, status)
                self.assertEqual(
                    adapter.last_balance_result, BalanceFetchFailure(code)
                )
                self.assertNotIn("moonshot-key", (card.detail or "") + (card.last_error or ""))

    def test_malformed_oversized_and_network_failures_are_typed_and_sanitized(self):
        from openusage_bar.network import (
            MalformedResponse,
            NetworkError,
            ResponseTooLarge,
        )

        for error, code, detail in (
            (
                MalformedResponse("private malformed response"),
                "invalid_response",
                "Balance response was invalid",
            ),
            (
                ResponseTooLarge("private oversized response"),
                "response_too_large",
                "Balance response was too large",
            ),
            (
                NetworkError("private timeout"),
                "network_error",
                "Balance request failed",
            ),
        ):
            with self.subTest(code=code):
                adapter, _, _ = self.adapter(error=error)
                card = adapter.fetch()
                self.assertEqual(
                    adapter.last_balance_result, BalanceFetchFailure(code)
                )
                self.assertEqual(card.detail, detail)
                self.assertNotIn("private", (card.detail or "") + (card.last_error or ""))


if __name__ == "__main__":
    unittest.main()
