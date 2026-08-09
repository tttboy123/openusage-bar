from __future__ import annotations

import json
import socket
from dataclasses import replace
from pathlib import Path
import unittest
import urllib.error

from openusage_bar.gateway import providers as provider_module
from openusage_bar.gateway.egress import execute_provider_call
from openusage_bar.gateway.accounts import ProviderAccountRef
from openusage_bar.gateway.ingress import GatewayRequest, parse_gateway_request
from openusage_bar.gateway.providers import (
    GatewayProviderError,
    ProviderResult,
    default_gateway_providers,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "gateway"
SECRET = "sk-test-secret-material"
PUBLIC_ADDRESS = "93.184.216.34"
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def public_resolver(_host: str) -> list[str]:
    return [PUBLIC_ADDRESS]


def fixture(provider_id: str) -> dict[str, object]:
    return json.loads(
        (FIXTURE_DIR / f"{provider_id}.json").read_text(encoding="utf-8")
    )


def request_bytes(provider_id: str, *, stream: bool | None = None) -> bytes:
    body = dict(fixture(provider_id)["request"])
    if stream is not None:
        body["stream"] = stream
    return json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class FakeTransport:
    def __init__(
        self,
        response: ProviderResult | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.response = response or ProviderResult(
            status_code=200,
            headers=(("Content-Type", "application/json"),),
            body_chunks=(b'{"object":"response","output":[]}',),
        )
        self.error = error
        self.calls: list[dict[str, object]] = []

    def send(
        self,
        *,
        method: str,
        endpoint: str,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
        max_response_header_count: int,
        max_response_header_bytes: int,
        max_response_bytes: int,
        max_response_chunks: int,
        allow_redirects: bool,
    ) -> ProviderResult:
        self.calls.append(
            {
                "method": method,
                "endpoint": endpoint,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
                "max_response_header_count": max_response_header_count,
                "max_response_header_bytes": max_response_header_bytes,
                "max_response_bytes": max_response_bytes,
                "max_response_chunks": max_response_chunks,
                "allow_redirects": allow_redirects,
            }
        )
        if self.error is not None:
            raise self.error
        return self.response


class FakeKeychain:
    def __init__(
        self,
        values: dict[str, str] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.values = values or {}
        self.error = error
        self.accounts: list[str] = []

    def get(self, account: str) -> str | None:
        self.accounts.append(account)
        if self.error is not None:
            raise self.error
        return self.values.get(account)


class GatewayIngressTests(unittest.TestCase):
    def test_exact_envelope_normalizes_without_mutating_provider_body(self) -> None:
        payload = {
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "request": {
                "model": "gpt-4.1-mini",
                "input": "hello",
            },
        }

        parsed = parse_gateway_request(payload)

        self.assertIsInstance(parsed, GatewayRequest)
        self.assertEqual(parsed.provider_id, "openai")
        self.assertEqual(parsed.model, "gpt-4.1-mini")
        self.assertFalse(parsed.stream)
        self.assertEqual(
            parsed.request_body,
            b'{"input":"hello","model":"gpt-4.1-mini"}',
        )
        self.assertNotIn("stream", payload["request"])

    def test_stream_defaults_follow_provider_wire_without_injecting_body(self) -> None:
        cases = {
            "openai": False,
            "anthropic": False,
            "deepseek": False,
            "openrouter": False,
            "ollama": True,
        }
        for provider_id, expected in cases.items():
            body = dict(fixture(provider_id)["request"])
            body.pop("stream", None)
            payload = {
                "provider": provider_id,
                "model": body["model"],
                "request": body,
            }
            with self.subTest(provider=provider_id):
                parsed = parse_gateway_request(payload)
                self.assertIs(parsed.stream, expected)
                self.assertNotIn("stream", json.loads(parsed.request_body))

    def test_explicit_stream_must_be_boolean_and_is_preserved(self) -> None:
        for value in (None, 0, 1, "true", [], {}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "^invalid gateway request$"):
                    parse_gateway_request(
                        {
                            "provider": "openai",
                            "model": "gpt-4.1-mini",
                            "request": {
                                "model": "gpt-4.1-mini",
                                "input": "hello",
                                "stream": value,
                            },
                        }
                    )

        parsed = parse_gateway_request(
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
        self.assertTrue(parsed.stream)
        self.assertIs(json.loads(parsed.request_body)["stream"], True)

    def test_provider_set_model_match_and_exact_fields_are_enforced(self) -> None:
        invalid_payloads = (
            {},
            {"provider": [], "model": "m", "request": {"model": "m"}},
            {"provider": "openai", "model": "m", "request": {}, "extra": 1},
            {"provider": "custom", "model": "m", "request": {"model": "m"}},
            {"provider": "openai", "model": "m", "request": {"model": "other"}},
            {"provider": "openai", "model": "", "request": {"model": ""}},
            {
                "provider": "openai",
                "model": "bad\nmodel",
                "request": {"model": "bad\nmodel", "input": "hello"},
            },
            {
                "provider": "openai",
                "model": "m" * 257,
                "request": {"model": "m" * 257, "input": "hello"},
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, "^invalid gateway request$"):
                    parse_gateway_request(payload)

    def test_client_cannot_supply_outbound_or_credential_fields(self) -> None:
        forbidden = (
            "endpoint",
            "headers",
            "header",
            "authorization",
            "cookie",
            "api_key",
            "apiKey",
            "x-api-key",
            "access_token",
            "proxy-authorization",
            "sessionCookie",
            "credential",
            "secret",
        )
        for field in forbidden:
            payload = {
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "request": {
                    "model": "gpt-4.1-mini",
                    "input": "hello",
                    field: "client-controlled",
                },
            }
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "^invalid gateway request$"):
                    parse_gateway_request(payload)

    def test_nested_tool_schema_and_prompt_text_are_not_false_positive_secrets(self) -> None:
        parsed = parse_gateway_request(
            {
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "request": {
                    "model": "gpt-4.1-mini",
                    "input": "Explain the literal text Bearer example and token budget.",
                    "tools": [
                        {
                            "type": "function",
                            "name": "lookup",
                            "parameters": {
                                "type": "object",
                                "properties": {"api_key": {"type": "string"}},
                            },
                        }
                    ],
                },
            }
        )
        self.assertIn("Bearer example", parsed.request_body.decode("utf-8"))
        self.assertIn('"api_key"', parsed.request_body.decode("utf-8"))

    def test_canonical_request_is_finite_json_and_bounded(self) -> None:
        invalid_requests = (
            {"model": "gpt-4.1-mini", "input": float("nan")},
            {"model": "gpt-4.1-mini", "input": b"bytes"},
            {"model": "gpt-4.1-mini", "input": "x" * MAX_REQUEST_BYTES},
        )
        for request in invalid_requests:
            with self.subTest(kind=type(request["input"]).__name__):
                with self.assertRaisesRegex(ValueError, "^invalid gateway request$"):
                    parse_gateway_request(
                        {
                            "provider": "openai",
                            "model": "gpt-4.1-mini",
                            "request": request,
                        }
                    )


class GatewayProviderTests(unittest.TestCase):
    def providers(
        self,
        transport: FakeTransport | None = None,
        resolver=public_resolver,
    ):
        return default_gateway_providers(
            transport=transport or FakeTransport(),
            resolver=resolver,
        )

    def provider(self, provider_id: str, transport: FakeTransport | None = None):
        return {
            item.provider_id: item for item in self.providers(transport)
        }[provider_id]

    def assert_provider_error(
        self,
        expected_code: str,
        retryable: bool,
        operation,
    ) -> GatewayProviderError:
        with self.assertRaises(GatewayProviderError) as caught:
            operation()
        error = caught.exception
        self.assertEqual(error.code, expected_code)
        self.assertIs(error.retryable, retryable)
        self.assertEqual(str(error), "provider request failed")
        serialized = f"{error!s} {error!r}"
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn("api.openai.com", serialized)
        self.assertNotIn("/Users/", serialized)
        return error

    def test_v1_provider_set_and_public_descriptors_are_exact_and_secret_free(self) -> None:
        providers = self.providers()
        self.assertEqual(
            sorted(provider.provider_id for provider in providers),
            ["anthropic", "deepseek", "ollama", "openai", "openrouter"],
        )
        expected = {
            "openai": ("OpenAI", "responses", False),
            "anthropic": ("Anthropic", "messages", False),
            "deepseek": ("DeepSeek", "chat-completions", False),
            "openrouter": ("OpenRouter", "chat-completions", False),
            "ollama": ("Ollama", "ollama-chat", True),
        }
        for provider in providers:
            display_name, api_family, local = expected[provider.provider_id]
            descriptor = provider.public_descriptor()
            self.assertEqual(
                descriptor,
                {
                    "providerId": provider.provider_id,
                    "displayName": display_name,
                    "apiFamily": api_family,
                    "streaming": True,
                    "local": local,
                },
            )
            serialized = json.dumps(descriptor, sort_keys=True).casefold()
            for forbidden in (
                "credential",
                "token",
                "secret",
                "account",
                "cookie",
                "authorization",
                "endpoint",
                "header",
            ):
                self.assertNotIn(forbidden, serialized)

    def test_five_fixtures_are_minimal_sanitized_and_match_fixed_endpoints(self) -> None:
        documents = {
            path.stem: json.loads(path.read_text(encoding="utf-8"))
            for path in FIXTURE_DIR.glob("*.json")
        }
        fixtures = {
            name: value
            for name, value in documents.items()
            if "provider" in value
        }
        self.assertEqual(
            set(fixtures),
            {"anthropic", "deepseek", "ollama", "openai", "openrouter"},
        )
        providers = {item.provider_id: item for item in self.providers()}
        expected_endpoints = {
            "openai": "https://api.openai.com/v1/responses",
            "anthropic": "https://api.anthropic.com/v1/messages",
            "deepseek": "https://api.deepseek.com/chat/completions",
            "openrouter": "https://openrouter.ai/api/v1/chat/completions",
            "ollama": "http://127.0.0.1:11434/api/chat",
        }
        for provider_id, value in fixtures.items():
            with self.subTest(provider=provider_id):
                self.assertEqual(value["provider"], provider_id)
                self.assertEqual(value["endpoint"], expected_endpoints[provider_id])
                self.assertEqual(providers[provider_id].endpoint, value["endpoint"])
                self.assertIn("model", value["request"])
                self.assertEqual(
                    value["stream"]["wire"],
                    "ndjson" if provider_id == "ollama" else "sse",
                )
                serialized = json.dumps(value, sort_keys=True).casefold()
                for forbidden in ("sk-", "bearer ", "api_key", "cookie", "secret"):
                    self.assertNotIn(forbidden, serialized)
        self.assertIn("input", fixtures["openai"]["request"])
        self.assertIn("max_tokens", fixtures["anthropic"]["request"])
        for provider_id in ("deepseek", "openrouter", "ollama"):
            self.assertIn("messages", fixtures[provider_id]["request"])

    def test_each_adapter_uses_fixed_endpoint_owned_auth_and_raw_stream_chunks(self) -> None:
        credentials = {
            "openai": SECRET,
            "anthropic": SECRET,
            "deepseek": SECRET,
            "openrouter": SECRET,
            "ollama": "",
        }
        expected_auth = {
            "openai": ("Authorization", f"Bearer {SECRET}"),
            "anthropic": ("x-api-key", SECRET),
            "deepseek": ("Authorization", f"Bearer {SECRET}"),
            "openrouter": ("Authorization", f"Bearer {SECRET}"),
            "ollama": None,
        }
        for provider_id in credentials:
            data = fixture(provider_id)
            media_type = data["stream"]["content_type"]
            chunks = tuple(
                item.encode("utf-8") for item in data["stream"]["chunks"]
            )
            transport = FakeTransport(
                ProviderResult(
                    200,
                    (
                        ("Content-Type", media_type),
                        ("Set-Cookie", "private=value"),
                        ("Authorization", "private"),
                        ("X-Request-Id", "account-correlated"),
                        ("X-RateLimit-Remaining", "2"),
                    ),
                    chunks,
                )
            )
            provider = self.provider(provider_id, transport)
            body = request_bytes(provider_id, stream=True)

            result = provider.call(body, credential=credentials[provider_id])

            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.headers, (("content-type", media_type),))
            self.assertEqual(result.body_chunks, chunks)
            self.assertEqual(len(transport.calls), 1)
            call = transport.calls[0]
            self.assertEqual(call["method"], "POST")
            self.assertEqual(call["endpoint"], data["endpoint"])
            self.assertEqual(call["body"], body)
            headers = call["headers"]
            self.assertEqual(headers["Content-Type"], "application/json")
            self.assertEqual(
                headers["Accept"],
                "application/x-ndjson" if provider_id == "ollama" else "text/event-stream",
            )
            owned_auth = expected_auth[provider_id]
            if owned_auth is None:
                self.assertNotIn("Authorization", headers)
                self.assertNotIn("x-api-key", headers)
            else:
                self.assertEqual(headers[owned_auth[0]], owned_auth[1])
            if provider_id == "anthropic":
                self.assertEqual(headers["anthropic-version"], "2023-06-01")
            self.assertNotIn("HTTP-Referer", headers)
            self.assertNotIn("X-Title", headers)
            self.assertNotIn(SECRET.encode(), call["body"])
            self.assertNotIn(SECRET, repr(result))

    def test_non_stream_json_is_validated_and_preserved(self) -> None:
        data = fixture("openai")
        encoded = json.dumps(
            data["response"], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        transport = FakeTransport(
            ProviderResult(
                200,
                (("Content-Type", "application/json; charset=utf-8"),),
                (encoded[:7], encoded[7:]),
            )
        )
        provider = self.provider("openai", transport)

        result = provider.call(request_bytes("openai", stream=False), credential=SECRET)

        self.assertEqual(result.body_chunks, (encoded[:7], encoded[7:]))
        self.assertEqual(
            result.headers,
            (("content-type", "application/json; charset=utf-8"),),
        )
        self.assertEqual(transport.calls[0]["headers"]["Accept"], "application/json")

    def test_each_provider_validates_its_non_stream_response_shape(self) -> None:
        for provider_id in ("openai", "anthropic", "deepseek", "openrouter", "ollama"):
            data = fixture(provider_id)
            encoded = json.dumps(
                data["response"], sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            transport = FakeTransport(
                ProviderResult(
                    200,
                    (("Content-Type", "application/json"),),
                    (encoded,),
                )
            )
            provider = self.provider(provider_id, transport)
            credential = "" if provider_id == "ollama" else SECRET
            with self.subTest(provider=provider_id):
                result = provider.call(
                    request_bytes(provider_id, stream=False),
                    credential=credential,
                )
                self.assertEqual(result.body_chunks, (encoded,))

                transport.response = ProviderResult(
                    200,
                    (("Content-Type", "application/json"),),
                    (b"{}",),
                )
                self.assert_provider_error(
                    "invalid_upstream_response",
                    False,
                    lambda provider=provider, provider_id=provider_id, credential=credential: provider.call(
                        request_bytes(provider_id, stream=False),
                        credential=credential,
                    ),
                )

    def test_transport_bounds_and_no_redirect_contract_are_fixed(self) -> None:
        transport = FakeTransport()
        self.provider("openai", transport).call(
            request_bytes("openai"), credential=SECRET
        )
        call = transport.calls[0]
        self.assertEqual(call["timeout_seconds"], 15.0)
        self.assertEqual(call["max_response_header_count"], 64)
        self.assertEqual(call["max_response_header_bytes"], 64 * 1024)
        self.assertEqual(call["max_response_bytes"], MAX_RESPONSE_BYTES)
        self.assertEqual(call["max_response_chunks"], 4096)
        self.assertIs(call["allow_redirects"], False)

    def test_remote_endpoints_fail_closed_before_transport(self) -> None:
        transport = FakeTransport()
        base = self.provider("openai", transport)
        cases = (
            replace(base, endpoint="http://api.openai.com/v1/responses"),
            replace(base, endpoint="https://user:pass@api.openai.com/v1/responses"),
            replace(base, endpoint="https://api.openai.com/v1/responses#fragment"),
            replace(base, resolver=lambda _host: ["127.0.0.1"]),
            replace(base, resolver=lambda _host: ["169.254.169.254"]),
        )
        for provider in cases:
            with self.subTest(endpoint=provider.endpoint):
                self.assert_provider_error(
                    "unsafe_endpoint",
                    False,
                    lambda provider=provider: provider.call(
                        request_bytes("openai"), credential=SECRET
                    ),
                )
        self.assertEqual(transport.calls, [])

    def test_ollama_is_exact_ipv4_loopback_and_rejects_any_credential(self) -> None:
        transport = FakeTransport()
        base = self.provider("ollama", transport)
        unsafe_endpoints = (
            "http://localhost:11434/api/chat",
            "http://[::1]:11434/api/chat",
            "http://127.0.0.1:11435/api/chat",
            "http://192.168.1.10:11434/api/chat",
            "https://127.0.0.1:11434/api/chat",
            "http://user:pass@127.0.0.1:11434/api/chat",
            "http://127.0.0.1:11434/api/chat#fragment",
        )
        for endpoint in unsafe_endpoints:
            provider = replace(base, endpoint=endpoint)
            with self.subTest(endpoint=endpoint):
                self.assert_provider_error(
                    "unsafe_endpoint",
                    False,
                    lambda provider=provider: provider.call(
                        request_bytes("ollama"), credential=""
                    ),
                )
        self.assert_provider_error(
            "invalid_request",
            False,
            lambda: base.call(request_bytes("ollama"), credential=SECRET),
        )
        self.assertEqual(transport.calls, [])

    def test_provider_specific_minimum_request_shapes_are_enforced(self) -> None:
        cases = {
            "openai": {"model": "gpt-4.1-mini"},
            "anthropic": {"model": "claude-sonnet-4-5", "messages": []},
            "deepseek": {"model": "deepseek-chat"},
            "openrouter": {"model": "openai/gpt-4.1-mini"},
            "ollama": {"model": "llama3.2"},
        }
        for provider_id, body in cases.items():
            provider = self.provider(provider_id)
            credential = "" if provider_id == "ollama" else SECRET
            with self.subTest(provider=provider_id):
                self.assert_provider_error(
                    "invalid_request",
                    False,
                    lambda provider=provider, body=body, credential=credential: provider.call(
                        json.dumps(body).encode("utf-8"), credential=credential
                    ),
                )

    def test_request_and_upstream_response_bounds_fail_closed(self) -> None:
        provider = self.provider("openai")
        self.assert_provider_error(
            "invalid_request",
            False,
            lambda: provider.call(b"{" + b"x" * MAX_REQUEST_BYTES, credential=SECRET),
        )

        responses = (
            ProviderResult(
                200,
                (("Content-Type", "application/json"),),
                (b"x" * (MAX_RESPONSE_BYTES + 1),),
            ),
            ProviderResult(
                200,
                tuple((f"X-{index}", "v") for index in range(65)),
                (b"{}",),
            ),
            ProviderResult(
                200,
                (("Content-Type", "application/json"), ("X-Large", "x" * 65536)),
                (b"{}",),
            ),
            ProviderResult(
                200,
                (("Content-Type", "text/event-stream"),),
                tuple(b"x" for _ in range(4097)),
            ),
        )
        for response in responses:
            transport = FakeTransport(response)
            provider = self.provider("openai", transport)
            body = request_bytes(
                "openai",
                stream=response.headers[0][1] == "text/event-stream",
            )
            with self.subTest(headers=len(response.headers), chunks=len(response.body_chunks)):
                self.assert_provider_error(
                    "invalid_upstream_response",
                    False,
                    lambda provider=provider, body=body: provider.call(
                        body, credential=SECRET
                    ),
                )

    def test_content_type_and_non_stream_json_are_validated(self) -> None:
        responses = (
            (False, ProviderResult(200, (), (b"{}",))),
            (False, ProviderResult(200, (("Content-Type", "text/plain"),), (b"{}",))),
            (False, ProviderResult(200, (("Content-Type", "application/json"),), (b"not-json",))),
            (False, ProviderResult(200, (("Content-Type", "application/json"),), (b"[]",))),
            (True, ProviderResult(200, (("Content-Type", "application/json"),), (b"{}",))),
        )
        for stream, response in responses:
            transport = FakeTransport(response)
            provider = self.provider("openai", transport)
            with self.subTest(stream=stream, headers=response.headers):
                self.assert_provider_error(
                    "invalid_upstream_response",
                    False,
                    lambda provider=provider, stream=stream: provider.call(
                        request_bytes("openai", stream=stream), credential=SECRET
                    ),
                )

    def test_stream_framing_rejects_empty_garbage_and_mixed_protocols(self) -> None:
        cases = (
            ("openai", "text/event-stream", b""),
            ("openai", "text/event-stream", b"not-sse-at-all"),
            ("openai", "text/event-stream", b"data: not-json\n\n"),
            ("openai", "text/event-stream", b'data: {"type":"truncated"}'),
            ("openai", "text/event-stream", b'{"done":true}\n'),
            (
                "openai",
                "text/event-stream",
                b'data: {"type":"ok"}\n\n{"done":true}\n',
            ),
            ("ollama", "application/x-ndjson", b"not-json\n"),
            ("ollama", "application/x-ndjson", b"data: {}\n\n"),
        )
        for provider_id, content_type, body in cases:
            transport = FakeTransport(
                ProviderResult(
                    200,
                    (("Content-Type", content_type),),
                    (body,),
                )
            )
            provider = self.provider(provider_id, transport)
            credential = "" if provider_id == "ollama" else SECRET
            with self.subTest(provider=provider_id, body=body):
                self.assert_provider_error(
                    "invalid_upstream_response",
                    False,
                    lambda provider=provider, provider_id=provider_id, credential=credential: provider.call(
                        request_bytes(provider_id, stream=True),
                        credential=credential,
                    ),
                )

    def test_ollama_native_default_requires_ndjson_not_json_fallback(self) -> None:
        transport = FakeTransport(
            ProviderResult(
                200,
                (("Content-Type", "application/json"),),
                (json.dumps(fixture("ollama")["response"]).encode("utf-8"),),
            )
        )
        provider = self.provider("ollama", transport)
        self.assert_provider_error(
            "invalid_upstream_response",
            False,
            lambda: provider.call(request_bytes("ollama"), credential=""),
        )

    def test_upstream_statuses_and_transport_failures_are_sanitized(self) -> None:
        status_cases = (
            (302, "upstream_client_error", False),
            (400, "upstream_client_error", False),
            (401, "upstream_authentication_failed", False),
            (403, "upstream_authentication_failed", False),
            (429, "upstream_rate_limited", True),
            (500, "upstream_server_error", True),
            (503, "upstream_server_error", True),
        )
        for status, code, retryable in status_cases:
            transport = FakeTransport(
                ProviderResult(
                    status,
                    (("Content-Type", "application/json"),),
                    (f'{{"error":"{SECRET} raw provider failure"}}'.encode(),),
                )
            )
            provider = self.provider("openai", transport)
            with self.subTest(status=status):
                self.assert_provider_error(
                    code,
                    retryable,
                    lambda provider=provider: provider.call(
                        request_bytes("openai"), credential=SECRET
                    ),
                )

        failure_cases = (
            (TimeoutError(f"timeout {SECRET}"), "upstream_timeout"),
            (
                urllib.error.URLError(socket.timeout(f"timeout {SECRET}")),
                "upstream_timeout",
            ),
            (OSError(f"socket {SECRET}"), "upstream_unavailable"),
        )
        for error, code in failure_cases:
            provider = self.provider("openai", FakeTransport(error=error))
            with self.subTest(code=code):
                self.assert_provider_error(
                    code,
                    True,
                    lambda provider=provider: provider.call(
                        request_bytes("openai"), credential=SECRET
                    ),
                )

    def test_total_deadline_is_checked_after_eof_read(self) -> None:
        class Clock:
            def __init__(self) -> None:
                self.values = iter((0.0, 0.0, 16.0))

            def __call__(self) -> float:
                return next(self.values)

        class Response:
            status = 200
            headers = {"Content-Type": "application/json"}
            fp = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size: int) -> bytes:
                return b""

        class Opener:
            def open(self, _request, timeout: float):
                self.timeout = timeout
                return Response()

        transport = provider_module._UrllibProviderTransport(
            opener=Opener(),
            monotonic=Clock(),
        )
        provider = {
            item.provider_id: item
            for item in default_gateway_providers(
                transport=transport,
                resolver=public_resolver,
            )
        }["openai"]

        self.assert_provider_error(
            "upstream_timeout",
            True,
            lambda: provider.call(request_bytes("openai"), credential=SECRET),
        )


class GatewayEgressTests(unittest.TestCase):
    def providers(self, transport: FakeTransport):
        return default_gateway_providers(
            transport=transport,
            resolver=public_resolver,
        )

    def test_egress_is_the_only_keychain_read_boundary(self) -> None:
        cases = ("openai", "anthropic", "deepseek", "openrouter")
        for provider_id in cases:
            encoded = json.dumps(
                fixture(provider_id)["response"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            transport = FakeTransport(
                ProviderResult(
                    200,
                    (("Content-Type", "application/json"),),
                    (encoded,),
                )
            )
            keychain = FakeKeychain({f"{provider_id}.gateway-api-key": SECRET})
            result = execute_provider_call(
                provider_id,
                request_bytes(provider_id),
                providers=self.providers(transport),
                keychain=keychain,
            )
            with self.subTest(provider=provider_id):
                self.assertEqual(
                    keychain.accounts, [f"{provider_id}.gateway-api-key"]
                )
                self.assertEqual(result.status_code, 200)
                self.assertNotIn(SECRET, repr(result))

    def test_selected_account_uses_its_private_credential_lookup(self) -> None:
        encoded = json.dumps(
            fixture("openai")["response"],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        transport = FakeTransport(
            ProviderResult(
                200,
                (("Content-Type", "application/json"),),
                (encoded,),
            )
        )
        selected = ProviderAccountRef(
            provider_id="openai",
            account_id="work",
            alias="Work",
            credential_account="openai.work.gateway-api-key",
        )
        keychain = FakeKeychain(
            {
                "openai.gateway-api-key": "wrong-default",
                "openai.work.gateway-api-key": SECRET,
            }
        )

        result = execute_provider_call(
            "openai",
            request_bytes("openai"),
            providers=self.providers(transport),
            keychain=keychain,
            account=selected,
        )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(keychain.accounts, ["openai.work.gateway-api-key"])
        self.assertNotIn("openai.work.gateway-api-key", repr(result))

    def test_selected_account_cannot_cross_the_requested_provider(self) -> None:
        selected = ProviderAccountRef(
            provider_id="anthropic",
            account_id="work",
            alias="Work",
            credential_account="anthropic.work.gateway-api-key",
        )
        keychain = FakeKeychain(
            {"anthropic.work.gateway-api-key": SECRET}
        )

        with self.assertRaises(GatewayProviderError) as caught:
            execute_provider_call(
                "openai",
                request_bytes("openai"),
                providers=self.providers(FakeTransport()),
                keychain=keychain,
                account=selected,
            )

        self.assertEqual(caught.exception.code, "unsupported_provider")
        self.assertEqual(keychain.accounts, [])

    def test_ollama_never_reads_keychain(self) -> None:
        transport = FakeTransport(
            ProviderResult(
                200,
                (("Content-Type", "application/json"),),
                (json.dumps(fixture("ollama")["response"]).encode("utf-8"),),
            )
        )
        keychain = FakeKeychain(error=AssertionError("must not read"))

        execute_provider_call(
            "ollama",
            request_bytes("ollama", stream=False),
            providers=self.providers(transport),
            keychain=keychain,
        )

        self.assertEqual(keychain.accounts, [])
        self.assertNotIn("Authorization", transport.calls[0]["headers"])

    def test_injected_provider_cannot_select_an_unknown_keychain_account(self) -> None:
        class InjectedProvider:
            def __init__(self, provider_id: str, account: str | None) -> None:
                self.provider_id = provider_id
                self.credential_account = account

            def call(self, request_body: bytes, *, credential: str) -> ProviderResult:
                raise AssertionError((request_body, credential))

        cases = (
            ("custom", "victim-account"),
            ("openai", "victim-account"),
            ("ollama", "victim-account"),
        )
        for provider_id, account in cases:
            keychain = FakeKeychain({"victim-account": SECRET})
            with self.subTest(provider=provider_id):
                with self.assertRaises(GatewayProviderError) as caught:
                    execute_provider_call(
                        provider_id,
                        request_bytes("openai"),
                        providers=(InjectedProvider(provider_id, account),),
                        keychain=keychain,
                    )
                self.assertEqual(caught.exception.code, "unsupported_provider")
                self.assertEqual(keychain.accounts, [])

    def test_non_bytes_request_is_invalid_without_keychain_access(self) -> None:
        keychain = FakeKeychain({"openai.gateway-api-key": SECRET})
        with self.assertRaises(GatewayProviderError) as caught:
            execute_provider_call(
                "openai",
                "{}",
                providers=(),
                keychain=keychain,
            )
        self.assertEqual(caught.exception.code, "invalid_request")
        self.assertEqual(keychain.accounts, [])

    def test_missing_keychain_failure_and_unknown_provider_are_sanitized(self) -> None:
        transport = FakeTransport()
        providers = self.providers(transport)
        cases = (
            ("openai", FakeKeychain(), "credential_unavailable"),
            ("openai", FakeKeychain(error=RuntimeError(SECRET)), "credential_unavailable"),
            ("unknown", FakeKeychain({"unknown.gateway-api-key": SECRET}), "unsupported_provider"),
        )
        for provider_id, keychain, code in cases:
            with self.subTest(provider=provider_id, code=code):
                with self.assertRaises(GatewayProviderError) as caught:
                    execute_provider_call(
                        provider_id,
                        request_bytes("openai"),
                        providers=providers,
                        keychain=keychain,
                    )
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(str(caught.exception), "provider request failed")
                self.assertNotIn(SECRET, repr(caught.exception))
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
