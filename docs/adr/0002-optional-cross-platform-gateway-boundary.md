# ADR 0002: Optional Cross-Platform Gateway Boundary

- Status: Accepted
- Date: 2026-08-08
- Owners: OpenUsage Bar maintainers
- Amends: ADR 0001

## Context

ADR 0001 separated durable resource facts, short-lived request telemetry,
scheduler reservations, and routing policy. That separation remains necessary,
but it described OpenUsage Bar as a macOS-only distribution and assigned all
admission and routing policy to Loom or another scheduler.

OpenUsage Bar now needs to preserve the observation product while supporting
Windows and Linux and adding an optional local Gateway. The Gateway must not
turn observed quota into global scheduling authority, make Loom a dependency,
change Local API v1, expose Provider credentials to a renderer, or put
request-shaped data in the durable fact ledger.

## Decision

OpenUsage Bar has two independently operable layers:

1. **Observer** is the default product. The Python Collector is its only
   durable fact writer, Local API v1 remains read-only, and SwiftUI or
   Electron/Web presents those facts.
2. **Gateway** is a separately started, explicit opt-in daemon. It may advise
   on or route only requests submitted directly to its own API. It is not a
   general scheduler and does not own Loom reservations or policy.

The installed modes are `observe`, `advise`, and `gateway`. Fresh installs and
upgrades remain in `observe`; neither `advise` nor `gateway` is enabled by an
upgrade.

### Contract and failure boundaries

- Local API `/v1/*` remains GET/HEAD-only and retains its existing listener,
  schemas, ETag behavior, and N-1 fixtures.
- Stateful Gateway calls use a separate listener, bearer token, and
  `/gateway/v1/*` namespace.
- The Collector and Gateway run as separate processes. Either may restart or
  fail without stopping the other.
- Should-Send reads recorded capacity snapshots and bounded Gateway
  aggregates. Its request thread never invokes a live quota adapter or reads a
  Provider credential.
- The Gateway pipeline may combine ingress, provider egress, cache, streaming,
  and fallback modules in one process; this does not merge its failure domain
  with the Collector.

### Data and privacy boundaries

| Store | Permitted data | Forbidden data |
| --- | --- | --- |
| `activity.sqlite3` | durable observed facts and privacy-safe daily Gateway aggregates | prompts, responses, chunks, request IDs, cache keys, and per-request events |
| `gateway-telemetry.sqlite3` | bounded local timestamps, fixed Provider IDs, domain-separated SHA-256 model scopes, Token counts, latency, status class, cost, cache outcome, and sanitized error codes | raw model labels, prompts, responses, credentials, cookies, direct identity, and raw Provider payloads |
| `gateway-cache.sqlite3` | opt-in, deterministically redacted canonical request/response entries with TTL, LRU metadata, and a size cap | unredacted content, credentials, tool-use responses, interrupted streams, and uncertain-to-redact content |

Gateway telemetry is local functional state, not remote analytics. Neither
Gateway database is included in diagnostics, canary submissions, or release
artifacts. Cache is disabled by default, can be cleared independently, expires
entries after one hour by default, and is capped at 100 MiB. Per-hit events stay
in the bounded telemetry store; only anonymous daily totals may be imported by
the Collector into the durable ledger.

Telemetry model scopes use explicit storage-version provenance. Version-zero
rows are migrated once from their original value, including values that happen
to look like a scope; version-one rows must satisfy the closed anonymous scope
format. A fast-failed aggregate write marks its Provider/model burn window
incomplete across same-path Gateway-process handles. An overlapping policy
read returns unknown rather than an optimistic exact rate.

Provider credentials remain owned by Python and are retrieved from macOS
Keychain, Windows Credential Manager, or Linux Secret Service. Renderers receive
neither Provider credentials nor Local/Gateway bearer tokens. A Linux build
that enables credentialed Provider calls must package and audit the existing
optional `secretstorage` backend; when that backend or Secret Service is
unavailable, credentialed Gateway mode fails closed while Observer mode remains
usable.

### Platform surface

- SwiftUI remains the native macOS menu-bar client.
- Electron/Web is the shared desktop client for macOS, Windows, and Linux.
- Python owns collection, credentials, the ledger, Gateway execution, and
  platform service registration through launchd, systemd user services, or
  Task Scheduler.
- Loom and other agent runtimes remain optional API consumers and never gate a
  release.

## Amendment to ADR 0001

This ADR supersedes only these parts of ADR 0001:

- the Policy row's exclusive assignment of admission and routing to Loom or
  another scheduler;
- the rejected alternative "Put reservations and routing in OpenUsage Bar",
  but only for requests explicitly submitted to the opt-in Gateway; and
- the consequence that the released desktop distribution remains macOS-only.

ADR 0001 continues to govern durable fact ownership, separation of request
telemetry from the fact ledger, read-only Local API semantics, scheduler
agnosticism, and Loom independence. Gateway routing does not grant write access
to scheduler reservations or convert an observed fact into universal
permission to run work.

## Consequences

- Existing Observer installs and Local API consumers can upgrade without
  enabling a new listener or changing their contract.
- Gateway storage, privacy, and canary evidence are reviewed separately from
  the existing observation cohort.
- Cross-platform release claims require native install, upgrade, rollback,
  uninstall, service, credential, and artifact evidence on every advertised
  operating system.
- Version truth, privacy documentation, and both canary cohorts must agree
  before a 1.0 stable tag is allowed.

## Rejected alternatives

- **Add POST routes to Local API v1.** Rejected because it breaks a stable,
  read-only observation contract.
- **Run Gateway inside the Collector.** Rejected because Provider latency,
  cache corruption, or proxy failure could stop fact refresh.
- **Write request events or cache keys to the fact ledger.** Rejected because
  their cardinality, retention, and privacy properties differ from durable
  facts.
- **Enable Gateway on upgrade.** Rejected because a compatible upgrade cannot
  silently begin forwarding requests or reading Provider credentials.
- **Make Loom mandatory.** Rejected because OpenUsage Bar remains independently
  useful and scheduler agnostic.
