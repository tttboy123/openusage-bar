# Plugin API v1

The Plugin API is an optional, private integration boundary for Loom, Codex,
Claude Code, and the UsageHub desktop host. It is a third listener at
`127.0.0.1:17824`; it is not part of Local API v1 or Gateway API v1.

## Authentication and scope

The daemon keeps four distinct bearer tokens in its private state directory:
`plugin/loom.token`, `plugin/codex.token`, `plugin/claude_code.token`, and
`plugin/desktop.token`. A token contains only printable ASCII and no newline.
The token itself determines the principal. `pluginId` assertions in headers or
request bodies are rejected.

The three external principals may read schema, capabilities, and only their own
decisions, and may call the five POST routes. The Desktop principal has only
`connections:read`; it cannot query facts, request advice, record outcomes, or
read decisions.

## Routes

The committed manifest is
`openusage_bar/resources/plugin-api-v1.schema.json`. The closed route set is:

- `GET /plugin/v1/schema`
- `GET /plugin/v1/capabilities`
- `GET /plugin/v1/decisions/{decisionId}`
- `GET /plugin/v1/connections`
- `POST /plugin/v1/usage/query`
- `POST /plugin/v1/quotas/query`
- `POST /plugin/v1/health/query`
- `POST /plugin/v1/route-advice`
- `POST /plugin/v1/outcomes`

GET routes accept neither a query string nor a body. POST bodies are strict
UTF-8 JSON objects, reject duplicate/non-finite values and unknown fields, and
are limited to 65,536 bytes. `route-advice` and `outcomes` additionally require
`Idempotency-Key: idem_` followed by 32 lowercase hexadecimal characters.

`usage/query` accepts exactly `{apiVersion,from,to}` and is limited to 31 days
and 1,000 public rows. `quotas/query` accepts exactly `{apiVersion,limit}`, where
`limit` is 1–128. Quotas remove private account identity and return at most the
most constrained observed scope for each provider, explicitly marked with
`selection: "most_constrained_observed_scope"`. Unknown values remain `null`.

`health/query` reports the listener, Observer API, and Gateway API separately.
It does not infer Provider, credential, account, quota, or execution health.

`route-advice` accepts exactly
`{apiVersion,provider,model,estimatedTokens,window}`. The provider is a fixed
known provider ID. The response owns a `decision_` ID and is always marked
`execution: "advice_only"`; it never echoes or persists provider/model.

`outcomes` accepts exactly
`{apiVersion,decisionId,outcome,reason,occurredAt}`. Outcomes are
`succeeded|failed|cancelled|ignored` with their closed reason mapping. A
decision's first terminal outcome wins. Repeating the same outcome returns the
original receipt; a different terminal outcome returns
`409 decision_outcome_conflict`.

All public timestamps use UTC with exactly six fractional digits. Shared deep
validation lives in `openusage_bar.plugin.contracts.sanitize_response`.

## Persistence and privacy

`plugin/plugin.sqlite3` is an independent private database. It never reuses the
activity ledger, Gateway cache, or Gateway telemetry. Idempotency is scoped by
principal and route and stores only a domain-separated hash of the key, a
canonical request digest, and an exact safe response. Raw keys and request
bodies are not stored.

Completed promises and decisions are retained for seven days, with at most
10,000 idempotency records per principal. Records inside the promise window are
not evicted to make room. Same key plus same canonical payload replays the exact
status/body across threads and restarts; a different payload returns
`409 idempotency_conflict`. A live in-flight operation is never taken over.
After a process crash, its residual in-flight record becomes a stable safe 503
response rather than executing the side effect again.

Stored decisions, terminal receipts, and their idempotency response are committed
in one SQLite transaction. The database never stores prompt, response, raw tool
error, credential, bearer/header, endpoint/path, private account ID, or an
external tool Attempt. Plugin `decision_*` records are separate from Task 15
process-lifetime Gateway `trace_*` records.

## Composition and failure isolation

The daemon reads facts only through a bounded Local API client and obtains
advice only through a bounded Gateway client. Dependency discovery is lazy, so
starting the Plugin daemon before Observer or Gateway does not require a Plugin
restart when those services later become available. Dependency failures return
closed unavailable states or stable 503 errors and do not stop Observer, Local
API, Gateway, or the external tool's existing fixed-account path.

Run it explicitly with `openusage-bar plugin start`. Separate opt-in user-service
commands are `plugin install-service`, `plugin uninstall-service`, and
`plugin print-service`; no Desktop startup path installs a service automatically.

The hidden packaged smoke `__plugin-self-test --format json` uses a temporary
private state directory and synthetic inputs. It proves token/principal
isolation, replay/conflict, and reopen replay only; it does not claim any third
party tool is installed, connected, or synchronized.
