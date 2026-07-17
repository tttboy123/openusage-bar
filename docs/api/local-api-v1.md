# OpenUsage Bar Local API v1

This is a read-only, single-version API for local schedulers and native clients.
It uses HTTP/1.1 over a user-only Unix domain socket by default. TCP is an
explicit IPv4-loopback opt-in and always requires bearer authentication.

## Response contract

Successful responses are UTF-8 JSON and preserve the canonical `QueryService`
camelCase envelope. Every response starts with these stable fields:

| Field | Type | Meaning |
|---|---|---|
| `schemaVersion` | string | `1.0` for this contract |
| `dataRevision` | integer | Ledger high-water revision used by the response |
| `generatedAt` | RFC 3339 string | Time the query result was generated |

The remaining fields are the same fields emitted by the existing collector CLI:

| Route | Query parameters | Data fields and ordering |
|---|---|---|
| `GET /v1/health` | none | `sources`, plus `health` |
| `GET /v1/schema` | none | `routes`, `errorShape` |
| `GET /schema` | none | Compatibility alias of `/v1/schema` |
| `GET /v1/summary` | optional `today=YYYY-MM-DD` | `todayTokens`, `modelCount`, `coveredDayCount` |
| `GET /v1/capabilities` | none | `providers`, sorted by `familyId`; nested sources retain declared priority |
| `GET /v1/providers` | optional comma-separated `providerIds` | observed/configured provider instances, sorted by `providerId` |
| `GET /v1/capacity` | optional `limit=1..1000` | `providers`, in canonical urgency order |
| `GET /v1/activity/daily` | required `from`, `to`; optional comma-separated `providerIds`, `modelIds` | `rows`, `coverage`, canonical chronological order; at most 731 days |
| `GET /v1/quotas/history` | optional `providerId`, `accountRef`, `limit`; `from` and `to` must be supplied together | `snapshots`; newest selected page returned in chronological order |
| `GET /v1/sources/status` | none | `sources`, canonical provider/source order |
| `GET /v1/changes` | optional `after` (default 0), `limit` (default 100) | `records`, `nextCursor`; an ahead cursor is invalid |

Unknown or repeated query parameters, malformed percent escapes, controls,
noncanonical dates, unstable identifiers, oversized ranges, and out-of-range
limits/cursors are rejected. Provider and model lists contain at most 50 stable
IDs. Query strings are at most 4096 bytes.

`/v1/capabilities` is the static family catalog. It exposes all 35 provider
families from OpenUsage 0.23.0 plus the MiniMax and Step Plan built-ins. Each
family includes `familyId`, `displayName`, `category`, `metricFamilies`,
`regions`, `supportsAccounts`, `capabilities`, and declared-priority `sources`.
The top-level
`upstream` object pins the OpenUsage version and revision used to build the
catalog. Source facts are machine-readable identifiers and never localized UI
status text. For schema v1 compatibility, every family also retains
`providerId` with the same canonical value as `familyId`; `providerId` is a
deprecated alias and will be removed only in a later schema version.

The `capabilities` object has these exact fields:

| Field | Type | Meaning |
|---|---|---|
| `quotaWindows` | object | `{state, values}` declaration for quota periods |
| `tokenHistory` | capability state | Historical token usage |
| `modelBreakdown` | capability state | Per-model usage breakdown |
| `resetTimestamps` | capability state | Quota reset timestamps |
| `billing` | capability state | Billing usage data |
| `credits` | capability state | Credit data |
| `balance` | capability state | Remaining balance data |
| `cost` | capability state | Monetary cost data |
| `rateLimits` | capability state | Provider rate-limit data |
| `serviceStatus` | capability state | Provider service-status data |

A capability state is one of `supported`, `unsupported`, or `unknown`.
`supported` is a conservative declaration backed by a known source;
`unsupported` means the capability is known not to be available; `unknown`
means OpenUsage Bar has no reliable declaration. `unknown` is not equivalent
to `unsupported` and must not be presented as a vendor limitation. In
`quotaWindows`, `values` uses zero or more of `session`, `five_hour`, `weekly`,
`monthly`, `billing_cycle`, and `model_specific`. A `supported` quota state
requires at least one value. `unknown` and `unsupported` require an empty
`values` array. The catalog does not infer or claim undeclared vendor
capabilities.

Each source retains the existing `sourceId`, `kind`, `timeoutSeconds`,
`freshnessSeconds`, `credentialType`, and `requiresCredential` fields and adds:

| Field | Type | Values |
|---|---|---|
| `operatingSystems` | string array | `macos`, `windows`, `linux` |
| `stability` | string | `stable`, `experimental`, `pinned`, `opaque` |
| `provenance` | string | `openusage_upstream`, `openusage_bar_builtin`, `provider_official`, `provider_local`, `user_session` |

All sources in the current catalog are macOS-only, so their
`operatingSystems` value is currently `["macos"]`; the enum is intentionally
extensible to the declared Windows and Linux values. Source array order is the
catalog's declared priority. Credential scopes, credential/account values,
local paths, tokens, and raw provider data are never serialized.

`/v1/providers` is the dynamic instance ledger. It exposes only
`providerId`, `familyId`, `displayName`, `category`, `credentialSource`,
`sourceKind`, `observedAt`, and `revision`. The equivalent offline CLI command
is logically `openusage-bar providers --format json`. The app does not install
a global executable; the real signed command path is:

```bash
HELPER="/Applications/OpenUsage Bar.app/Contents/Helpers/OpenUsage Provider Settings.app/Contents/MacOS/OpenUsage Provider Settings"
"$HELPER" providers --format json --offline
```

From a source checkout, use `.build-venv/bin/python openusage_settings.py
providers --format json --offline`. Account identity, email, local paths,
tokens, cookies, raw provider attributes, payload hashes, and internal
change-log fields are never part of this response. An unknown future OpenUsage
provider is represented as its own family (`providerId == familyId`) until the
static catalog is updated. `providerIds` has set semantics: duplicate and
reordered valid identifiers produce the same canonical response and ETag.

Only HTTP/1.1 is accepted. Every connection serves exactly one request and
returns `Connection: close`; ambiguous framing bytes can therefore never be
reinterpreted as a second request. `HEAD` has the same status and headers as
`GET`, including the `Content-Length` of the corresponding representation, but
no body. This also applies when the HTTP parser rejects a HEAD request before
route dispatch, including an oversized request line; only an exact `HEAD`
method token receives this treatment. All successful resource
responses carry a weak ETag derived from the complete canonical semantic JSON
payload. Only the non-semantic `generatedAt` render timestamp is excluded, so
catalog families, sources, upstream provenance, ledger revisions, and provider
instances all participate in the validator. An exact `If-None-Match` returns
`304` with no body and no `Content-Length`. Conditional GET and HEAD use the
standard weak comparison: an equivalent strong tag, any matching tag in a
valid entity-tag list, or `*` for an existing resource returns `304`.
Malformed or duplicate `If-None-Match` headers are rejected with the sanitized
`invalid_header` response.

## Errors

Every API-generated error has one shape and no implementation detail:

```json
{"error":{"code":"invalid_parameter","message":"Invalid request parameter."}}
```

Stable status/code families are:

| HTTP | Codes |
|---|---|
| 400 | `invalid_request`, `invalid_target`, `invalid_query`, `invalid_header`, `invalid_parameter`, `missing_parameter` |
| 401 | `authentication_required` |
| 403 | `forbidden_host`, `forbidden_origin` |
| 404 | `not_found` |
| 405 | `method_not_allowed` (`Allow: GET, HEAD`) |
| 413 | `request_too_large`, `request_body_not_allowed` |
| 429 | `rate_limited` with bounded `Retry-After` |
| 500 | `internal_error` |

Errors use `Cache-Control: no-store`; successful resources use
`Cache-Control: private, no-cache`. JSON responses include `Content-Length`,
`Content-Type: application/json; charset=utf-8`, and
`X-Content-Type-Options: nosniff`.

## Transport and security

- Unix socket: the parent directory is mode `0700`, the socket is `0600`, a
  live socket/symlink/non-socket is never replaced, and shutdown removes only
  the inode created by this server. Cleanup first uses macOS exclusive rename
  inside an `O_NOFOLLOW` parent directory descriptor to quarantine the name,
  then deletes only an exact socket inode match. A raced replacement is
  atomically restored or preserved with a recoverable error. The parent must be owned by the current
  user, so a shared system directory is never chmodded. Where the platform exposes peer
  credentials, the peer UID must equal the process UID.
- TCP: binds only `127.0.0.1`; the exact Host is
  `127.0.0.1:<actual-port>`. A supplied bearer has at least 43 non-whitespace
  ASCII characters, or one is generated with `secrets` and stored in an
  explicitly supplied user-only token file. A safe existing token file is
  reused across restarts. Verification is constant-time. A bounded,
  thread-safe token bucket applies per TCP server/bearer and returns `429` when
  exhausted.
- Origin: absent is accepted. A present Origin must exactly match the explicit
  allowlist. Wildcard Origin configuration is invalid.
- Request line, header parser, query, body, thread count, idle time, and total
  per-request wall-clock time are bounded. Deadline timers are canceled and
  active sockets are interrupted during shutdown. Request logs and handler
  tracebacks are disabled so authorization and query values cannot appear in
  logs.

## Non-goals

There are no POST/PUT/PATCH/DELETE/OPTIONS operations, refresh routes,
credential or provider configuration routes, remote binds, TLS termination, or
API-managed background process in v1. Packaging and launch are separate work.
The persistent daemon and launch integration remain an explicit Task 9 delivery
gate.
