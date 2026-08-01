# ADR 0001: OpenUsage Infrastructure Boundary

- Status: Accepted
- Date: 2026-08-01
- Owners: OpenUsage Bar maintainers

## Context

OpenUsage Bar started as a macOS menu-bar product and now also exposes a
versioned local resource API. Loom and other schedulers can consume those
facts, but they must not become runtime dependencies or gain write authority
over the local ledger.

The repository currently contains four kinds of behavior that must remain
separate:

1. long-lived resource facts and their provenance;
2. short-lived request observations;
3. scheduler reservations and forecasts;
4. human-facing presentation.

Mixing them would make an observed quota look like permission to schedule,
allow ephemeral request events to bloat the durable ledger, or make the macOS
UI part of a machine consumer contract.

## Decision

OpenUsage Bar is a standalone macOS product built on an OS-neutral resource
fact contract. Its stable infrastructure names are:

- **OpenUsage Ledger** for durable observed facts and history;
- **OpenUsage Resource API** for read-only snapshots and changes;
- **OpenUsage Collector** for bounded source adapters and writes;
- **OpenUsage Provider Kit** for reusable adapter contracts and fixtures.

The architecture has four explicit ownership layers:

| Layer | Owns | May write | Must not own |
| --- | --- | --- | --- |
| Fact | observed capacity, Token activity, API balance/spend, source health, provenance and freshness | Python Collector to the durable ledger | forecasts, reservations or routing policy |
| Telemetry | bounded request timestamps, anonymized scope, Token counters, latency, status and cost | a future bounded telemetry ingestor to a separate short-retention store | prompts, responses, credentials, direct identity or durable quota truth |
| Reservation | predicted work, active reservations and effective headroom | Loom or another scheduler in its own store | OpenUsage ledger facts |
| Policy | admission, checkpoint, pause and routing decisions | Loom or another scheduler | Provider credentials or OpenUsage ledger writes |

The Resource API remains UI-independent and read-only. Unknown, stale and
missing facts never become numeric zero. Consumers use source, quality,
coverage, freshness, scope and revision when deciding whether a fact is usable.

Runtime Telemetry, if implemented, uses a separate database or bounded ring
buffer with an explicit retention policy. Only privacy-safe daily aggregates
may enter the durable activity ledger. It is not part of the 0.7 P0 migration.

The core Provider and Source schemas accept any declared supported operating
system. The OpenUsage Bar distribution separately requires every registered
runtime source to support macOS. Windows and Linux declarations therefore do
not imply that desktop distributions for those systems exist.

OpenUsage.sh remains an optional producer. OpenUsage Bar depends on a frozen
export contract and fixtures, not on imports from its Go implementation or on
undocumented development-commit behavior.

## Consequences

- OpenUsage Bar remains fully useful without Loom.
- Loom can read `/v1/snapshot` and `/v1/changes` without reading SQLite,
  Keychain or UI text.
- `ProviderCard` becomes a presentation result; new core collection paths emit
  fact-specific results first.
- Request telemetry and scheduler state cannot silently change Local API v1
  resource facts.
- Cross-platform evolution starts at the schema and adapter contract, while
  the released application remains macOS-only.
- A future telemetry implementation and a future OpenUsage export protocol
  each require their own ADR and compatibility fixtures before code lands.

## Rejected alternatives

- **Put reservations and routing in OpenUsage Bar.** Rejected because observed
  facts are not scheduling authority and the product must remain scheduler
  agnostic.
- **Store request events in `activity.sqlite3`.** Rejected because request
  cardinality, retention and privacy differ from durable daily facts.
- **Make Loom a runtime dependency.** Rejected because the menu-bar product and
  its local API must continue working independently.
- **Keep macOS as a core schema invariant.** Rejected because it blocks valid
  Linux and Windows adapter declarations before a distribution has evaluated
  whether it can run them.
