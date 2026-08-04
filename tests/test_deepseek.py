import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from openusage_bar.deepseek import DEEPSEEK_API_KEY_ENV, DeepSeekBalanceAdapter
from openusage_bar.models import ProviderStatus
from openusage_bar.network import NetworkError
from openusage_bar.providers.contracts import BalanceFetchFailure, BalanceFetchSuccess


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def get_json(self, url, headers):
        self.calls.append((url, headers))
        if self.error is not None:
            raise self.error
        return self.payload


class DeepSeekBalanceAdapterTests(unittest.TestCase):
    def _adapter(self, client):
        return DeepSeekBalanceAdapter(client=client, clock=lambda: NOW)

    def test_reports_balance_as_measured_quota_fact(self):
        client = FakeClient(
            {
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "CNY",
                        "total_balance": "110.00",
                        "granted_balance": "10.00",
                        "topped_up_balance": "100.00",
                    }
                ],
            }
        )
        adapter = self._adapter(client)
        with patch.dict(os.environ, {DEEPSEEK_API_KEY_ENV: "sk-test"}, clear=True):
            card = adapter.fetch()

        self.assertEqual(card.status, ProviderStatus.OK)
        self.assertEqual(card.primary, "CNY 110")
        self.assertIsInstance(adapter.last_balance_result, BalanceFetchSuccess)
        observation = adapter.last_balance_result.observations[0]
        self.assertEqual(observation.currency, "CNY")
        self.assertEqual(observation.available, "110")
        self.assertEqual(observation.voucher, "10")
        self.assertEqual(observation.cash, "100")
        self.assertEqual(observation.source_id, "deepseek.balance")
        self.assertEqual(observation.quality, "direct")
        self.assertEqual(observation.account_ref, "deepseek")
        self.assertEqual(client.calls[0][0], "https://api.deepseek.com/user/balance")
        self.assertEqual(
            client.calls[0][1], {"Authorization": "Bearer sk-test"}
        )

    def test_missing_key_requires_authentication(self):
        client = FakeClient({})
        adapter = self._adapter(client)
        with patch.dict(os.environ, {}, clear=True):
            card = adapter.fetch()

        self.assertEqual(card.status, ProviderStatus.AUTH)
        self.assertEqual(
            adapter.last_balance_result,
            BalanceFetchFailure("authentication_required"),
        )
        self.assertEqual(client.calls, [])

    def test_invalid_payload_fails_closed(self):
        client = FakeClient({"is_available": False, "balance_infos": []})
        adapter = self._adapter(client)
        with patch.dict(os.environ, {DEEPSEEK_API_KEY_ENV: "sk-test"}, clear=True):
            card = adapter.fetch()

        self.assertEqual(card.status, ProviderStatus.ERROR)
        self.assertEqual(
            adapter.last_balance_result, BalanceFetchFailure("invalid_response")
        )

    def test_network_error_is_sanitized(self):
        client = FakeClient(error=NetworkError())
        adapter = self._adapter(client)
        with patch.dict(os.environ, {DEEPSEEK_API_KEY_ENV: "sk-test"}, clear=True):
            card = adapter.fetch()

        self.assertEqual(card.status, ProviderStatus.ERROR)
        self.assertEqual(
            adapter.last_balance_result, BalanceFetchFailure("network_error")
        )
