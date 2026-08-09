# Gateway provider protocol research

Date: 2026-08-08

Scope: Task 7 adapter fixtures for OpenAI Responses API, Anthropic Messages API, DeepSeek OpenAI-compatible chat completions, OpenRouter chat completions, and Ollama `/api/chat`. This note is intentionally limited to provider protocol facts needed for offline conformance tests and adapter boundaries.

## Primary sources checked

- OpenAI official OpenAPI specification, `POST /responses`, response `application/json` or `text/event-stream`, and cURL example for `https://api.openai.com/v1/responses` with `Authorization: Bearer $OPENAI_API_KEY`: <https://raw.githubusercontent.com/openai/openai-openapi/master/openapi.yaml>
- OpenAI API reference landing page for Responses API: <https://platform.openai.com/docs/api-reference/responses/create>
- Anthropic official API docs for Messages and streaming: <https://docs.anthropic.com/en/api/messages>, <https://docs.anthropic.com/en/api/streaming>
- Anthropic official SDK generated from its OpenAPI spec, confirming `/v1/messages`, `max_tokens`, `messages`, `model`, and `stream`: <https://raw.githubusercontent.com/anthropics/anthropic-sdk-python/main/src/anthropic/resources/messages/messages.py>
- Anthropic official SDK client, confirming default base URL, auth headers, and `anthropic-version`: <https://raw.githubusercontent.com/anthropics/anthropic-sdk-python/main/src/anthropic/_client.py>
- DeepSeek official API docs, chat completions `POST /chat/completions`, stream as data-only SSE terminating with `data: [DONE]`: <https://api-docs.deepseek.com/api/create-chat-completion/>
- DeepSeek official getting-started docs, base URL and bearer auth example: <https://api-docs.deepseek.com/>
- OpenRouter official chat completion reference: <https://openrouter.ai/docs/api-reference/chat-completion>
- OpenRouter official quickstart / attribution docs: <https://openrouter.ai/docs/quickstart>
- Ollama official API docs, including `/api/chat`, streaming, and local HTTP API: <https://docs.ollama.com/api>
- Local code seam audited for Task 7: [openusage_bar/gateway/api.py](/Users/lune/Documents/Codex/2026-07-13/new-chat/work/openusage-bar-public-release/openusage_bar/gateway/api.py), [openusage_bar/network.py](/Users/lune/Documents/Codex/2026-07-13/new-chat/work/openusage-bar-public-release/openusage_bar/network.py), [openusage_bar/keychain.py](/Users/lune/Documents/Codex/2026-07-13/new-chat/work/openusage-bar-public-release/openusage_bar/keychain.py)

## Minimum protocol matrix

| Provider | Base URL / path | Auth | Minimum request | Non-stream response | Stream wire | Adapter constraints |
|---|---|---|---|---|---|---|
| OpenAI Responses | `https://api.openai.com/v1/responses` | `Authorization: Bearer <OPENAI_API_KEY>` | JSON object with `model` and `input`; optional `stream: true` | JSON `response` object | `text/event-stream`; OpenAPI schema names `ResponseStreamEvent` | Preserve Responses-specific `input`/`output` shape. Do not coerce into Chat Completions internally except in fixtures explicitly marked conversion tests. |
| Anthropic Messages | `https://api.anthropic.com/v1/messages` | `X-Api-Key: <ANTHROPIC_API_KEY>` per official SDK; official SDK also supports bearer token; include `anthropic-version: 2023-06-01` | JSON object with `model`, `max_tokens`, `messages`; optional `stream: true` | JSON `message` object | SSE message stream; official SDK type `RawMessageStreamEvent` | Preserve Anthropic block/event vocabulary. `max_tokens` is required by SDK method signature and must be fixture-tested. |
| DeepSeek Chat | `https://api.deepseek.com/chat/completions` | `Authorization: Bearer <DEEPSEEK_API_KEY>` | OpenAI-compatible JSON object with `model`, `messages`; optional `stream: true` | OpenAI-compatible chat completion JSON | Data-only SSE token deltas; terminates with `data: [DONE]` | Treat as OpenAI-compatible chat, not Responses. Keep DeepSeek model IDs and error bodies adapter-owned. |
| OpenRouter Chat | `https://openrouter.ai/api/v1/chat/completions` | `Authorization: Bearer <OPENROUTER_API_KEY>` | OpenAI-compatible JSON object with `model`, `messages`; optional `stream: true` | OpenAI-compatible chat completion JSON | Streaming chat completion chunks over SSE | Do not send optional attribution headers by default. `HTTP-Referer` and `X-Title` are opt-in product attribution only. |
| Ollama Chat | default local server `http://127.0.0.1:11434/api/chat` or `http://localhost:11434/api/chat` | No credential by default | JSON object with `model`, `messages`; optional `stream: false` for a single JSON response | JSON object with message/done fields | Default is newline-delimited JSON objects, not SSE | Allow only loopback HTTP. Never route Ollama through the cloud endpoint validator. No keychain lookup. |

## Canonical ingress and egress design

Gateway ingress can be shared across all adapters:

- Accept only a bounded JSON object from `/gateway/v1/responses`.
- Reject client-supplied `Authorization`, `Cookie`, `api_key`, `secret`, bearer-looking values, embedded credentials in URLs, and arbitrary outbound headers.
- Normalize a Gateway-internal request envelope with `provider`, `model`, `stream`, `body`, `timeout`, and stable request ID.
- Keep provider credentials out of the renderer, Local API, logs, telemetry, cache keys before hashing, and diagnostics.

Gateway egress can share these rules:

- Cloud providers use HTTPS public endpoints and must pass the existing `openusage_bar.network.validate_endpoint()` boundary.
- Credentials are loaded inside the Gateway process through `default_keychain()` only when the selected adapter requires one.
- Redirects should be disabled unless a provider-specific fixture proves a safe same-origin redirect is required. Current Task 7 recommendation: no redirects.
- Cap request body, response body, header count/size, stream idle time, and total request time. The existing `BoundedHTTPClient` is a useful starting point for non-stream JSON, but Task 7 needs an additional raw/stream transport because current helper returns JSON objects.
- Only allowlist response headers returned to Gateway API clients; never pass through `set-cookie`, `authorization`, provider request IDs if they may encode account identity, or backend-specific diagnostic URLs.

Adapter-owned differences:

- OpenAI Responses uses `input` and response `output` events/objects; do not force `messages`.
- Anthropic requires `max_tokens` and uses message content blocks and its own stream event names.
- DeepSeek and OpenRouter are OpenAI-compatible chat, but base URLs, model namespaces, and error bodies remain provider-owned.
- Ollama is loopback-only, no credential, and streams NDJSON rather than SSE.

## Error and status semantics for tests

Use provider fixtures to preserve raw upstream status class but sanitize public Gateway errors:

- 2xx non-stream: parse the provider-specific JSON shape into `ProviderResult`.
- 2xx stream: parse event/chunk framing and aggregate usage when present; interrupted streams are not cacheable.
- 4xx: classify as `upstream_client_error`, except recognized auth/rate-limit status can map to `provider_unavailable` or `upstream_rate_limited` if adapter tests prove it.
- 5xx/network timeout: classify as `upstream_server_error` or `upstream_timeout`.
- Invalid JSON, invalid SSE, invalid NDJSON, oversized headers/body, and unsupported content type: classify as `invalid_upstream_response`.
- Public Gateway response must never echo provider raw error text or credential-bearing request headers.

## Offline fixture recommendations

Create one fixture directory per provider under `tests/fixtures/gateway/<provider>/`:

- `request_minimal.json`: canonical Gateway ingress object.
- `upstream_request.json`: exact provider request body after adapter mapping.
- `upstream_response.json`: minimal non-stream success.
- `stream_success.txt`: exact stream wire. Use SSE for OpenAI/Anthropic/DeepSeek/OpenRouter; use NDJSON for Ollama.
- `error_401.json`, `error_429.json`, `error_500.json`: upstream raw bodies with safe synthetic text.
- `expected_public_response.json`: Gateway sanitized output.

Suggested tests:

- `test_cloud_adapters_reject_non_https_private_or_credentialed_endpoints`
- `test_ollama_accepts_only_loopback_http_and_reads_no_keychain`
- `test_openrouter_optional_attribution_headers_are_absent_by_default`
- `test_each_adapter_builds_only_provider_owned_auth_header`
- `test_stream_parser_accepts_provider_wire_and_rejects_mixed_framing`
- `test_upstream_errors_are_classified_without_echoing_raw_body`
- `test_renderer_and_local_api_never_receive_provider_headers`

## Known uncertainty / decisions to keep conservative

- Anthropic docs are dynamic; specific method/path/header facts above were cross-checked against Anthropic’s official SDK source generated from its OpenAPI spec. Keep a future docs-refresh task before provider release.
- OpenAI Responses stream event names evolve with the Responses schema. Tests should fixture accepted event classes from the official OpenAPI `ResponseStreamEvent`, but adapter code should ignore unknown safe event types rather than persist them raw.
- OpenRouter attribution headers are useful for public apps but are not required for Gateway correctness. Default should be absent; add only if the product owner explicitly opts in.
- Ollama host configuration must not accept arbitrary LAN/private hosts by default. If a user later needs a remote Ollama server, that should be a separate explicit trust setting, not the default adapter.
