# ADR 0003: Local Strategy Routing Plane

- Status: Accepted
- Date: 2026-08-02
- Owners: OpenUsage Bar maintainers

## Context

OpenUsage Bar already observes durable resource facts and bounded recent
Runtime signals. A local application or coding agent still has to decide which
Provider, account and model to use before it can benefit from those facts.
Depending on Loom for that choice would break the standalone product boundary,
while turning the existing Resource API into a proxy would break its read-only
contract and expand its credential and content authority.

Community routers split into two broad groups:

- gateways that provide load balancing, budgets, retries and fallbacks; and
- learned routers that predict whether a request needs a stronger model.

Neither group has OpenUsage Bar's local subscription capacity, multi-account,
source-provenance and missing-versus-zero semantics. The first product slice
must use those facts without becoming a request relay.

## Decision

OpenUsage Bar will add an optional **Local Strategy Routing Plane**. It is an
independent product capability and has no Loom runtime dependency.

### Phase A: content-free decision service

- A new user-private Unix socket hosts a versioned Route Decision API.
- The existing Resource API socket remains GET/HEAD-only and unchanged.
- The resident Python Controller may host both sockets to avoid another idle
  process, but the handlers, schemas, storage and authority remain separate.
- `POST /v1/decisions` accepts only bounded routing metadata: task kind,
  capability tags, Token estimates, privacy/region constraints, policy ID and
  optional anonymous session budget.
- Prompt, Response, messages, tool contents, headers, credentials, raw Provider
  payloads and direct identity are invalid input and are never persisted.
- Hard eligibility filters run before deterministic scoring.
- The default policy is reliability-first. It uses health, fact freshness,
  usable capacity/balance, recent failure rate and bounded latency before cost.
- Unknown, stale, partial and unsupported states remain explicit. A required
  missing fact can reject a target; it never becomes zero or unlimited.
- Every decision includes stable reason codes, alternatives, rejected targets,
  policy version, `dataRevision`, optional `runtimeRevision`, generation time
  and expiry.
- An anonymous session budget is advisory in Phase A. The response must not
  claim an atomic reservation or deduct Provider quota.
- Tie-breaking is deterministic by canonical target ID.

### Policy and decision ownership

ADR 0001 assigned Policy to “Loom or another scheduler.” The Local Strategy
Routing Plane is that optional local scheduler. This ADR does not grant policy
write authority to the Fact or Runtime layers:

| Layer | Exclusive writer | Storage | Authority |
| --- | --- | --- | --- |
| Fact | Collector | `activity.sqlite3` | observed durable resource truth |
| Runtime | Runtime ingestor | `runtime.sqlite3` | bounded recent observations |
| Route policy | Route Controller | strict policy document | target eligibility and scoring configuration |
| Route decision | Route Controller | bounded `routing.sqlite3` | content-free decision evidence only |
| Request execution | optional Proxy | no content ledger | bounded network attempts after opt-in |

`routing.sqlite3` has an explicit retention and size cap. It stores decision
metadata, scores, reason codes and fact revisions, never request content. Exact
limits are frozen in the implementation specification before code lands.

### Phase B: optional execution proxy

- A separate, explicitly enabled OpenAI-compatible loopback proxy may execute a
  Phase A decision.
- It binds only to IPv4 loopback by default and requires a generated local
  one-time bearer credential with only an irreversible private verifier persisted.
- Request content exists only in bounded memory required for forwarding and is
  excluded from logs, facts, Runtime observations and decision history.
- Provider credentials remain behind the existing Python/Keychain boundary.
- Attempts, timeouts and fallbacks are bounded. Once streaming response bytes
  have been emitted, the proxy never switches targets transparently.
- External Provider targets and self-hosted inference-pool targets implement
  separate adapters behind one decision contract.
- Disabling the proxy leaves the menu bar, Provider Center, ledgers, Resource
  API and Decision API usable.

### Learned routing

A learned scorer is not part of the first authority path. It may be added only
after a content-free replay/evaluation harness and shadow-decision evidence
exist. It remains downstream of hard capability, privacy, health and resource
filters and must have an immediate deterministic rollback.

## Alternatives considered

### Add routing to the existing Resource API

- **Pros:** one socket and one public API.
- **Cons:** requires POST/body handling and mixes observed facts with policy
  authority.
- **Why not:** the Resource API's read-only boundary is already public and
  security-tested.

### Start with a LiteLLM or Bifrost proxy

- **Pros:** fast access to a broad Provider surface and existing fallbacks.
- **Cons:** makes request forwarding the architecture before OpenUsage-specific
  quota and provenance decisions are specified.
- **Why not:** Phase A needs a reusable decision contract that can later drive
  multiple proxy implementations.

### Route directly inside SwiftUI

- **Pros:** no new local API.
- **Cons:** duplicates Python facts, moves policy away from the machine
  interface, and risks SwiftUI credential authority.
- **Why not:** SwiftUI remains a presentation and bounded configuration client.

### Make a learned prompt router authoritative immediately

- **Pros:** potentially better quality/cost trade-offs.
- **Cons:** requires request content, training/evaluation evidence and drift
  controls that do not yet exist.
- **Why not:** deterministic, explainable and resource-safe routing is the
  required first capability.

## Consequences

### Positive

- OpenUsage Bar can prevent predictable quota exhaustion without Loom.
- Any local client can consume one stable, explainable decision contract.
- The read-only Resource API and credential isolation remain intact.
- The future proxy can be replaced without rewriting policy semantics.

### Negative

- A second socket and a third bounded database add operational surface.
- Provider/model capability and pricing metadata must be maintained explicitly.
- Phase A decisions are advisory and cannot prevent another process from
  consuming the selected account concurrently.

### Risks and mitigations

- **Stale facts drive a bad route.** Hard freshness policies, expiry and source
  revision echo; fail closed for required facts.
- **Decision logs reveal behavior.** Bounded retention, anonymous references,
  no content and privacy scans.
- **Proxy retries double bill.** Bounded attempts, status-aware retry rules,
  idempotency where supported and no post-stream switching.
- **Policy drift makes results irreproducible.** Immutable policy revisions and
  full reason/fact-version evidence.
- **A learned scorer overrules safety.** Learned scores never bypass hard
  filters and begin in shadow-only mode.

## Acceptance evidence

- Human review of the routing specification and ownership boundary: approved
  on 2026-08-02 before product implementation began.
- Threat-model review of both sockets and the optional proxy.
- Frozen JSON Schemas and N-1 compatibility fixtures.
- TDD proof for stale/unknown data, deterministic tie-breaking, no-route,
  privacy rejection and bounded decision history.
- Independent implementation review before proxy opt-in can ship.

The remaining bullets are implementation and release gates, not conditions on
the architecture decision itself.
