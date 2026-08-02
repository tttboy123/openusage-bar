# Route Decision API v1

OpenUsage Bar exposes automatic strategy routing through a second private Unix
socket. It is standalone from Loom and never proxies a model request in Phase A.

```text
~/.local/state/openusage-bar/router.sock
```

The socket is owned by the current user and has mode `0600`. The API accepts
HTTP/1.1 only, closes every connection after one request, limits request bodies
to 64 KiB and limits JSON output to 256 KiB.

## Routes

```text
GET  /v1/health
GET  /v1/schema.json
GET  /v1/policies
GET  /v1/targets
POST /v1/decisions
POST /v1/simulations
POST /v1/shadow-decisions
POST /v1/replays
GET  /v1/decisions?before=<decisionId>&limit=1..100
GET  /v1/decisions/<decisionId>
GET  /v1/shadow-decisions?before=<shadowId>&limit=1..100
```

`POST /v1/decisions` reads one Resource Snapshot and one independent Runtime
Summary, applies hard safety filters before deterministic scoring, and stores
only bounded content-free evidence. `POST /v1/simulations` uses the same engine
but never writes evidence.

`POST /v1/shadow-decisions` accepts the same metadata-only decision request plus
one public `actualTargetId`. It evaluates current facts once, compares the
actual target with the recommendation and stores only the comparison. It never
stores a normal decision, executes a model request or accepts an outcome,
prompt, response, header or credential. The response reports agreement, actual
target state/rejection codes, score advantage and comparable cost/latency
deltas. Missing facts remain `unknown`; they are not converted to zero.

`POST /v1/replays` evaluates 1–256 canonical frozen cases against one policy.
Every case contains an exact decision request, frozen Route Targets, frozen
Target Facts and their data/runtime revisions. Replay never reads current facts
and never writes decision or shadow evidence. Reports are ordered by `caseId`
and aggregate agreement, no-route, actual-rejected, comparable score, latency
and currency-separated cost deltas. Duplicate case, target or fact identifiers
fail closed. The global 64 KiB request limit still applies, so a batch containing
large target sets may reach the byte limit before the 256-case logical limit.

`GET /v1/health` also reports `decisionApiEnabled`, `defaultPolicyId` and the
monotonic `preferencesRevision`. The master switch is stored in a private,
atomically replaced `routing-preferences.json` document. Disabling decisions
keeps health, policy, target and content-free history reads available so the
native app can explain and re-enable the feature without restarting the
collector. Decision and simulation writes then fail with the stable
`router_disabled` error and write no evidence.

The authoritative request schemas are available together at `/v1/schema.json`
under `schema`, `shadowSchema` and `replaySchema`. They are tracked in:

- `openusage_bar/resources/routing-api-v1.schema.json`;
- `openusage_bar/resources/routing-shadow-v1.schema.json`;
- `openusage_bar/resources/routing-replay-v1.schema.json`.

Unknown fields, duplicate JSON keys, trailing JSON values, booleans in numeric
fields and noncanonical identifiers fail closed.

No request field can represent a prompt, model output, tool body, HTTP header,
secret, endpoint URL or direct account identity. `accountRef`, `connectionRef`
and optional session references are opaque local identifiers.

## CLI

Pass metadata-only JSON on standard input:

```bash
openusage-bar route decide --format json < route-request.json
openusage-bar route simulate --format json < route-request.json
openusage-bar route history --format json --limit 20
openusage-bar route shadow --format json < shadow-request.json
openusage-bar route shadow-history --format json --limit 20
openusage-bar route replay --format json < replay-fixture.json
```

Use `--socket /absolute/path/router.sock` only for an explicitly selected local
instance. The CLI validates socket ownership, mode and inode before sending any
bytes.

Exit codes are stable:

- `0`: decision, simulation or history succeeded;
- `1`: local API or routing facts unavailable;
- `2`: invalid request;
- `3`: no eligible route.

`router_disabled` uses exit code `1`; callers must not silently route around a
deliberate local disable.

## Evidence and compatibility

Decision and shadow evidence are retained for at most seven days in
`routing.sqlite3`, capped at 10,000 decisions, 10,000 shadow comparisons,
30,000 execution attempt summaries and 16 MiB. Replay writes nothing. Logging
failure never changes the selected target and is reported as
`evidenceStored=false`.

The Resource API remains read-only and unchanged. Route Decision API schema
`1.0` is a separate compatibility surface. Additive output fields may be ignored
by clients; request fields remain closed and require a new schema revision.

Phase A does not claim quota reservation or execution success. The optional
loopback execution proxy is a later, separately enabled phase.
