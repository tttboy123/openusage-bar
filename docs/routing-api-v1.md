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
GET  /v1/decisions?before=<decisionId>&limit=1..100
GET  /v1/decisions/<decisionId>
```

`POST /v1/decisions` reads one Resource Snapshot and one independent Runtime
Summary, applies hard safety filters before deterministic scoring, and stores
only bounded content-free evidence. `POST /v1/simulations` uses the same engine
but never writes evidence.

The authoritative request schema is available at `/v1/schema.json` and tracked
in `openusage_bar/resources/routing-api-v1.schema.json`. Unknown fields,
duplicate JSON keys, trailing JSON values, booleans in numeric fields and
noncanonical identifiers fail closed.

No request field can represent a prompt, model output, tool body, HTTP header,
secret, endpoint URL or direct account identity. `accountRef`, `connectionRef`
and optional session references are opaque local identifiers.

## CLI

Pass metadata-only JSON on standard input:

```bash
openusage-bar route decide --format json < route-request.json
openusage-bar route simulate --format json < route-request.json
openusage-bar route history --format json --limit 20
```

Use `--socket /absolute/path/router.sock` only for an explicitly selected local
instance. The CLI validates socket ownership, mode and inode before sending any
bytes.

Exit codes are stable:

- `0`: decision, simulation or history succeeded;
- `1`: local API or routing facts unavailable;
- `2`: invalid request;
- `3`: no eligible route.

## Evidence and compatibility

Decision evidence is retained for at most seven days in
`routing.sqlite3`, capped at 10,000 decisions, 30,000 execution attempt summaries
and 16 MiB. Logging failure never changes the selected target and is reported as
`evidenceStored=false`.

The Resource API remains read-only and unchanged. Route Decision API schema
`1.0` is a separate compatibility surface. Additive output fields may be ignored
by clients; request fields remain closed and require a new schema revision.

Phase A does not claim quota reservation or execution success. The optional
loopback execution proxy is a later, separately enabled phase.
