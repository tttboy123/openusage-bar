# OpenUsage Bar Smart Routing Specification

- Status: Approved for implementation
- Date: 2026-08-02
- Product: OpenUsage Bar, standalone from Loom
- Default objective: reliability first
- Related: [ADR 0003](../../adr/0003-local-strategy-routing.md),
  [community research](../../research/2026-08-02-community-smart-routing.md)

## 1. Objective

Add automatic, local strategy routing that can choose a Provider account and
model from OpenUsage facts without requiring Loom. Delivery is intentionally
split:

1. **Phase A — Route Decision API:** content-free, explainable selection and
   dry-run simulation.
2. **Phase B — optional execution proxy:** an explicitly enabled
   OpenAI-compatible loopback proxy that executes Phase A decisions with
   bounded fallback.

The first production policy is deterministic and reliability-first. Learned
routing remains shadow-only until a replay/evaluation gate proves it.

## 2. Success criteria

The goal is complete only when all of the following are true:

- OpenUsage Bar can route without Loom or any Loom configuration.
- A local client can request a decision and receive one selected target,
  ordered alternatives and rejected targets with stable reason codes.
- Required stale, missing, unsupported or unhealthy facts fail closed; Unknown
  never becomes zero, unlimited or healthy.
- Identical inputs and fact revisions produce the same ordering and reasons.
- The decision includes immutable policy and fact revisions and an expiry.
- Provider, account and model are independently represented.
- Provider Center can configure targets/policies and explain the latest
  decisions without displaying credentials.
- The optional proxy can execute a decision, stream a response and perform only
  safe bounded fallback.
- Prompt, Response, messages, tools, credentials, headers, raw Provider payloads
  and direct identity are absent from routing storage and diagnostics.
- Privacy, dependency, performance, fault-injection, Python, Swift and release
  gates pass without weakening current coverage thresholds.

## 3. Non-goals

- Loom integration, SessionBinding or Loom-owned reservations.
- A cloud account, hosted relay or telemetry upload.
- Atomic Provider quota reservation in Phase A.
- Hidden rewriting of model parameters or request content.
- Automatic prompt classification in the deterministic first release.
- Transparent target switching after streaming output begins.
- Kubernetes or local GPU scheduling in the first external-Provider lane.
- Replacing LiteLLM, Bifrost, Envoy or another complete gateway ecosystem.

## 4. Architecture

```text
Provider sources ──> activity.sqlite3 ──> Resource API (read-only)
Runtime producers ─> runtime.sqlite3  ──> Runtime Summary (read-only)
                                   \
Route targets + policy ------------> Route Controller
                                      │
                                      ├─ router.sock: decisions / simulation
                                      ├─ routing.sqlite3: bounded evidence
                                      └─ optional loopback proxy: execution
```

### 4.1 Process model

Phase A reuses the resident Python Controller process and adds a second Unix
socket. This avoids another idle Agent/process while keeping a different
handler, schema and storage boundary. A crash or disabled router must not stop
fact collection, the menu bar or the Resource API.

Default paths:

```text
Resource API:   ~/.local/state/openusage-bar/openusage.sock
Decision API:   ~/.local/state/openusage-bar/router.sock
Decision store: ~/.local/state/openusage-bar/routing.sqlite3
```

The Decision API socket must be owned by the current user, mode `0600`, not a
symlink and validated before and after bind/connect using the existing Local API
hardening pattern.

Phase B is disabled by default. When enabled, it binds `127.0.0.1` only and
requires a generated bearer token whose raw value is returned once and never
persisted; only an irreversible verifier is stored privately. It never reuses the
Resource API's optional TCP listener.

### 4.2 Storage ownership and bounds

`routing.sqlite3` stores:

- decision ID and UTC time;
- policy ID and immutable policy revision;
- target IDs, normalized component scores and reason codes;
- `dataRevision` and optional `runtimeRevision`;
- anonymous client/session reference when supplied;
- decision expiry and execution attempt outcomes without error bodies.

It must not store request content, model output, tools, headers, credentials,
Provider payloads, endpoint URLs or direct account identity.

Initial bounds:

- retention: 7 days;
- maximum rows: 10,000 decisions plus 30,000 attempt summaries;
- maximum database size: 16 MiB;
- one decision request: 64 KiB;
- one decision response: 256 KiB;
- targets considered: at most 128;
- alternatives returned: at most 16;
- rejected targets returned: at most 128;
- strict JSON duplicate-key and trailing-value rejection.

Oldest rows are removed in a bounded transaction after a successful insert.
A logging failure must not change the selected target, but the response must
report `evidenceStored=false`.

## 5. Route target contract

A route target is a non-secret execution-capable reference:

```json
{
  "schemaVersion": 1,
  "targetId": "openai.work.gpt-5",
  "providerId": "openai",
  "accountRef": "account-1",
  "modelId": "gpt-5",
  "connectionRef": "connection-1",
  "executionClass": "direct_api",
  "executionAdapterId": "openai.direct",
  "resourceMode": "quota",
  "factAccountRef": "account-1",
  "runtimeScopeRef": "anon_0123456789abcdef",
  "balanceCurrency": null,
  "costCurrency": "USD",
  "inputCostMicrosPerMillion": 3000000,
  "outputCostMicrosPerMillion": 3000000,
  "enabled": true,
  "regions": ["global"],
  "privacyClass": "direct_provider",
  "capabilities": ["chat", "reasoning", "tools"],
  "contextWindowTokens": 400000,
  "qualityTier": 4
}
```

Rules:

- IDs use the existing stable public-ID grammar and are capped at 128 bytes.
- `accountRef` and `connectionRef` are opaque local references, never account
  names, e-mail addresses or Provider IDs.
- `factAccountRef` is the exact nullable account scope used to join Resource
  facts. It is separate from the execution account so an unscoped fact never
  silently matches every account.
- `runtimeScopeRef` is either null or an anonymous `anon_...` Runtime scope.
  Runtime observations never join on an account name or direct identity.
- `executionClass` is one of `direct_api`, `subscription_cli`,
  `openai_compatible` or `self_hosted`.
- `executionAdapterId` names an installed execution adapter; discovery alone
  does not make that adapter or its connection available.
- `resourceMode` is `quota` or `balance`. A mode without a complete compatible
  fact adapter remains unavailable rather than being treated as unmetered.
- `balanceCurrency` is null for quota targets and the exact uppercase native
  currency for balance targets. Balances are never converted using an implicit
  or network-fetched exchange rate.
- cost metadata is either entirely absent or declares one uppercase currency
  plus input and output price microunits per million tokens. It is versioned
  with the target document and contains no credential material.
- capability tags are from a versioned allowlist; unknown tags fail closed.
- context window and quality tier are declared metadata with source/revision,
  not inferred from a recent request.
- endpoint and credential material remain in Provider configuration/Keychain
  and are not part of this contract.
- a discovered Provider is not automatically routeable. It needs an enabled
  target and a compatible execution adapter.

Target configuration is a strict, atomically replaced versioned document owned
by the Python Controller. SwiftUI submits bounded mutation commands but never
reads secrets.

## 6. Policy contract

The built-in immutable policies are:

| Policy | Hard behavior | Score emphasis |
| --- | --- | --- |
| `reliable` | rejects incompatible, disabled, privacy/region-invalid, unhealthy and resource-exhausted targets | reliability 50%, headroom 25%, latency 15%, cost 10% |
| `balanced` | same hard safety filters | reliability 35%, headroom 25%, latency 20%, cost 20% |
| `economy` | same hard safety filters plus known-cost preference | reliability 30%, headroom 20%, latency 10%, cost 40% |
| `fast` | same hard safety filters plus Runtime freshness requirement when multiple candidates exist | reliability 35%, headroom 15%, latency 40%, cost 10% |
| `private` | only `local_only` or policy-approved `direct_provider` targets | reliability 50%, headroom 25%, latency 15%, cost 10% |

Weights are integers summing to 100. Custom policies may change weights and
thresholds but cannot disable content rejection, credential isolation, Unknown
semantics, deterministic tie-breaking or socket bounds.

Every policy has:

- `policyId` and monotonic immutable `policyRevision`;
- minimum quota headroom and balance reserve;
- maximum source age;
- maximum recent error rate;
- required Runtime sample count, if any;
- one comparison currency, minimum native-currency balance reserve and balance
  score reference;
- per-window reserve floors;
- allowed execution/privacy classes, Providers and targets;
- retry/fallback attempt limits used by Phase B.

## 7. Decision request

`POST /v1/decisions` accepts:

```json
{
  "schemaVersion": "1.0",
  "clientRequestRef": "req_0123456789abcdef",
  "policyId": "reliable",
  "task": {
    "kind": "code",
    "requiredCapabilities": ["chat", "reasoning", "tools"],
    "estimatedInputTokens": 12000,
    "maxOutputTokens": 4000,
    "minimumContextWindowTokens": 16000,
    "privacy": "direct_provider",
    "regions": ["global"]
  },
  "constraints": {
    "allowProviders": [],
    "denyProviders": [],
    "allowTargets": [],
    "denyTargets": [],
    "maximumEstimatedCostMicrounits": null,
    "costCurrency": null
  },
  "session": {
    "sessionRef": "anon_0123456789abcdef",
    "remainingBudgetMicrounits": null,
    "reserveMicrounits": null,
    "budgetCurrency": null
  }
}
```

`clientRequestRef` and `session` are optional. All sets are capped at 50 IDs.
The server rejects unknown fields, duplicate keys, booleans where numbers are
required, noncanonical IDs, negative values and arithmetic overflow.

`task.kind` is one of `chat`, `code`, `reasoning`, `embedding`, `image`,
`audio` or `other`. `task.privacy` is one of `local_only`, `direct_provider` or
`allow_proxy`. All other strings are strict IDs, enum values or anonymous
references rather than free text. A content-bearing key is therefore an unknown
field and fails schema validation; content, credentials, authorization headers,
direct identity and Provider payload fields have no representable wire field.
The public API documents that clients pass task metadata only.

Cost limits require `costCurrency`; session budget and reserve require one
matching `budgetCurrency`. A target cost expressed in another currency is
Unknown for that comparison and fails closed. Built-in cross-target cost scores
use USD only; native CNY or credit balances can still pass their own reserve
check under reliability policies but are not silently compared as USD.

## 8. Eligibility and scoring

### 8.1 Hard filters, in order

1. target enabled and execution adapter available;
2. request allow/deny lists;
3. task capability and context-window compatibility;
4. privacy class and region compatibility;
5. Provider instance/connection exists and is not explicitly disabled;
6. required source health is not authentication-failed or unavailable;
7. required capacity/balance facts are covered, usable and within freshness;
8. quota/balance remains above policy reserve after the estimated request;
9. recent failure rate is below the hard threshold when the minimum sample
   count is met;
10. optional session budget remains above its reserve.

Stable rejection codes include:

```text
target_disabled
adapter_unavailable
not_allowed
capability_missing
context_too_small
privacy_incompatible
region_incompatible
connection_unavailable
source_unhealthy
fact_missing
fact_stale
coverage_partial
quota_reserve_exceeded
balance_reserve_exceeded
error_rate_exceeded
session_budget_exceeded
cost_unknown
cost_limit_exceeded
```

Multiple codes may apply, but the engine returns them in the fixed order above.

### 8.2 Deterministic score

Eligible targets receive normalized integer component scores `0...10000`:

- reliability: source health, freshness and recent success/error evidence;
- headroom: applicable quota windows or API balance after policy reserve;
- latency: recent duration and TTFT p95 with sample count;
- cost: Provider-reported or catalog-estimated request cost.

Unknown optional data receives an explicit policy-defined `unknownPenalty`; it
is never encoded as a real zero measurement. Components include their evidence
state and reason. Weighted arithmetic uses integers and checked bounds. Final
ties resolve by canonical `targetId` ascending.

`qualityTier` is a hard/manual preference only in v1 and is not silently folded
into a learned score. A future learned scorer gets a separate named component
and policy revision.

### 8.3 Fact consistency

The engine reads one durable Resource Snapshot transaction and one independent
Runtime Summary. It reports both revisions and does not claim a transaction
across databases. The decision expires at the earliest of:

- policy maximum decision age;
- applicable source freshness boundary;
- quota reset;
- Runtime window expiry.

Official facts and fallback facts retain the selected source/quality semantics;
the engine never sums overlapping sources.

For quota targets, every Provider/account/model-applicable quota window is a
simultaneous constraint. Normalization uses the smallest known remaining ratio;
one missing window makes coverage partial, and any stale applicable window
makes the target stale. Ratios convert to basis points by flooring, so a value
just below a reserve boundary cannot round upward into eligibility. Runtime
quality joins only on the explicit Provider/model/anonymous scope tuple.

For balance targets, normalization selects only the exact
Provider/account/currency scope and never sums overlapping records. Decimal
balances floor to native-currency microunits. The target is eligible only when
the request cost is known in the same currency and the post-request balance is
at or above the policy reserve. Missing, duplicate, stale or mismatched-currency
facts remain missing, partial or stale rather than becoming zero or unlimited.

## 9. Decision response

Successful selection returns HTTP `200`:

```json
{
  "schemaVersion": "1.0",
  "decisionId": "route_0123456789abcdef",
  "generatedAt": "2026-08-02T12:00:00Z",
  "expiresAt": "2026-08-02T12:00:30Z",
  "policy": {"policyId": "reliable", "policyRevision": 1},
  "facts": {"dataRevision": 60000, "runtimeRevision": 120},
  "selected": {
    "targetId": "openai.work.gpt-5",
    "providerId": "openai",
    "accountRef": "account-1",
    "modelId": "gpt-5",
    "score": 8730,
    "reasons": ["healthy_source", "quota_headroom", "low_recent_error_rate"]
  },
  "alternatives": [],
  "rejected": [],
  "warnings": [],
  "evidenceStored": true
}
```

If no candidate is eligible, the API returns HTTP `409` with code `no_route`
and the bounded rejected target list. It must not select a fallback target that
violates a hard filter.

Other stable errors include `invalid_request`, `request_too_large`,
`policy_not_found`, `facts_unavailable`, `runtime_unavailable` when required,
`router_busy` and `internal_error`. Messages are sanitized and never include a
path, credential, Provider payload or input substring.

## 10. Phase A API surface

```text
GET  /v1/health
GET  /v1/schema.json
GET  /v1/policies
GET  /v1/targets
POST /v1/decisions
POST /v1/simulations
GET  /v1/decisions?before=<cursor>&limit=1..100
GET  /v1/decisions/<decisionId>
```

`simulations` runs the same engine against an explicit frozen fixture or current
facts but never stores a decision and sets `simulated=true`. Policy and target
mutations continue through the existing bounded Provider Settings Controller,
not through this API.

The server supports HTTP/1.1 only, one request per connection, fixed header/body
limits, bounded threads, timeouts, sanitized JSON errors and no redirects.

## 11. Provider Center and human interface

Add a `Routing` section with:

- master toggle for Decision API;
- built-in policy selector and custom policy editor;
- route targets grouped by Provider and account;
- an explicit Provider Center reuse flow for managed inference credentials;
- missing execution adapter/capability/fact warnings;
- dry-run form with selected, alternative and rejected explanations;
- recent content-free decision history;
- separate, unmistakable Phase B proxy toggle and local endpoint/token reset;
- Chinese and English strings, keyboard navigation and VoiceOver labels.

Provider credential reuse is allowlisted by concrete Provider type. The
Controller, not SwiftUI, owns each China/international endpoint and reads the
source Keychain account only after explicit confirmation. It copies the value
to an isolated routing Keychain account; neither the source nor copied value is
returned over JSON. Billing/admin keys and custom usage feeds are never inferred
to be inference credentials. Missing native Keychain access reports unavailable
state and cannot silently create a connection.

The menu bar remains a quick resource view. It may show the current default
policy and latest no-route health badge, but it must not add a dense routing
dashboard.

## 12. Phase B optional proxy

Initial compatibility:

```text
POST /v1/chat/completions
GET  /v1/models
```

The model field may name an explicit target or a virtual route such as
`openusage/reliable`. The normal response `model` identifies the selected public
model and response headers expose an opaque route decision ID; no response
metadata exposes account or connection references.

Execution rules:

- resolve one fresh Phase A decision per request;
- reject expired decisions before the first Provider attempt;
- maximum three total attempts and policy-configured per-target timeout;
- retry only documented transient transport, `408`, `429` and eligible `5xx`
  outcomes;
- authentication, invalid request and capability errors do not retry;
- a target that emits any streaming response bytes is final for that request;
- record only target, timestamps, status class, Token/cost facts and sanitized
  attempt reason; never the error body;
- proxy failure cannot mutate Provider credentials or resource facts;
- generated bearer token never appears in UI after creation and is replaceable,
  not retrievable.

The Responses API, embeddings, images, audio and local inference pools are
separate additive slices after chat completion compatibility is proven.

## 13. Code structure

Planned Python modules:

```text
openusage_bar/routing_contract.py   immutable request/response value objects
openusage_bar/routing_targets.py    strict non-secret target configuration
openusage_bar/routing_policy.py     built-in/custom policy validation
openusage_bar/routing_engine.py     pure filters, score and explanations
openusage_bar/routing_facts.py      Resource/Runtime normalization
openusage_bar/routing_store.py      bounded content-free SQLite evidence
openusage_bar/routing_api.py        strict Unix-socket HTTP surface
openusage_bar/routing_proxy.py      Phase B loopback execution boundary
```

SwiftUI uses existing Provider Settings and UsageCore patterns; no third-party
UI, chart, database, state-management or dependency-injection package is added.
Python uses standard library and existing dependencies before adding anything.

## 14. Implementation slices and TDD gates

### Slice A1: contracts and pure engine

- freeze JSON fixtures, reason order and built-in policies;
- RED tests for Unknown/stale/partial, capability mismatch, reserve exhaustion,
  integer overflow, deterministic ties and no-route;
- implement immutable contracts and pure engine;
- property-style table tests across policy/target/fact mutations.

### Slice A2: fact adapter and decision store

- read current Resource Snapshot plus independent Runtime Summary;
- prove no SQLite writes while reading facts;
- implement bounded routing database and retention;
- prove content/credential/direct-identity sentinels never persist.

### Slice A3: Decision API and CLI

- strict UDS tests for mode/owner/symlink/TOCTOU;
- duplicate JSON, oversized body/header, invalid HTTP, slow client and bounded
  concurrency tests;
- frozen JSON Schema and N-1 additive compatibility fixtures;
- add `openusage-bar route decide|simulate|history --format json`.

### Slice A4: Provider Center

- target/policy configuration, validation and rollback on save failure;
- Chinese/English, keyboard and VoiceOver tests;
- dry-run and explanation views using the same wire decoder as CLI/API.

### Slice B1: optional chat proxy

- RED tests for disabled-by-default, loopback-only, bearer auth, request limits,
  in-memory content, safe retry graph and no post-stream switch;
- frozen fake Provider adapters; no real credentials in tests;
- fault injection for timeout, 429, 5xx, malformed stream and disconnect.

### Slice B2: evaluation and release

- deterministic replay report and shadow-decision mode;
- Shadow accepts the normal metadata-only request plus one public actual target
  ID, writes a separate bounded comparison and never writes a normal decision;
- Replay accepts 1–256 frozen, canonically ordered cases, rejects duplicate
  case/target/fact identifiers, reads no live facts and writes no evidence;
- the Decision, Shadow and Replay request contracts are separately frozen JSON
  Schemas returned additively by the private schema endpoint;
- CPU/memory/wakeup and p95 decision overhead baselines;
- privacy/secret/dependency/full-history scans;
- complete Python and Swift suites, product coverage at existing thresholds;
- build, packaged smoke, deep signing and install/upgrade/rollback checks;
- independent security and implementation review.

## 15. Performance budgets

On the repository's recorded lightweight Apple Silicon baseline:

- pure decision engine p95: at most 5 ms for 128 targets;
- current-fact Decision API p95: at most 50 ms after warm startup;
- idle Decision API: no polling and no additional wakeup timer;
- resident memory increase: at most 20 MiB;
- routing database pruning: at most 100 ms per bounded batch;
- proxy routing overhead before Provider network time: p95 at most 20 ms;
- limits are measured, not relaxed, if a test environment is slower.

## 16. Security and privacy verification

Mandatory tests and scans cover:

- Prompt/Response/message/tool/header/cookie/API-key/e-mail sentinels;
- socket ownership, permissions, symlink and replacement races;
- loopback-only proxy and bearer comparison;
- request smuggling, duplicate lengths, chunking policy and slow clients;
- bounded concurrency, bodies, candidates, attempts and retention;
- error sanitization and log redaction;
- Keychain values passed only through existing stdin/helper boundaries;
- no environment-variable credential inheritance in resident helpers;
- no source weakening of current Resource API GET/HEAD-only behavior.

## 17. Compatibility and rollout

- Decision API starts at schema `1.0` and has its own compatibility document.
- Resource API v1 gains no POST route and no breaking field.
- Routing is disabled when migrating from an older installation until target
  configuration validates successfully.
- Phase B remains a separate opt-in after Phase A ships.
- A route decision is not presented as a Provider reservation or guarantee.
- Canary must include at least two Provider families, two accounts in one
  family, stale facts, quota reset, Runtime missing, 429 and streaming failure.

## 18. Human review decisions requested

Approval of this specification freezes these choices:

1. Phase A uses a second Unix socket but reuses the resident Python process.
2. Decision evidence is retained for 7 days in a separate bounded database.
3. Reliability-first is the default and hard filters cannot be weight-disabled.
4. The initial target catalog is explicit; discovery alone never makes a
   Provider routeable.
5. Phase B is a separate opt-in loopback proxy and begins with Chat Completions.
6. Learned routing begins only in shadow mode after deterministic replay.

Changing one of these after implementation begins requires an ADR amendment and
updated fixtures before code changes.

Human review approved all six decisions on 2026-08-02. WQ-26 may proceed under
the TDD and release gates in this specification.
