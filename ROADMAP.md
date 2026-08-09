# OpenUsage Bar Roadmap

<!-- openusage-release-state: version=0.8.6 channel=rc api=1.0 canary=0/5 clock=not_started -->
<!-- openusage-build-identity: product=UsageHub candidate=0.8.6 build=28 channel=rc stage=candidate publication=not_published published=v0.7.1 -->

OpenUsage Bar is a local-first AI usage product with two compatible layers:

1. a cross-platform observation product built on the Python collector, SQLite
   fact ledger, read-only Local API v1, SwiftUI on macOS, and Electron/Web on
   macOS, Windows, and Linux;
2. an optional Gateway Core that adds Should-Send decisions, provider proxying,
   bounded response caching, health-aware fallback, PII handling, and streaming
   aggregation without changing the observation product's default behavior.

Technical namespaces (`openusage` paths, CLI, sockets, and Local API v1) remain
compatible. Loom and other agent runtimes are optional consumers and do not
gate an OpenUsage Bar release.

Current published baseline: **v0.7.1** (2026-08-04). The feature branch contains
later v0.8/v0.9 work, but that work is not a stable release until the version,
contract, packaging, and canary gates below pass. The existing external Canary
remains **0 / 5 qualified Apple Silicon Macs** with its 30-day clock **not
started**.

The repository's single candidate truth is now `0.8.6` build `28`, channel
`rc`, across machine state, bundle metadata, Python, Web, Desktop, release
guides, and verification markers. That is candidate preparation, not evidence
that `v0.8.6` has been published or that its Canary clock has started.

## Compatibility invariants

- Fresh install and upgrade default to `observe`; Gateway execution is never
  enabled implicitly.
- `/v1/*` remains a GET/HEAD-only resource API. Stateful or body-bearing
  Gateway calls use a separate `/gateway/v1/*` API and bearer token.
- The collector remains the only writer to the durable fact ledger.
- Gateway telemetry and cache use separate bounded stores; the durable ledger
  never contains prompts, responses, raw chunks, or request payloads.
- Provider credentials stay in macOS Keychain, Windows Credential Manager, or
  Linux Secret Service and are owned by Python, not a renderer.
- Gateway failure cannot stop collector refresh or observation clients.
- macOS keeps the native SwiftUI menu-bar client; Electron/Web supplies the
  shared desktop client on macOS, Windows, and Linux.

## Delivery sequence

| Version | Goal | Status | Exit gates |
| --- | --- | --- | --- |
| 0.4-0.6 Trust foundation | Token semantics, reconciliation, Provider evidence, Adapter Kit, Local API v1, release engineering. | **Released.** | Existing data-integrity, privacy, artifact, N-1, upgrade, and rollback evidence remains valid. |
| 0.7.1 Compatibility stabilization | Finish Infrastructure Boundary, reconcile version truth, preserve GUI parity, and close Local API/client contract defects. | **Current baseline; stabilization in progress.** | Dirty work is separated into reviewable commits; Local API, Provider mutation, credential, and release metadata tests pass without weakened validation. |
| 0.8.0 Cross-platform Observer RC | Ship the observation product on macOS, Windows, and Linux with platform credentials, service registration, and Electron/Web packaging. Establish the disabled-by-default Gateway daemon/schema skeleton in parallel. | **Partially implemented on the feature branch.** | Install, first run, offline use, upgrade, rollback, and uninstall pass on all three platforms; renderer-visible data contains no token or Provider credential. |
| 0.9.0 Advise Preview | Add `/gateway/v1/should-send`, prediction, idempotency, bounded telemetry, and client capability/health surfaces. | **Planned.** | Five Provider fact fixtures pass the same contract on three operating systems; Local API v1 fixtures remain compatible; Should-Send p99 is below 50 ms. |
| 0.9.1 Proxy and Cache Preview | Add five Provider adapters, opt-in proxying, PII-safe L1/L2 cache, and stream aggregation. | **Planned.** | Observe mode has zero regressions; no credential/raw payload reaches ledger, logs, diagnostics, renderer, or artifacts; proxy/cache performance budgets pass. |
| 0.9.2 Fallback Preview | Add circuit breakers, rolling health scores, sticky sessions, replay safety, cost caps, and deterministic fallback outcomes. | **Planned.** | Injected outage, quota, 429, 5xx, timeout, client-abort, and side-effect fixtures pass on all three platforms. |
| 1.0 RC | Freeze cross-platform clients, Gateway API/OpenAPI, Python/TypeScript SDKs, privacy contract, performance budgets, and release artifacts. | **Planned.** | Full Python, Swift, Web, Electron, compatibility, security, packaging, and provenance gates pass. |
| 1.0 Stable | Complete external observation and opt-in Gateway canaries and publish an auditable stable release. | **Planned.** | Existing 5 Apple Silicon × 5 Provider configurations × 30 days gate passes without a blocking incident; additional Windows/Linux cohorts pass their published matrix; N-1 upgrade/rollback, checksum, manifest, SPDX SBOM, provenance, attestation, dependency, privacy, performance, and data-integrity gates pass. |

The compatibility boundary is recorded in
[ADR 0002](docs/adr/0002-optional-cross-platform-gateway-boundary.md). The
detailed implementation runbook is intentionally kept as a local working
document under the ignored `docs/superpowers/plans/` directory and is not a
published contract. Published changes remain recorded in the
[changelog](CHANGELOG.md).
