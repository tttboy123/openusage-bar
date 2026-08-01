# ADR 0002: Bounded Runtime Observation Plane

- Status: Accepted
- Date: 2026-08-01
- Owners: OpenUsage Bar maintainers

## Context

The durable activity ledger records daily usage, capacity, spend, balance and
source health. It is intentionally unsuitable for request-level observations:
request cardinality, retention, privacy and update frequency are different,
and writing every request to `activity.sqlite3` would inflate its change log
and blur observed resource facts with transient telemetry.

Loom and other schedulers need recent Token velocity, latency and failure
signals, but OpenUsage Bar must remain an optional sensor. It must not own
reservations, admission or routing policy, and runtime ingestion must never
accept prompts, responses, credentials, raw Provider payloads or direct account
identity.

## Decision

OpenUsage Bar provides an optional, local Runtime Observation Plane with these
boundaries:

- Python Collector code is the only writer.
- Raw observations live in a separate `runtime.sqlite3`; they never enter
  `activity.sqlite3` or Local API v1 changes.
- Raw observations are terminal request summaries, not stream chunks. A local
  opaque `observationId` makes retries idempotent without storing an upstream
  request ID.
- Provider and model IDs are public catalog identifiers. Account/session scope
  is represented only by an `anon_` prefixed hexadecimal `scopeRef`; raw account
  names, e-mail addresses and Provider account IDs are rejected.
- Accepted data is limited to UTC timestamps, Token counters, terminal status,
  latency derivable from timestamps, optional micro-unit cost, source and
  quality.
- Retention is 24 hours. The store is capped at 100,000 rows and 64 MiB. One
  ingestion document is capped at 1 MiB and 256 observations.
- Read-only summaries are bounded to a 24-hour window and at most 512
  Provider/model/scope groups. They expose a separate monotonic
  `runtimeRevision`; they do not reuse the durable ledger `dataRevision`.
- All limits are fail-closed. Complete empty means a covered zero-observation
  window; malformed, oversized or private input never becomes zero.

The first implementation exposes bounded Collector CLI commands. It does not
add a network write route, change Local API v1, write daily aggregates, or add
Provider-specific hooks. Producers such as LiteLLM OTel, CLIProxyAPI or local
agent hooks can be adapted later against this frozen ingestion document.

## Alternatives Considered

### Store request events in `activity.sqlite3`

- **Pros:** one database and one query stack.
- **Cons:** high-cardinality rows and retention would bloat durable history and
  couple runtime telemetry to Local API v1 changes.
- **Why not:** violates ADR 0001's Fact/Telemetry ownership boundary.

### Keep only an in-memory ring buffer

- **Pros:** minimal disk writes and automatic cleanup on exit.
- **Cons:** loses all recent evidence on process restart and cannot support a
  stable revision for local consumers.
- **Why not:** a small, short-lived SQLite store provides bounded restart
  continuity without turning telemetry into durable history.

### Persist upstream request and account identifiers

- **Pros:** easy cross-system correlation and deduplication.
- **Cons:** increases identity and prompt-adjacency risk and makes diagnostics
  more sensitive.
- **Why not:** local opaque IDs and anonymous scope are sufficient for resource
  observation.

### Put reservation and routing decisions in the runtime store

- **Pros:** one apparent resource-control surface.
- **Cons:** turns observed telemetry into scheduling authority and makes Loom a
  hidden dependency.
- **Why not:** reservations and policy remain scheduler-owned under ADR 0001.

## Consequences

### Positive

- Loom can read recent velocity and failures without accessing credentials,
  prompts, the durable ledger database or UI text.
- Runtime volume, retention and schema changes cannot silently alter Local API
  v1 resource facts.
- Idempotent terminal summaries avoid stream-event amplification.

### Negative

- Raw request observations survive for only 24 hours.
- The first slice does not report in-flight requests or expose a network API.
- Producers need a small adapter that emits the strict ingestion document.

### Risks

- Anonymous scope may still be locally linkable. Mitigation: strict grammar,
  short retention, no mapping table and no export in diagnostics.
- A producer may retry or flood the store. Mitigation: idempotent IDs, batch,
  row, file-size and time-window bounds with oldest-first pruning.
- Consumers may confuse `runtimeRevision` with `dataRevision`. Mitigation:
  separate schema, commands, names and database with no Local API v1 changes.
