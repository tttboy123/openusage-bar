# OpenUsage Bar Gateway response contract v1

This document freezes the Gateway-native response emitted by
`POST /gateway/v1/responses`. The machine-readable contract is
[`gateway-response-v1.schema.json`](../../openusage_bar/resources/gateway-response-v1.schema.json).
It covers both the bounded JSON result and the `data` object of every
Gateway-native Server-Sent Event (SSE).

The contract identifier is `gateway.openusage/v1`. It is shared by OpenAI,
Anthropic, DeepSeek, OpenRouter, and Ollama. Provider-native response headers,
event names, identifiers, and bodies are never part of this public contract.

This response contract begins only after the Gateway API transport/router has
accepted a request for response execution. It does not replace the generic
problem JSON frozen by [Gateway API v1](gateway-api-v1.md). Listener, Host,
authentication, HTTP framing, body-limit, route, mode, and proxy-disabled
failures continue to return the unversioned
`{"error":{"code":"invalid_request","message":"Invalid request.","retryable":false}}`
shape as bounded JSON. They do not switch to SSE, even when the request
advertises `Accept: text/event-stream`.

## Compatibility rules

- Existing `gateway.response` JSON fields keep their names and meanings.
  `tool` and the complete `fallback` summary are required additions before the
  executable pipeline can claim this contract.
- Success and error objects are discriminated by `object`. SSE event data is
  discriminated by `type`.
- Producers validate at the HTTP boundary and emit no properties outside the
  schema. Every object has `additionalProperties: false` to make accidental
  disclosure a contract failure.
- Fields cannot be removed, renamed, or assigned a different type within v1.
  New optional fields require a contract-first schema revision. New event,
  Provider, status, cache-outcome, or fallback-action values require a new
  compatible contract revision before a producer emits them.
- Error `code`, cache `reason`, and fallback `errorCode` are deliberately open
  stable-code spaces matching `^[a-z][a-z0-9_]{0,63}$`. Consumers branch on a
  code, never on human-readable `message`, and tolerate previously unknown
  valid codes.
- JSON object-property order is not meaningful. SSE event order and `sequence`
  values are meaningful and are specified below.

## JSON success object

A successful bounded JSON response has exactly these required fields:

```json
{
  "apiVersion": "gateway.openusage/v1",
  "object": "gateway.response",
  "provider": "openai",
  "model": "gpt-4.1-mini",
  "stream": false,
  "status": "complete",
  "outputText": "Hello.",
  "usage": {
    "inputTokens": 8,
    "outputTokens": 2
  },
  "tool": {
    "observed": false
  },
  "cache": {
    "outcome": "miss"
  },
  "fallback": {
    "attempted": false,
    "attemptCount": 1,
    "finalAction": "none"
  }
}
```

Field semantics:

| Field | Meaning |
|---|---|
| `provider` | The effective Provider that actually produced the returned output: `openai`, `anthropic`, `deepseek`, `openrouter`, or `ollama`. It equals the accepted primary unless a permitted fallback call produced the final result. |
| `model` | The effective model that actually produced the returned output, bounded to 256 non-control characters. It equals the accepted model unless a permitted fallback call produced the final result. |
| `stream` | Whether the accepted Provider request was a streaming request. A client that asks for bounded JSON may receive an aggregated JSON object with `stream: true`. |
| `status` | `complete`, `interrupted`, or `uncertain`. Only `complete` is eligible for cache admission, and tool-observed output is still ineligible. |
| `outputText` | The text intentionally returned to the caller: either Provider output as received or text rehydrated using only this request's redaction map. This field is **not safe to log, persist, index, include in diagnostics, or send as telemetry**. |
| `usage` | A Provider-reported snapshot with required `inputTokens` and `outputTokens`, each a non-negative integer or `null`; the whole value is `null` when no trustworthy snapshot exists. Counts are never estimated to fill missing Provider data. |
| `tool.observed` | `true` once any tool call or equivalent side-effect-bearing control is observed. Tool arguments, names, call IDs, and results are not public fields. |
| `cache` | The final cache outcome defined below. |
| `fallback` | The final fallback summary defined below. |

`interrupted` means the accepted stream did not finish because transport was
closed or aborted. `uncertain` means the Gateway cannot prove a valid Provider
terminal state, including a missing Provider terminal event, malformed or
truncated upstream framing, or a size/event/deadline limit. Neither status can
be cached or replayed as a successful answer.

## Cache summary

`cache.outcome` is one of:

| Outcome | Additional fields and behavior |
|---|---|
| `disabled` | Cache was not enabled. No `reason` or `hintDepth`. |
| `bypass` | Request was deliberately not looked up or stored. A bounded stable `reason` is required. |
| `miss` | No exact or prefix entry matched. No `reason` or `hintDepth`. |
| `exact_hit` | A safe exact entry was replayed. No `reason` or `hintDepth`. |
| `prefix_hint` | An L2 prefix hint matched. `hintDepth` is required and is an integer from 1 through 64. |
| `store_skipped` | A completed result was not eligible for storage. A bounded stable `reason` is required. |
| `store_failed` | Cache storage failed open after an otherwise safe response. A bounded stable `reason` is required. |

The schema machine-enforces these state relationships on bounded JSON success
objects and on all three response terminal SSE events:

- `exact_hit` is valid only with `status: "complete"`,
  `tool.observed: false`, `fallback.attempted: false`,
  `fallback.attemptCount: 0`, and `fallback.finalAction: "none"`. It is
  therefore invalid on `interrupted` or `uncertain` terminals.
- `prefix_hint` requires `fallback.attemptCount` of at least 1, because a hint
  never replaces the required Provider call.
- `tool.observed: true` forbids `exact_hit`. Its final cache outcome is limited
  to `disabled`, `bypass`, `miss`, `prefix_hint`, `store_skipped`, or
  `store_failed`, with the outcome-specific `reason`/`hintDepth` rules intact.

No cache summary contains cached text, a cache key or hash, a Provider request
identifier, a path, or entry metadata. A `prefix_hint` contains only the
bounded depth and still requires a real Provider call; it never injects old
text into the request or response.

## Fallback summary

Every success contains:

```json
{
  "attempted": false,
  "attemptCount": 1,
  "finalAction": "none"
}
```

- `attempted` says whether the fallback policy selected a non-primary action.
  Merely considering configured candidates does not make it `true`.
- `attemptCount` is the number of actual Provider transport calls started for
  this request, not the number of policy evaluations. It is 0 for an exact
  cache hit, normally 1 for a primary call, and at most 2 in v1.
- `finalAction` is `none`, `retry`, `queue`, `fail`, or
  `degrade_to_cheap`. `attempted: false` requires `none`; `retry` and
  `degrade_to_cheap` require exactly two Provider calls.
- Optional `errorCode` and `retryable` appear together. They contain only a
  sanitized stable classification and whether the same logical operation may
  later succeed. They never contain Provider error text.

V1 permits at most one alternative Provider call. `retry` and
`degrade_to_cheap` both consume that single retry. Unselected candidate lists,
health samples, breaker internals, cost calculations, sticky-selection state,
and raw errors are never public fallback fields. Once a selected fallback has
actually produced the final output, its effective Provider/model is reported
only in the JSON result or terminal SSE event so the answer source is accurate.

## Accepted-runtime JSON error object

After the router has accepted `/gateway/v1/responses` for execution, a
response-domain failure uses the existing `code`, `message`, and `retryable`
detail shape inside a versioned Gateway envelope:

```json
{
  "apiVersion": "gateway.openusage/v1",
  "object": "gateway.error",
  "error": {
    "code": "upstream_timeout",
    "message": "Provider request failed.",
    "retryable": true
  },
  "fallback": {
    "attempted": true,
    "attemptCount": 1,
    "finalAction": "fail",
    "errorCode": "upstream_timeout",
    "retryable": true
  }
}
```

`fallback` is optional when the accepted runtime failed before fallback policy
was applied. `message` is a sanitized, bounded human-facing sentence. It cannot
contain control characters or internal exception text. A producer may add a
new stable error code without changing this schema's closed object shape.

Transport/router rejections listed in the scope boundary above remain the
unversioned Gateway API v1 problem JSON and are outside this response schema.

## Gateway-native SSE

For an accepted streaming delivery the HTTP response uses
`Content-Type: text/event-stream; charset=utf-8` and `Cache-Control: no-store`.
It does not use `Content-Length`. The server writes and flushes each bounded
event separately and stops the upstream iterator when the client disconnects
or the request deadline expires.

Each contract event consists of exactly one `event:` line and one single-line
UTF-8 JSON `data:` line, followed by a blank line. The wire event name MUST be
identical to `data.type`:

```text
event: gateway.response.started
data: {"apiVersion":"gateway.openusage/v1","type":"gateway.response.started","sequence":0,"provider":"openai","model":"gpt-4.1-mini","stream":true}

event: gateway.output_text.delta
data: {"apiVersion":"gateway.openusage/v1","type":"gateway.output_text.delta","sequence":1,"text":"Hello."}

event: gateway.response.completed
data: {"apiVersion":"gateway.openusage/v1","type":"gateway.response.completed","sequence":2,"provider":"openai","model":"gpt-4.1-mini","status":"complete","usage":null,"tool":{"observed":false},"cache":{"outcome":"disabled"},"fallback":{"attempted":false,"attemptCount":1,"finalAction":"none"}}

```

The `$defs.sseEvent` discriminated union in the schema contains exactly these
data shapes:

| `data.type` and matching `event:` | Event-specific data |
|---|---|
| `gateway.response.started` | Required first accepted-request event; includes the accepted primary `provider`, accepted primary `model`, and `stream: true`. |
| `gateway.output_text.delta` | Includes `text`. Concatenating these values in sequence order produces the same public text as JSON `outputText`. |
| `gateway.usage.snapshot` | Includes a non-null `usage` object. Snapshots are cumulative and Provider-reported, not deltas or estimates. |
| `gateway.tool.observed` | Includes exactly `tool: {"observed": true}` and is emitted at most once, when first observed. |
| `gateway.cache.event` | Includes a cache summary. It is emitted when a cache outcome becomes observable; the terminal event remains authoritative. |
| `gateway.fallback.event` | Includes the current fallback summary after a fallback action becomes observable. No candidate identifiers or raw errors appear. |
| `gateway.response.completed` | Terminal event with `status: "complete"`, authoritative effective `provider`/`model`, and final `usage`, `tool`, `cache`, and `fallback`. |
| `gateway.response.interrupted` | Terminal event with `status: "interrupted"`, authoritative last effective `provider`/`model`, and the same final summaries. It is emitted only while the client connection is writable. |
| `gateway.response.uncertain` | Terminal event with `status: "uncertain"`, authoritative last effective `provider`/`model`, and the same final summaries. |
| `gateway.error` | Terminal sanitized runtime-domain `error` and optional final `fallback`; it may also be the only event when the accepted runtime fails before a started event can be emitted. |

Every SSE `data` object requires `apiVersion`, `type`, and `sequence`.
`sequence` starts at 0 and increases by exactly one for each data event. It is
bounded to 0 through 4095, so a response contains at most 4096 data events.
The complete UTF-8 output-text budget is 16 MiB. Implementations split output
into data events of at most 64 KiB of UTF-8 text and enforce the encoded event,
idle, and total response limits before writing. These byte and sequence rules
are stream invariants enforced by boundary tests; JSON Schema validates each
event object independently and cannot validate a whole event sequence. The
schema records the two UTF-8 byte budgets with the contract-specific annotation
`x-openusage-maxUtf8Bytes`: `16777216` on bounded JSON `outputText` and `65536`
on SSE delta `text`. OpenUsage validators interpret that annotation against the
encoded UTF-8 byte length. It is not JSON Schema `maxLength` (which counts
characters), and generic validators that do not implement the extension may
otherwise treat it as an unknown annotation.

### Ordering and terminal rules

1. An accepted request emits `gateway.response.started` at sequence 0 before
   any output, usage, tool, cache, or fallback event.
2. An accepted runtime-domain error before stream start may emit only
   `gateway.error` at sequence 0; it does not emit a synthetic started event.
   Transport/router rejection never enters this SSE sequence.
3. A normally writable stream emits exactly one terminal event:
   `gateway.response.completed`, `gateway.response.interrupted`,
   `gateway.response.uncertain`, or `gateway.error`.
4. No data event is emitted after a terminal event.
5. A client abort is the sole wire-level exception: the connection may end
   without a terminal event because it is no longer writable. Internally the
   request is recorded as `interrupted`, the Provider iterator is closed, and
   the partial result is never cached or replayed.
6. A missing Provider terminal event, malformed/truncated framing, or any
   stream byte/event/deadline limit produces `uncertain` when the client is
   still writable. It never produces `completed`.
7. For all three response terminals, terminal `provider` and `model` are the
   authoritative effective source of the output. They may differ from the
   accepted primary values in `gateway.response.started` only after one
   permitted fallback call.

## Cache and replay sequences

An exact-cache-hit SSE has this deterministic shape:

1. `gateway.response.started`
2. `gateway.cache.event` with `outcome: "exact_hit"`
3. One or more `gateway.output_text.delta` events containing only text
   rehydrated through the current request's redaction map. Even empty cached
   output emits one empty delta so the replay shape is unambiguous.
4. `gateway.response.completed`

An exact hit performs zero credential reads and zero Provider calls, so its
fallback `attemptCount` is 0. Safe cached usage may be returned; otherwise
`usage` is `null`. A tool-observed result can never become an exact hit.

A prefix hit emits `gateway.cache.event` with `outcome: "prefix_hint"` and a
bounded `hintDepth`, then continues to a Provider call. The event exposes no
old response text, cache hash, key, or candidate data. Prefix hints are not
replay responses.

Once `gateway.tool.observed` occurs, the request is not eligible for cache
storage or automatic replay. Fallback retry or `degrade_to_cheap` is prohibited
after any output delta has become visible to the caller, observed tool use,
Provider side effects, or client abort. This prevents concatenating output from
two Providers and preserves at-most-once side-effect behavior.

## Privacy boundary

Gateway JSON and SSE never expose:

- Provider response headers or Provider/request/control/tool-call IDs;
- credentials, bearer tokens, cookies, authorization fields, or keychain data;
- local filesystem paths or direct account identity;
- prompts, Provider-native bodies, raw chunks, raw error messages, or
  unselected fallback candidate identifiers/internal selection state.

`outputText` and `gateway.output_text.delta.text` are the deliberate public
answer channel and may contain original or request-local rehydrated user data.
That content is not a safe-to-log representation. Logs, telemetry, diagnostics,
crash reports, and `activity.sqlite3` must omit it entirely. Cache storage may
contain only the sealed redacted representation admitted by the cache policy;
rehydration remains request-local and is never persisted.

## Current implementation boundary

The executable streaming path is pull-driven end to end. A closeable
`ProviderEventStream` owns the upstream response; `GatewayRuntime.stream_events`
incrementally parses the five v1 Provider protocols and yields normalized
Gateway events without first aggregating the Provider response. The listener
writes and flushes one event per downstream pull, so a slow client applies
backpressure to upstream consumption.

`GatewayRouter.dispatch_events(...)` wraps every live source in a lazy,
closeable contract validator. Before an event reaches the wire, the wrapper
rebuilds it from the closed v1 allowlist and enforces accepted-source identity,
contiguous sequence values, the full event union, terminal/status agreement,
cross-field response invariants, per-delta and total UTF-8 budgets, and one
terminal with no post-terminal output. Invalid, raising, or prematurely
exhausted sources fail closed with at most one locally built sanitized error;
rejected source material is never reflected.

Iterator ownership propagates listener disconnect, write/flush failure,
timeout, and explicit close to the upstream source exactly once. A successful
runtime resumes only after the terminal event has been written and flushed,
which is the cache-admission point; failure while delivering that terminal
closes the generator before storage. `dispatch_sse(...)` remains a materialized
compatibility helper, and buffered `ProviderResult` remains available for the
bounded JSON/non-stream path without weakening the live streaming path.

The conformance suite covers JSON/SSE equivalence, all five Providers,
arbitrary UTF-8 chunk boundaries, exact-hit replay, prefix hints, tool use,
fallback, missing or malformed terminals, resource limits, downstream abort,
terminal write/flush failure, public-payload privacy, and exact-once upstream
close. Native Windows/Linux package execution remains release evidence rather
than part of this response-wire contract.
