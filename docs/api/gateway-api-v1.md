# OpenUsage Bar Gateway API v1

Gateway API v1 is an optional, loopback-only interface for admission advice
and explicitly submitted Provider requests. It is not part of the read-only
[Local API v1](local-api-v1.md), does not share that API's listener or bearer
token, and never changes Local API routes or semantics.

The committed route manifest is
[`openusage_bar/resources/gateway-api-v1.schema.json`](../../openusage_bar/resources/gateway-api-v1.schema.json).
Its API identifier is `gateway.openusage/v1`.

## Activation and modes

Fresh installs and upgrades remain in `observe` mode with the Gateway disabled.
This default does not open a Gateway listener, forward Provider requests, read
Provider credentials, or enable the Gateway cache. A user must explicitly opt
in before `advise` or `gateway` is enabled; installation or upgrade never makes
that choice on the user's behalf.

| Mode | Listener and behavior |
|---|---|
| `observe` | Default, disabled state. The observation product and Local API continue to work, but no Gateway listener is opened. |
| `advise` | Enables the Gateway listener and `POST /gateway/v1/should-send`. It never forwards a Provider request, and `POST /gateway/v1/responses` remains disabled. |
| `gateway` | Enables explicitly configured forwarding through `POST /gateway/v1/responses`; advice, cache, and fallback behavior remain subject to explicit configuration. |

## Compatible additive route surface

Gateway API v1 preserves the original four route semantics and adds two
renderer-safe optional GET projections for Account Pools and recent Decision
Trace entries. Existing request and response shapes remain unchanged:

| Method and path | Contract |
|---|---|
| `GET /gateway/v1/account-pools` | Returns configured local account aliases, derived display IDs, closed status/quota/cooldown facts, and Pool membership. It never returns account refs, credential lookup names, tokens, paths, headers, or raw Provider errors. |
| `GET /gateway/v1/decision-traces` | Returns at most 128 newest-first, process-lifetime routing facts from the active Gateway. It is read-only and never returns prompts, responses, credentials, raw Provider errors, private account IDs, paths, endpoints, or headers. |
| `GET /gateway/v1/health` | Returns sanitized Gateway capability and health state. Gateway health is not added to Local API health. |
| `GET /gateway/v1/schema` | Returns the committed v1 Gateway route manifest. It never advertises Local API routes. |
| `POST /gateway/v1/should-send` | Evaluates admission advice from recorded capacity facts and bounded aggregate telemetry. The request path does not invoke a live quota adapter or read a Provider credential. |
| `POST /gateway/v1/responses` | Accepts a Provider request only in explicitly enabled `gateway` mode and passes it through the configured Gateway pipeline. |

No other method or path is a Gateway API v1 route. In particular, Gateway API
v1 is not mounted below `/v1/*`, and the Local API listener never dispatches a
`/gateway/*` target. Adding the Account Pool and Decision Trace GETs does not
alter Local API v1, Should-Send, Responses, health, or schema request
semantics.

### Account Pool projection

`GET /gateway/v1/account-pools` is available only through the authenticated
private Gateway boundary. With no configured accounts it returns
`{"accounts":[]}`. A configured account is initially `unknown`; creating an
account or Pool never proves credential availability, account health, or quota.
The renderer receives only a user alias, a one-way derived display ID, closed
status/quota/cooldown facts, and bounded membership priority/weight.

The configuration keeps the existing provider default credential mapping when
no account is selected. An explicit selected account is resolved inside the
Python/OS credential boundary. Cross-provider, cross-model, and cross-region
fallback remain disabled unless each scope is explicitly confirmed by the
local Pool configuration.

### Decision Trace projection

`GET /gateway/v1/decision-traces` exposes the exact
`gateway-decision-trace.openusage/v1` projection. It contains no more than 128
entries in newest-first order. Each entry has exactly the following public
fields: an opaque trace ID, canonical UTC time, closed kind/execution/outcome
and reason values, optional public Pool revision and strategy, an optional
fixed Provider ID plus derived account display ID, bounded exclusions,
sanitized fallback facts, and an optional derived facts-window duration.

The three trace kinds preserve execution semantics. `route_advice` is always
`advice_only` and never claims a Provider request ran. `gateway_execution`
describes only an accepted non-streaming Gateway result. `pool_selection` is
recorded only when a caller explicitly uses a `PoolSelector` with the process
recorder; the first v1 slice does not imply that account-pool selection is
wired into every Gateway execution.

Decision Trace is bounded memory owned by the active Gateway process. It is
cleared when that process restarts and is not a durable audit history. Observe
mode does not start the default Gateway listener; an explicitly constructed
Observe router projects the exact empty root. Invalid or private fields fail
closed at the Python, Desktop, and Web boundaries rather than being reflected.

### Representation negotiation

`GET /gateway/v1/health` keeps its original `gateway.openusage/v1` JSON body
and byte shape when `Accept` is absent, generic, duplicated, or otherwise
ambiguous. A client receives the closed renderer-safe capability snapshot only
when it sends exactly one value:

```http
Accept: application/vnd.openusage.runtime-capability+json
```

That representation has `apiVersion` `runtime-capability.openusage/v1` and
`object` `runtime.capability`. Authentication and the normal request-boundary
checks run before representation selection, so negotiation never turns an
unauthenticated request into a capability probe. Health responses include
`Vary: Accept`; caches must therefore keep the legacy health document and the
runtime-capability representation separate.

`POST /gateway/v1/responses` similarly uses `Accept: text/event-stream` to
request the Gateway-native SSE representation. Its JSON and SSE responses also
include `Vary: Accept`. Provider-native headers, event names, and chunks are
never forwarded as the public Gateway stream contract.

## Should-Send wire contract

`POST /gateway/v1/should-send` preserves the snake-case contract from the
original Gateway plan. Its body is exactly one JSON object:

```json
{
  "provider": "openai",
  "model": "gpt-4o",
  "estimated_tokens": 8000,
  "window": "5m"
}
```

`estimated_tokens` is an integer from 1 through 2147483647. Unknown fields,
empty identifiers, non-object JSON, and non-standard JSON values are invalid.
The success shape is:

```json
{
  "decision": "defer",
  "confidence": 0.8,
  "reason": "quota_low",
  "defer_until": null,
  "details": {
    "quota_remaining": null,
    "burn_rate_per_min": 10.0,
    "predicted_exhaustion_minutes": null
  }
}
```

Decision inputs come only from recorded capacity facts and bounded aggregate
telemetry. `details.limit_per_window` may be added when that fact is actually
known; it is never synthesized. A non-null `predicted_exhaustion_minutes` is a
derived estimate available only when the remaining quota and observed burn use
compatible units. It is not a deadline or guarantee, and the Gateway never
divides a remaining ratio by a Token rate to fabricate it. Consumers must
tolerate additive reason values and detail fields.

`quota_remaining`, when present, is a remaining amount in the same quota unit as
the compatible burn-rate input. It is not the `remaining_ratio` field from the
capacity snapshot. If the unit cannot be proven compatible, both
`quota_remaining` and `predicted_exhaustion_minutes` remain `null` while
`burn_rate_per_min` may still be reported as bounded telemetry context.

The default local burn-rate input uses at most the newest 1,000 matching rows
from a closed five-minute window. No matching Token sample is `null`; a real
zero-Token aggregate is `0.0`. If the Gateway knows that overlapping telemetry
writes were skipped or could not be proven complete, every telemetry handle
for that database path treats the affected burn window as incomplete. Reads
check before and after SQLite access, so a racing gap also returns `null`
rather than a falsely precise partial rate. Telemetry contention fails quickly
instead of delaying or failing an otherwise safe Provider response.

## CLI control plane

The CLI exposes three local commands:

```text
openusage-bar gateway print-config
openusage-bar gateway status --config ~/.config/openusage-bar/gateway.json
openusage-bar gateway start --config ~/.config/openusage-bar/gateway.json
```

`print-config` emits a secret-free disabled template. `status` reports the
validated configuration and whether it is startable; it does not probe a
Provider, read credentials, create a token, or claim process liveness. `start`
opens the independent listener only for an explicitly enabled `advise` or
`gateway` configuration.

## Listener and authentication

When enabled, the Gateway runs as a separate process on its own IPv4 loopback
listener, bound exactly to `127.0.0.1`. It uses a dedicated bearer token that
is distinct from the Local API token. The listener validates its loopback
`Host` and authenticates a request before route handling. There is no remote
bind, unauthenticated mode, renderer-visible token, or TLS-termination contract
in v1.

Provider credentials remain inside the Python Gateway process and the
operating-system credential store. A Web or Electron renderer receives neither
the Gateway bearer token nor a Provider credential.

## Request bounds

Gateway requests use bounded HTTP/1.1 framing. A POST body must be one UTF-8
JSON object and is limited to 4 MiB. The server rejects an oversized body
before JSON parsing or Provider dispatch. Request lines, headers, admission
rate, read time, and total request time are also bounded; malformed or
ambiguous framing is rejected rather than forwarded. GET routes do not accept
a request body.

The body limit is an upper transport bound, not permission to persist the
body. Endpoint validation may impose smaller field or collection limits.

## Stable, sanitized errors

Gateway transport and router rejections use this envelope:

```json
{
  "error": {
    "code": "invalid_request",
    "message": "Invalid request.",
    "retryable": false
  }
}
```

`code` is a stable machine-readable string, `message` is a sanitized
human-readable string, and `retryable` states whether retrying the same logical
operation may succeed. The Gateway never echoes credentials, bearer values,
raw Provider errors, prompts, responses, chunks, local paths, direct account
identity, or internal exception text in an error. Consumers must branch on
`code`, not `message`, and must tolerate additive error codes and fields within
v1.

Authentication failures, disabled-mode routes, malformed JSON, body-limit
violations, admission rate limits, unsupported methods, and unknown routes
retain this unversioned envelope. They occur before a response request enters
the executable runtime and are not cacheable.

After `POST /gateway/v1/responses` has been accepted for execution, a Provider
or internal runtime failure uses the versioned `gateway.error` JSON/SSE
representation frozen by
[Gateway response contract v1](gateway-response-v1.md#accepted-runtime-json-error-object).
Both error domains use the same sanitized `code`, `message`, and `retryable`
detail fields, but clients must not confuse a pre-runtime transport rejection
with an accepted runtime result.

## Privacy and storage boundary

Gateway request content is request-local pipeline data; it is not a durable
observation fact. In particular:

- `activity.sqlite3` never stores prompts, responses, chunks, request IDs,
  cache keys, or per-request events. Only privacy-safe daily aggregates may be
  imported by the Collector.
- Bounded Gateway telemetry may store timestamps, a fixed Provider ID, a
  domain-separated SHA-256 model scope, Token counts, latency, status class,
  cost, cache outcome, fallback count, and sanitized error codes. The raw
  caller-supplied model label is transformed before it reaches the telemetry
  call or SQLite. Storage-version provenance distinguishes legacy raw labels
  from admitted anonymous scopes; a scope-looking caller value is still raw
  input. It does not store request or response content, credentials, cookies,
  direct identity, or raw Provider payloads.
- Gateway caching is opt-in and disabled by default. Cacheable entries must be
  deterministically redacted, bounded by TTL and size, and independently
  clearable. Unredacted, interrupted, tool-use, or uncertain-to-redact content
  is not cacheable.
- Gateway telemetry and cache stores are excluded from diagnostics, canary
  submissions, and release artifacts. They are local functional state, not
  remote analytics.

Gateway failure, restart, or refusal does not stop Collector refresh, Local API
reads, or the observation user interface. Routing applies only to requests
explicitly submitted to `POST /gateway/v1/responses`; observed quota is not a
global scheduling authorization.

## Performance evidence

Gateway API v1 performance evidence is documented in
[Gateway Performance Evidence](../gateway-performance.md). The authoritative
exact-cache release gate is the core cache lookup p99 < 5 ms. Authenticated
loopback cache-hit latency is informational-only diagnostic context. Should-Send
and cache latency summaries use nearest-rank p99, three rounds, and worst-round
selection; proxy overhead uses paired signed deltas; throughput counts only
terminal completions inside half-open one-second buckets. Late completions are
excluded and reported separately; they are not reclassified as request errors.

The dispatch-only manual CI job runs, verifies, and uploads a Gateway
performance report. Its absolute budgets are non-blocking because it does not
use `--enforce` on a shared runner. A report is release evidence only when it is
schema-valid, source-tree clean, and captured on an idle reference machine.
