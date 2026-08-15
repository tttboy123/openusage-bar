from __future__ import annotations

import os
import unittest
from pathlib import Path

from openusage_bar.plugin.clients import GatewayAdviceClient, LocalFactsClient
from openusage_bar.plugin.contracts import API_VERSION, sanitize_response


class RecordingTransport:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[tuple[str, str, bytes]] = []

    def json_request(self, method: str, path: str, body: bytes = b"") -> dict[str, object]:
        self.calls.append((method, path, body))
        return self.response


class PluginClientTests(unittest.TestCase):
    def test_usage_projection_drops_every_private_local_api_field(self) -> None:
        transport = RecordingTransport({
            "schemaVersion": "1.0", "dataRevision": 4,
            "generatedAt": "2026-08-10T04:05:06Z",
            "rows": [{
                "day": "2026-08-10", "providerId": "openai",
                "accountRef": "private-account", "modelId": "gpt-5",
                "inputTokens": 1, "outputTokens": 2, "cacheReadTokens": 3,
                "cacheCreationTokens": 4, "reasoningTokens": None,
                "totalTokens": 10, "tokenCountingConvention": "provider_reported",
                "costAmount": None, "costCurrency": None, "costBasis": None,
                "quality": "direct", "importedAt": "private-time", "revision": 2,
                "recordId": "private-record", "sourceId": "private-source",
                "modelFamily": "gpt-5",
            }],
            "coverage": [{"accountRef": "private-account"}],
        })
        client = LocalFactsClient(transport)
        payload = client.query_usage({
            "apiVersion": API_VERSION, "from": "2026-08-10", "to": "2026-08-10",
        })
        sanitized = sanitize_response("/plugin/v1/usage/query", 200, payload)
        self.assertEqual(payload, sanitized)
        encoded = repr(payload)
        for private in ("accountRef", "recordId", "sourceId", "private-account", "private-record"):
            self.assertNotIn(private, encoded)
        self.assertEqual(transport.calls[0][:2], ("GET", "/v1/activity/daily?from=2026-08-10&to=2026-08-10"))

    def test_gateway_client_accepts_only_exact_should_send_contract(self) -> None:
        response = {
            "decision": "yes", "confidence": 1.0, "reason": "quota_healthy",
            "defer_until": None,
            "details": {"quota_remaining": 4.0, "burn_rate_per_min": None, "predicted_exhaustion_minutes": None},
        }
        transport = RecordingTransport(response)
        client = GatewayAdviceClient(transport)
        self.assertEqual(client.should_send({
            "provider": "openai", "model": "gpt-5", "estimated_tokens": 1, "window": "5m",
        }), response)
        response["raw_error"] = "secret"
        with self.assertRaises(RuntimeError):
            client.should_send({
                "provider": "openai", "model": "gpt-5", "estimated_tokens": 1, "window": "5m",
            })


if __name__ == "__main__":
    unittest.main()


def _capacity_provider(**overrides):
    row = {
        "recordId": "openai.subscription", "providerId": "openai", "accountRef": None,
        "quotaName": "Subscription", "unit": "percent", "used": "20", "quotaLimit": "100",
        "remaining": "80", "remainingRatio": 0.8, "resetsAt": None, "periodStart": None,
        "periodEnd": None, "observedAt": "2026-08-10T04:05:06Z",
        "freshnessSeconds": 30, "state": "ok", "quality": "direct", "stale": False,
        "revision": 1, "sourceId": "current.quota", "quotaWindow": "subscription",
        "appliesTo": {"kind": "account", "modelIds": []},
        "estimatedCostPerMillionTokens": None, "constraints": [],
    }
    row.update(overrides)
    return row


class PluginClientFailClosedTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "requires POSIX file semantics")
    def test_clients_reject_invalid_transport_and_requests(self) -> None:
        from openusage_bar.plugin.clients import (
            BoundedJSONTransport,
            GatewayAdviceClient,
            LocalFactsClient,
        )

        with self.assertRaisesRegex(ValueError, "invalid Local API transport"):
            LocalFactsClient(object())
        with self.assertRaisesRegex(ValueError, "invalid Gateway transport"):
            GatewayAdviceClient(object())
        with self.assertRaisesRegex(ValueError, "invalid internal transport"):
            BoundedJSONTransport(host="127.0.0.1", port=17821, unix_socket_path=Path("/tmp/x.sock"))
        with self.assertRaisesRegex(ValueError, "invalid internal transport"):
            BoundedJSONTransport(host="127.0.0.1", port=17821, timeout_seconds=0.05)
        with self.assertRaisesRegex(ValueError, "invalid internal transport"):
            BoundedJSONTransport(host="127.0.0.1", port=17821, bearer_token="")

        transport = BoundedJSONTransport(host="127.0.0.1", port=17821)
        with self.assertRaisesRegex(RuntimeError, "invalid internal request"):
            transport.json_request("DELETE", "/v1/x")

    def test_local_facts_client_fails_closed_on_bad_responses(self) -> None:
        from openusage_bar.plugin.clients import LocalFactsClient
        from openusage_bar.plugin.contracts import API_VERSION

        client = LocalFactsClient(RecordingTransport({}))
        with self.assertRaisesRegex(RuntimeError, "invalid Local API request"):
            client.request("/v1/other", {})
        with self.assertRaisesRegex(RuntimeError, "invalid Local API response"):
            client.query_usage({
                "apiVersion": API_VERSION, "from": "2026-08-10", "to": "2026-08-10",
            })
        with self.assertRaisesRegex(RuntimeError, "invalid Local API response"):
            client.query_quotas({"apiVersion": API_VERSION, "limit": 10})

    def test_query_quotas_projects_capacity_and_enforces_limit(self) -> None:
        from openusage_bar.plugin.clients import LocalFactsClient, ResultTooLarge
        from openusage_bar.plugin.contracts import API_VERSION

        payload = {
            "schemaVersion": "1.0", "dataRevision": 4,
            "generatedAt": "2026-08-10T04:05:06Z",
            "providers": [
                _capacity_provider(),
                _capacity_provider(providerId="deepseek", remainingRatio=None, remaining=None),
            ],
        }
        client = LocalFactsClient(RecordingTransport(payload))
        result = client.query_quotas({"apiVersion": API_VERSION, "limit": 10})
        self.assertEqual(result["object"], "plugin.quotas")
        self.assertEqual(len(result["quotas"]), 2)

        payload["providers"] = [_capacity_provider() for _ in range(129)]
        with self.assertRaisesRegex(RuntimeError, "invalid Local API response"):
            LocalFactsClient(RecordingTransport(payload)).query_quotas(
                {"apiVersion": API_VERSION, "limit": 128}
            )
        payload["providers"] = [{"bad": True}]
        with self.assertRaisesRegex(RuntimeError, "invalid Local API response"):
            LocalFactsClient(RecordingTransport(payload)).query_quotas(
                {"apiVersion": API_VERSION, "limit": 10}
            )

    def test_gateway_advice_client_fails_closed_on_bad_gateway_responses(self) -> None:
        from openusage_bar.plugin.clients import GatewayAdviceClient

        client = GatewayAdviceClient(RecordingTransport({"decision": "maybe"}))
        with self.assertRaises(RuntimeError):
            client.should_send({
                "provider": "openai", "model": "gpt-5",
                "estimated_tokens": 1, "window": "5m",
            })
        with self.assertRaises(RuntimeError):
            client.health()


class PluginClientHelperTests(unittest.TestCase):
    def test_private_helper_validation_fails_closed(self) -> None:
        from openusage_bar.plugin.clients import _integer, _text, _timestamp

        with self.assertRaises(RuntimeError):
            _timestamp("not-a-date")
        with self.assertRaises(RuntimeError):
            _timestamp("x" * 100)
        with self.assertRaises(RuntimeError):
            _integer("9")
        with self.assertRaises(RuntimeError):
            _integer(-1)
        with self.assertRaises(RuntimeError):
            _text(123)
        with self.assertRaises(RuntimeError):
            _text("  padded  ")

    def test_query_usage_enforces_row_limit_and_row_shape(self) -> None:
        from openusage_bar.plugin.clients import LocalFactsClient, ResultTooLarge
        from openusage_bar.plugin.contracts import API_VERSION

        base_row = {
            "day": "2026-08-10", "providerId": "openai", "accountRef": None,
            "modelId": "gpt-5", "inputTokens": 1, "outputTokens": 2,
            "cacheReadTokens": 3, "cacheCreationTokens": 4, "reasoningTokens": None,
            "totalTokens": 10, "tokenCountingConvention": "provider_reported",
            "costAmount": None, "costCurrency": None, "costBasis": None,
            "quality": "direct", "importedAt": "2026-08-10T04:05:06Z",
            "revision": 2, "recordId": "r", "sourceId": "s", "modelFamily": "gpt-5",
        }
        payload = {
            "schemaVersion": "1.0", "dataRevision": 4,
            "generatedAt": "2026-08-10T04:05:06Z",
            "rows": [dict(base_row), dict(base_row, providerId="deepseek")],
            "coverage": [],
        }
        client = LocalFactsClient(RecordingTransport(payload))
        result = client.query_usage({
            "apiVersion": API_VERSION, "from": "2026-08-10", "to": "2026-08-10",
        })
        self.assertEqual(len(result["rows"]), 2)

        payload["rows"] = [dict(base_row) for _ in range(1001)]
        with self.assertRaises(ResultTooLarge):
            LocalFactsClient(RecordingTransport(payload)).query_usage({
                "apiVersion": API_VERSION, "from": "2026-08-10", "to": "2026-08-10",
            })
        payload["rows"] = [{"bad": True}]
        with self.assertRaisesRegex(RuntimeError, "invalid Local API response"):
            LocalFactsClient(RecordingTransport(payload)).query_usage({
                "apiVersion": API_VERSION, "from": "2026-08-10", "to": "2026-08-10",
            })


class BoundedJSONTransportTests(unittest.TestCase):
    def test_json_request_http_roundtrip_and_fail_closed(self) -> None:
        import http.client as http_client
        from unittest.mock import patch

        from openusage_bar.plugin.clients import BoundedJSONTransport

        _OS_ERROR = object()
        queue: list[FakeResponse | object] = []

        class FakeResponse:
            def __init__(self, status=200, body=b'{"ok": true}'):
                self.status = status
                self.body = body

            def read(self, limit):
                return self.body

        class FakeConnection:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                self.calls = []

            def request(self, method, path, **kwargs):
                self.calls.append((method, path, kwargs))

            def getresponse(self):
                entry = queue.pop(0)
                if entry is _OS_ERROR:
                    raise OSError("connection refused")
                return entry

            def close(self):
                return None

        transport = BoundedJSONTransport(
            host="127.0.0.1", port=17821, bearer_token="secret-token"
        )
        with patch.object(http_client, "HTTPConnection", FakeConnection):
            queue.append(FakeResponse())
            result = transport.json_request("POST", "/v1/x", body=b"{}")
            self.assertEqual(result, {"ok": True})

            queue.append(FakeResponse(status=500))
            with self.assertRaisesRegex(RuntimeError, "internal dependency unavailable"):
                transport.json_request("GET", "/v1/x")

            queue.append(FakeResponse(body=b"x" * (1024 * 1024 + 1)))
            with self.assertRaisesRegex(RuntimeError, "internal dependency unavailable"):
                transport.json_request("GET", "/v1/x")

            queue.append(_OS_ERROR)
            with self.assertRaisesRegex(RuntimeError, "internal dependency unavailable"):
                transport.json_request("GET", "/v1/x")
