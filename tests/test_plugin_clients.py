from __future__ import annotations

import unittest

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
