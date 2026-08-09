from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import openusage_bar.collector_cli as collector_cli
from openusage_bar.gateway.config import GatewayConfig, GatewayMode
from openusage_bar.gateway.cache import SQLiteGatewayCache
from openusage_bar.gateway.contracts import Decision
from openusage_bar.gateway.fallback import Candidate, FallbackSelector
from openusage_bar.gateway.providers import GatewayProviderError, ProviderResult


SECRET = "sk-runtime-secret-must-not-leak"
NO_FALLBACK = {
    "attempted": False,
    "attemptCount": 1,
    "finalAction": "none",
}
EXACT_HIT_FALLBACK = {
    "attempted": False,
    "attemptCount": 0,
    "finalAction": "none",
}


@dataclass(frozen=True)
class _Capacity:
    provider_id: str
    model_id: str
    remaining: int = 1_000
    limit: int = 2_000
    window: str = "daily"


class _Query:
    def capacity(self):
        return _Capacity("openai", "gpt-4.1-mini")


class GatewayRuntimeTests(unittest.TestCase):
    def test_runtime_returns_gateway_native_non_stream_response(self) -> None:
        from openusage_bar.gateway.runtime import GatewayRuntime

        calls: list[tuple[str, bytes]] = []

        def egress(provider_id: str, request_body: bytes, **_kwargs):
            calls.append((provider_id, request_body))
            body = {
                "id": "resp_123",
                "object": "response",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "hello from gateway",
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 3, "output_tokens": 4},
                "private": SECRET,
            }
            return ProviderResult(
                200,
                (("Content-Type", "application/json"),),
                (
                    json.dumps(
                        body,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8"),
                ),
            )

        runtime = GatewayRuntime(egress=egress)
        response = runtime(
            {
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "request": {
                    "model": "gpt-4.1-mini",
                    "input": "hello",
                    "stream": False,
                },
            }
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "openai")
        self.assertEqual(
            json.loads(calls[0][1].decode("utf-8")),
            {
                "input": "hello",
                "model": "gpt-4.1-mini",
                "stream": False,
            },
        )
        self.assertEqual(
            response,
            {
                "apiVersion": "gateway.openusage/v1",
                "object": "gateway.response",
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "stream": False,
                "status": "complete",
                "outputText": "hello from gateway",
                "usage": {"inputTokens": 3, "outputTokens": 4},
                "tool": {"observed": False},
                "cache": {"outcome": "disabled"},
                "fallback": NO_FALLBACK,
            },
        )
        self.assertNotIn("headers", response)
        self.assertNotIn(SECRET, repr(response))

    def test_runtime_normalizes_five_provider_non_stream_response_shapes(
        self,
    ) -> None:
        from openusage_bar.gateway.runtime import GatewayRuntime

        provider_bodies = {
            "openai": {
                "output_text": "openai text",
                "usage": {"input_tokens": 1, "output_tokens": 2},
                "private": SECRET,
            },
            "anthropic": {
                "content": [{"type": "text", "text": "anthropic text"}],
                "usage": {"input_tokens": 3, "output_tokens": 4},
                "private": SECRET,
            },
            "deepseek": {
                "choices": [{"message": {"content": "deepseek text"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6},
                "private": SECRET,
            },
            "openrouter": {
                "choices": [{"message": {"content": "openrouter text"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 8},
                "private": SECRET,
            },
            "ollama": {
                "message": {"role": "assistant", "content": "ollama text"},
                "prompt_eval_count": 9,
                "eval_count": 10,
                "private": SECRET,
            },
        }

        for provider_id, body in provider_bodies.items():
            with self.subTest(provider=provider_id):
                def egress(_provider_id: str, _request_body: bytes, **_kwargs):
                    self.assertEqual(_provider_id, provider_id)
                    return ProviderResult(
                        200,
                        (("Content-Type", "application/json"),),
                        (
                            json.dumps(
                                body,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ).encode("utf-8"),
                        ),
                    )

                runtime = GatewayRuntime(egress=egress)
                response = runtime(
                    {
                        "provider": provider_id,
                        "model": "test-model",
                        "request": {
                            "model": "test-model",
                            "input": "hello",
                            "stream": False,
                        },
                    }
                )

                self.assertEqual(response["apiVersion"], "gateway.openusage/v1")
                self.assertEqual(response["object"], "gateway.response")
                self.assertEqual(response["provider"], provider_id)
                self.assertEqual(response["model"], "test-model")
                self.assertFalse(response["stream"])
                self.assertEqual(response["status"], "complete")
                self.assertIn(" text", response["outputText"])
                self.assertIn("inputTokens", response["usage"])
                self.assertIn("outputTokens", response["usage"])
                self.assertEqual(response.get("tool"), {"observed": False})
                self.assertEqual(response["cache"], {"outcome": "disabled"})
                self.assertEqual(response["fallback"], NO_FALLBACK)
                serialized = json.dumps(response, sort_keys=True)
                self.assertNotIn(SECRET, serialized)
                self.assertNotIn("private", serialized)

    def test_runtime_exact_cache_hit_replays_gateway_native_stream_response(
        self,
    ) -> None:
        from openusage_bar.gateway.runtime import GatewayRuntime

        calls: list[str] = []

        def egress(provider_id: str, _request_body: bytes, **_kwargs):
            calls.append(provider_id)
            return ProviderResult(
                200,
                (("Content-Type", "text/event-stream"),),
                (
                    b"event: response.output_text.delta\n"
                    b'data: {"type":"response.output_text.delta",'
                    b'"delta":"hello jane@example.com"}\n\n'
                    b"event: response.completed\n"
                    b'data: {"type":"response.completed","response":'
                    b'{"usage":{"input_tokens":3,"output_tokens":4}}}\n\n',
                ),
            )

        payload = {
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "request": {
                "model": "gpt-4.1-mini",
                "input": "hello jane@example.com",
                "stream": True,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteGatewayCache(Path(directory) / "gateway-cache.sqlite3")
            runtime = GatewayRuntime(egress=egress, cache=cache)
            try:
                first = runtime(payload)
                second = runtime(payload)
            finally:
                runtime.close()

            with self.assertRaisesRegex(RuntimeError, "^cache is closed$"):
                cache.stats()
            runtime.close()

        self.assertEqual(calls, ["openai"])
        self.assertEqual(first["cache"], {"outcome": "miss"})
        self.assertEqual(second["cache"], {"outcome": "exact_hit"})
        self.assertEqual(second["outputText"], "hello jane@example.com")
        self.assertIsNone(second["usage"])
        self.assertEqual(second.get("tool"), {"observed": False})
        self.assertEqual(second.get("fallback"), EXACT_HIT_FALLBACK)
        self.assertNotIn(SECRET, repr(second))

    def test_runtime_errors_are_sanitized_gateway_native_responses(self) -> None:
        from openusage_bar.gateway.runtime import GatewayRuntime

        def egress(_provider_id: str, _request_body: bytes, **_kwargs):
            raise GatewayProviderError("upstream_timeout", True)

        runtime = GatewayRuntime(egress=egress)
        response = runtime(
            {
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "request": {
                    "model": "gpt-4.1-mini",
                    "input": "hello",
                    "stream": False,
                },
            }
        )

        self.assertEqual(
            response,
            {
                "apiVersion": "gateway.openusage/v1",
                "object": "gateway.error",
                "error": {
                    "code": "upstream_timeout",
                    "message": "Provider request failed.",
                    "retryable": True,
                },
            },
        )

    def test_runtime_uses_one_safe_fallback_candidate_for_retryable_failure(
        self,
    ) -> None:
        from openusage_bar.gateway.runtime import GatewayRuntime

        calls: list[tuple[str, dict[str, object]]] = []

        def egress(provider_id: str, request_body: bytes, **_kwargs):
            request = json.loads(request_body.decode("utf-8"))
            calls.append((provider_id, request))
            if provider_id == "openai":
                raise GatewayProviderError("upstream_timeout", True)
            return ProviderResult(
                200,
                (("Content-Type", "application/json"),),
                (
                    json.dumps(
                        {
                            "message": {
                                "role": "assistant",
                                "content": "fallback ok",
                            },
                            "prompt_eval_count": 1,
                            "eval_count": 2,
                        },
                        separators=(",", ":"),
                    ).encode("utf-8"),
                ),
            )

        runtime = GatewayRuntime(
            egress=egress,
            fallback_selector=FallbackSelector(cost_cap_multiplier=3.0),
            fallback_candidates=(
                Candidate(
                    "ollama",
                    "qwen",
                    0.0,
                    Decision.YES,
                ),
            ),
        )

        response = runtime(
            {
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "request": {
                    "model": "gpt-4.1-mini",
                    "input": "hello",
                    "stream": False,
                },
            }
        )

        self.assertEqual([provider for provider, _ in calls], ["openai", "ollama"])
        self.assertEqual(calls[1][1]["model"], "qwen")
        self.assertEqual(response["provider"], "ollama")
        self.assertEqual(response["model"], "qwen")
        self.assertEqual(response["outputText"], "fallback ok")
        self.assertEqual(
            response["fallback"],
            {
                "attempted": True,
                "attemptCount": 2,
                "finalAction": "retry",
                "errorCode": "upstream_timeout",
                "retryable": True,
            },
        )
        self.assertEqual(response.get("tool"), {"observed": False})
        self.assertNotIn("fromProvider", response["fallback"])
        self.assertNotIn("toProvider", response["fallback"])
        self.assertNotIn("reason", response["fallback"])

    def test_runtime_reports_sanitized_fallback_summary_when_second_call_fails(
        self,
    ) -> None:
        from openusage_bar.gateway.runtime import GatewayRuntime

        calls: list[str] = []

        def egress(provider_id: str, _request_body: bytes, **_kwargs):
            calls.append(provider_id)
            if provider_id == "openai":
                raise GatewayProviderError("upstream_timeout", True)
            raise GatewayProviderError("upstream_server_error", False)

        runtime = GatewayRuntime(
            egress=egress,
            fallback_selector=FallbackSelector(cost_cap_multiplier=3.0),
            fallback_candidates=(
                Candidate("ollama", "qwen-private", 0.0, Decision.YES),
            ),
        )
        response = runtime(
            {
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "request": {
                    "model": "gpt-4.1-mini",
                    "input": SECRET,
                    "stream": False,
                },
            }
        )

        self.assertEqual(calls, ["openai", "ollama"])
        self.assertEqual(response["apiVersion"], "gateway.openusage/v1")
        self.assertEqual(response["object"], "gateway.error")
        self.assertEqual(
            response["error"],
            {
                "code": "fallback_exhausted",
                "message": "Provider request failed.",
                "retryable": False,
            },
        )
        self.assertEqual(
            response.get("fallback"),
            {
                "attempted": True,
                "attemptCount": 2,
                "finalAction": "fail",
                "errorCode": "fallback_exhausted",
                "retryable": False,
            },
        )
        serialized = json.dumps(response, sort_keys=True)
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn("qwen-private", serialized)
        self.assertNotIn("fromProvider", serialized)
        self.assertNotIn("toProvider", serialized)
        self.assertNotIn('"reason"', serialized)

    def test_runtime_sanitizes_non_provider_failure_during_fallback_call(
        self,
    ) -> None:
        from openusage_bar.gateway.runtime import GatewayRuntime

        calls: list[str] = []

        def egress(provider_id: str, _request_body: bytes, **_kwargs):
            calls.append(provider_id)
            if provider_id == "openai":
                raise GatewayProviderError("upstream_timeout", True)
            raise RuntimeError(SECRET)

        runtime = GatewayRuntime(
            egress=egress,
            fallback_selector=FallbackSelector(cost_cap_multiplier=3.0),
            fallback_candidates=(
                Candidate("ollama", "qwen-private", 0.0, Decision.YES),
            ),
        )
        response = runtime(
            {
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "request": {
                    "model": "gpt-4.1-mini",
                    "input": "hello",
                    "stream": False,
                },
            }
        )

        self.assertEqual(calls, ["openai", "ollama"])
        self.assertEqual(
            response,
            {
                "apiVersion": "gateway.openusage/v1",
                "object": "gateway.error",
                "error": {
                    "code": "gateway_unavailable",
                    "message": "Provider request failed.",
                    "retryable": True,
                },
                "fallback": {
                    "attempted": True,
                    "attemptCount": 2,
                    "finalAction": "fail",
                    "errorCode": "gateway_unavailable",
                    "retryable": True,
                },
            },
        )
        serialized = json.dumps(response, sort_keys=True)
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn("fallback_exhausted", serialized)
        self.assertNotIn("qwen-private", serialized)

    def test_runtime_does_not_fallback_after_terminal_provider_failure(self) -> None:
        from openusage_bar.gateway.runtime import GatewayRuntime

        calls: list[str] = []

        def egress(provider_id: str, _request_body: bytes, **_kwargs):
            calls.append(provider_id)
            raise GatewayProviderError("upstream_client_error", False)

        runtime = GatewayRuntime(
            egress=egress,
            fallback_selector=FallbackSelector(cost_cap_multiplier=3.0),
            fallback_candidates=(
                Candidate("ollama", "qwen", 0.0, Decision.YES),
            ),
        )

        response = runtime(
            {
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "request": {
                    "model": "gpt-4.1-mini",
                    "input": "hello",
                    "stream": False,
                },
            }
        )

        self.assertEqual(calls, ["openai"])
        self.assertEqual(response["object"], "gateway.error")
        self.assertEqual(response["error"]["code"], "upstream_client_error")

    def test_runtime_cache_lookup_failure_is_fail_open_for_provider_call(
        self,
    ) -> None:
        from openusage_bar.gateway.runtime import GatewayRuntime

        class BrokenCache:
            enabled = True

            def lookup(self, _keys):
                raise RuntimeError("corrupt cache")

            def store(self, _candidate):
                raise AssertionError("store should not run after lookup bypass")

        calls: list[str] = []

        def egress(provider_id: str, _request_body: bytes, **_kwargs):
            calls.append(provider_id)
            return ProviderResult(
                200,
                (("Content-Type", "text/event-stream"),),
                (
                    b"event: response.output_text.delta\n"
                    b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                    b"event: response.completed\n"
                    b'data: {"type":"response.completed","response":'
                    b'{"usage":{"input_tokens":1,"output_tokens":1}}}\n\n',
                ),
            )

        runtime = GatewayRuntime(
            egress=egress,
            cache=BrokenCache(),
            _unsafe_test_allow_cache_double=True,
        )

        response = runtime(
            {
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "request": {
                    "model": "gpt-4.1-mini",
                    "input": "hello",
                    "stream": True,
                },
            }
        )

        self.assertEqual(calls, ["openai"])
        self.assertEqual(response["object"], "gateway.response")
        self.assertEqual(response.get("tool"), {"observed": False})
        self.assertEqual(
            response["cache"],
            {"outcome": "bypass", "reason": "cache_unavailable"},
        )
        self.assertEqual(response.get("fallback"), NO_FALLBACK)


class GatewayRuntimeCompositionTests(unittest.TestCase):
    def test_default_gateway_factory_composes_proxy_only_for_gateway_proxy_mode(
        self,
    ) -> None:
        captured: list[object] = []

        def fake_server_factory(router, **_kwargs):
            captured.append(router)
            return object()

        with patch(
            "openusage_bar.gateway.server.create_gateway_server",
            side_effect=fake_server_factory,
        ):
            collector_cli._build_default_gateway_server(
                config=GatewayConfig(
                    enabled=True,
                    mode=GatewayMode.ADVISE,
                    proxy_enabled=False,
                ),
                token_path=Path("/tmp/openusage-gateway-token"),
                query=_Query(),
            )
            collector_cli._build_default_gateway_server(
                config=GatewayConfig(
                    enabled=True,
                    mode=GatewayMode.GATEWAY,
                    proxy_enabled=True,
                ),
                token_path=Path("/tmp/openusage-gateway-token"),
                query=_Query(),
            )

        self.assertEqual(len(captured), 2)
        advise_status, advise_payload = captured[0].dispatch(
            "GET", "/gateway/v1/health", b""
        )
        gateway_status, gateway_payload = captured[1].dispatch(
            "GET", "/gateway/v1/health", b""
        )
        self.assertEqual(advise_status, 200)
        self.assertEqual(gateway_status, 200)
        self.assertFalse(advise_payload["capabilities"]["responses"])
        self.assertTrue(gateway_payload["capabilities"]["responses"])

    def test_default_gateway_factory_creates_cache_only_when_effective(self) -> None:
        captured_runtime_kwargs: list[dict[str, object]] = []

        def fake_runtime(**kwargs):
            captured_runtime_kwargs.append(kwargs)

            def proxy(_payload):
                return {"apiVersion": "gateway.openusage/v1", "object": "gateway.response"}

            return proxy

        def fake_server_factory(router, **_kwargs):
            return router

        with tempfile.TemporaryDirectory() as directory, patch(
            "openusage_bar.gateway.runtime.GatewayRuntime",
            side_effect=fake_runtime,
        ), patch(
            "openusage_bar.gateway.server.create_gateway_server",
            side_effect=fake_server_factory,
        ), patch.object(
            collector_cli,
            "DEFAULT_GATEWAY_CACHE_PATH",
            Path(directory) / "gateway-cache.sqlite3",
        ):
            collector_cli._build_default_gateway_server(
                config=GatewayConfig(
                    enabled=True,
                    mode=GatewayMode.ADVISE,
                    cache_enabled=True,
                ),
                token_path=Path("/tmp/openusage-gateway-token"),
                query=_Query(),
            )
            collector_cli._build_default_gateway_server(
                config=GatewayConfig(
                    enabled=True,
                    mode=GatewayMode.GATEWAY,
                    proxy_enabled=True,
                    cache_enabled=False,
                ),
                token_path=Path("/tmp/openusage-gateway-token"),
                query=_Query(),
            )
            collector_cli._build_default_gateway_server(
                config=GatewayConfig(
                    enabled=True,
                    mode=GatewayMode.GATEWAY,
                    proxy_enabled=True,
                    cache_enabled=True,
                ),
                token_path=Path("/tmp/openusage-gateway-token"),
                query=_Query(),
            )
            cache_to_close = captured_runtime_kwargs[1]["cache"]
            if not isinstance(cache_to_close, SQLiteGatewayCache):
                raise AssertionError("effective Gateway cache was not created")
            cache_to_close.close()

        self.assertEqual(len(captured_runtime_kwargs), 2)
        self.assertIsNone(captured_runtime_kwargs[0]["cache"])
        self.assertIsInstance(
            captured_runtime_kwargs[1]["cache"],
            SQLiteGatewayCache,
        )


if __name__ == "__main__":
    unittest.main()
