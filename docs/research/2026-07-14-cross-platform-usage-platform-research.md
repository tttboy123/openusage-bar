# Cross-platform AI Usage Platform Research

Date: 2026-07-14
Status: Research complete, product scope approved

## Product boundary clarification

The product being optimized and iterated is **OpenUsage Bar**. OpenUsage.sh is an upstream data source and reusable collection dependency, not the product being redesigned. OpenUsage Bar owns the native interface, activity ledger, provider management, subscription enrichment, normalized machine contract, data-health experience, and future scheduler-facing read model.

The separate Swift project named OpenUsage is research input only. It is not an implementation target and is not part of the installed product identity.

Approved delivery boundary:

- Optimize and iterate OpenUsage Bar on macOS.
- Connect as many relevant AI providers and local clients as practical.
- Do not build Windows or Linux applications in the current roadmap.
- Preserve future portability through provider-neutral schemas, capability descriptors, fixtures, and machine interfaces rather than a cross-platform UI framework.

## Decision summary

OpenUsage Bar should become a native usage product and integration layer over a provider-neutral protocol. It should not grow into a second monolithic collector when OpenUsage.sh already supplies the required facts.

Recommended direction:

1. Reuse OpenUsage.sh as the cross-platform collection and historical reporting core where it already has coverage.
2. Keep the current Python adapters temporarily for missing subscription sources such as the existing StepFun, MiniMax, and Kiro enrichments.
3. Normalize both into a versioned Usage Protocol v1.
4. Serve the same normalized data through CLI JSON, a loopback read-only API, SQLite-derived history, and the macOS SwiftUI client.
5. Keep UI shells platform-native. SwiftUI remains the macOS implementation; Windows and Linux are later shells over the same protocol.

## What was researched

- CodexBar
- ClaudeBar
- OpenUsage.sh, the cross-platform Go terminal product used by this local project
- OpenUsage, the separate native Swift macOS product
- Quotio
- OpenCode Bar
- AgentBar
- ccusage
- Tokdash
- Apple, Microsoft, and freedesktop platform status surfaces

## Competitive findings

### CodexBar

CodexBar is the strongest reference for provider breadth and acquisition strategies. Its provider descriptors declare capabilities and ordered fetch strategies such as CLI, OAuth, API token, browser cookies, local probes, and web dashboards. It exposes JSON through a CLI and a loopback `serve` mode.

What to migrate:

- Descriptor-driven provider registry
- Ordered source fallback
- Multi-account isolation
- Last-good response behavior
- Provider capability and authentication diagnostics
- One merged menu-bar icon

What not to copy:

- Provider-specific display models as the external data contract
- Treating every metric as a visually similar quota bar

Source: https://github.com/steipete/CodexBar

### ClaudeBar

ClaudeBar is a useful native SwiftUI and provider-TDD reference. It offers enable/disable controls, session and weekly quota presentation, notifications, and a layered provider architecture. It does not document a unified long-term history or stable external API.

Source: https://github.com/tddworks/ClaudeBar

### OpenUsage.sh

The locally installed OpenUsage 0.23 line belongs to the Go-based OpenUsage.sh project. Its current product already advertises:

- macOS, Linux, and Windows binaries
- 35 provider integrations
- Daily, weekly, monthly, session, and block reports
- Model and Token breakdowns
- A background daemon and local SQLite history
- JSON and CSV export
- Prometheus metrics
- Local tool integrations and hooks

This should remain the default collection and history source rather than being replaced by another custom parser.

Source: https://github.com/janekbaraniewski/openusage

### OpenUsage native Swift edition

The separate Swift OpenUsage project has a narrower provider list but a stronger machine contract. Its `/v1/limits` response uses stable provider and resource IDs, raw scalar values, explicit units, freshness, reset timestamps, and errors. Missing values are omitted instead of fabricated as zero, and refresh failure preserves the last-good provider snapshot.

This is the best reference for Usage Protocol v1 limits semantics.

Source: https://github.com/robinebers/openusage/blob/main/docs/local-http-api.md

### Quotio

Quotio combines provider accounts, local proxy routing, real-time traffic, quota, and failover. Its account-pool and routing concepts are relevant to a future scheduler, but routing mutations must remain outside the first usage-monitor release.

Source: https://github.com/nguyenphutrong/quotio

### ccusage and Tokdash

ccusage is a strong reference for local-client log coverage and JSON aggregation. Tokdash is a strong reference for local history, quota snapshots, heatmaps, session drill-down, and local API surfaces.

Sources:

- https://github.com/ccusage/ccusage
- https://github.com/JingbiaoMei/Tokdash

## Required semantic model

The normalized layer must keep four metric families physically and semantically separate:

1. `subscription_quota`
   - Session, five-hour, weekly, monthly, and model-specific windows
   - Used, limit, remaining, ratio, reset, and window duration

2. `token_activity`
   - Input, output, cache read, cache creation/write, reasoning, and total Tokens
   - Provider, local client, model, day, session, and project dimensions when available

3. `billing`
   - Estimated cost, actual spend, credits, grants, and account balance
   - Explicit currency or native credit unit

4. `operational`
   - Requests, failures, latency, rate limits, provider status, and source health

Every observation carries:

- Stable provider, resource, and model IDs
- Stable pseudonymous account ID when available
- Source kind and provenance
- Observed, period-start, period-end, and expiry timestamps
- Data state and freshness
- Exact or estimated quality
- Native unit

Token accounting follows an inclusive-total rule. Provider-native input and output totals remain authoritative totals; cache-read, cache-creation, and reasoning values are typed subsets and are not added a second time. Monetary values use decimal strings with an explicit currency and basis such as `provider_reported`, `invoice_reconciled`, or `price_table_estimated`.

Required data states:

- `ok`
- `unsupported`
- `not_configured`
- `auth_expired`
- `temporarily_unavailable`
- `stale`

Unsupported, unavailable, failed, missing, stale, and actual zero values must never collapse into the same representation.

## Provider capability descriptor

Each adapter declares capabilities before collection:

- Supported operating systems
- Supported regions
- Authentication and credential source
- Quota windows
- Token history
- Model breakdown
- Multiple accounts
- Reset timestamps
- Billing, balance, and cost
- Rate limits and service status
- Source stability: official API, CLI, local log, local app state, or reverse-engineered endpoint
- Default freshness and timeout

The UI and scheduler use these declarations to degrade intentionally rather than infer meaning from missing fields.

## Machine-readable contract

SQLite remains an internal implementation detail. External consumers use a versioned protocol.

Initial read-only surfaces:

```text
openusagebar capabilities --json
openusagebar snapshot --json
openusagebar history --from ... --to ... --bucket day --json|csv

GET /v1/health
GET /v1/capabilities
GET /v1/snapshot
GET /v1/limits
GET /v1/activity/daily?from=&to=&provider=&model=
GET /v1/quota/history?from=&to=&provider=
```

Contract rules:

- Every response identifies its schema version and generation time.
- IDs, units, timestamps, and scalar values are contract fields.
- Localized labels, colors, chart geometry, and SwiftUI state are not contract fields.
- Collection and parse errors are structured and provider-scoped.
- A failed refresh never replaces a last-good observation with zero.
- Bounded queries have deterministic ordering and pagination or cursors.
- Incremental consumers can use `observedAfter` plus a stable observation ID.
- HTTP binds only to loopback, rejects non-loopback Host headers, and exposes no credentials or full account identity.
- Automatic provider switching is not part of the read-only protocol.

The ledger maintains an append-only change log in the same SQLite transaction as each canonical record update. Each entry contains a monotonic sequence, stable record ID, revision, operation, timestamp, normalized payload, and payload hash. Identical observations do not produce revisions. Historical corrections reuse the same record ID and increment its revision. Consumers first read a consistent snapshot and its high-water cursor, then request changes after that cursor. Expired cursors return an explicit reset-required error rather than silently skipping changes.

Preferred machine outputs are ordered as follows:

1. Cross-platform CLI JSONL for snapshots and cursor-based changes.
2. Read-only Unix socket on macOS/Linux or named pipe on Windows.
3. Optional authenticated loopback HTTP for broad client compatibility.
4. OpenMetrics for low-cardinality operational monitoring, not ledger synchronization.
5. Read-only MCP resources for agent summaries, not bulk history transport.

Any TCP endpoint requires a local bearer secret, Origin validation, no permissive CORS, rate limiting, and explicit opt-in. The current Swift OpenUsage permissive loopback CORS behavior is useful for compatibility research but is not an acceptable default for this product.

For a future scheduler, a derived capacity view may expose:

- `available`
- `remainingRatio`
- `resetsAt`
- `freshnessSeconds`
- `confidence`
- `estimatedCostPerMillionTokens`
- `constraints`

These values support scheduling decisions without allowing the usage monitor to mutate routing.

The canonical store and every export exclude API keys, session tokens, cookies, prompts, responses, tool arguments, raw request IDs, and direct account identity. Stable account references use an installation-scoped HMAC so schedulers can distinguish accounts locally without recovering an email or provider identifier.

## Platform architecture

```text
OpenUsage.sh collectors + temporary local enhancement adapters
                            |
                    Usage Protocol v1
                            |
          Local ledger, aggregation, and last-good cache
              /             |                \
         CLI JSON     Loopback HTTP       Native UI shells
                                             |
                                macOS SwiftUI first
```

Recommended platform strategy:

- macOS: SwiftUI, Swift Charts, AppKit bridging where required
- iOS and iPadOS: later read-only companion and WidgetKit, not a menu-bar clone
- Windows: later WinUI 3 main window plus notification-area integration
- Linux: later GTK4/Libadwaita window plus StatusNotifierItem when the desktop host supports it

A single cross-platform web shell is not recommended for the primary desktop experience. Shared protocol, design tokens, chart behavior, fixtures, and screenshot contracts provide consistency without sacrificing native platform quality.

## Product information architecture

The menu bar answers only immediate questions:

- Which capacity is closest to exhaustion?
- How many Tokens were used today?
- Is any source stale or failing?
- When is the next relevant reset?

The SwiftUI client uses task-oriented sections:

1. Overview
2. Capacity
3. Activity
4. API Spend
5. Local Tools
6. Providers and Accounts
7. Data Health

The Activity section retains the approved annual single-color square heatmap and 30-day model stacked chart. Model colors never double as risk colors. Quota and Token activity remain in separate sections.

## Design assessment

The product is a dense native developer resource console. Recommended dials:

- Design variance: 4/10
- Motion: 2/10
- Density: 7/10

Design Taste is useful here for anti-slop, hierarchy, color discipline, state completeness, and avoiding decorative card grids. Apple HIG and SwiftUI remain authoritative for native controls, accessibility, charts, focus, reduced motion, and platform behavior.

## Three possible approaches

### A. Protocol-first native shells, recommended

Reuse OpenUsage.sh collectors, introduce Usage Protocol v1, keep SwiftUI for macOS, and add OS-specific shells later.

Advantages:

- Maximum reuse and lowest data duplication
- Best native UI quality
- Stable scheduler integration
- Provider and OS growth are independent

Cost:

- Requires a carefully versioned protocol
- Multiple UI implementations later

### B. Extend the current Python application into the platform

Continue adding adapters, storage, HTTP, CLI, and OS UI around the current Python package.

Advantages:

- Fastest short-term reuse of local custom adapters

Cost:

- Duplicates OpenUsage.sh capabilities
- Creates two histories and two provider models
- Makes Windows and Linux packaging harder

### C. One Tauri or Electron desktop application

Move the UI to one cross-platform web shell while retaining a native or Go collector.

Advantages:

- One UI codebase
- Faster Windows and Linux visual parity

Cost:

- Weaker macOS menu-bar fit and native accessibility
- Platform tray differences remain
- Conflicts with the approved SwiftUI direction

## Recommendation and scope

Choose Approach A.

Add Protocol v1 and the Provider Capability Model as Phase 0.5 before the SwiftUI views bind to storage. Continue the approved macOS vertical slice. Do not add Windows/Linux UI or automatic routing to the same implementation plan.

The first release should prove:

1. The same normalized facts drive menu bar, SwiftUI, CLI JSON, and loopback API.
2. Current OpenUsage.sh history remains reusable.
3. StepFun, MiniMax, Kiro, and other missing sources fit through the same capability and observation contracts.
4. A scheduler can read facts and constraints without credentials or SQLite knowledge.

## Confirmed scope

`All platforms` means broad AI provider and local-client coverage inside the macOS OpenUsage Bar product. Windows and Linux UI work is explicitly deferred. The data and adapter boundaries remain portable.
