# OpenUsage Bar Roadmap

<!-- openusage-release-state: version=0.6.0 channel=rc api=1.0 canary=0/5 clock=not_started -->

OpenUsage Bar is a standalone, local-first macOS product. Loom and other local
schedulers are optional read-only API consumers and do not gate any OpenUsage
Bar release.

Current published baseline: **v0.6.0 RC** (2026-07-30). Repository and release
gates are implemented and CI verified. The opt-in external Canary remains at
**0 / 5 qualified Macs** with its 30-day clock **not started**; real-account,
accessibility, and external-machine evidence therefore remains pending.

| Version | Goal | Status | Exit gates |
| --- | --- | --- | --- |
| 0.4.x Hardening | Align public release metadata, freeze Token accounting semantics, add daily reconciliation, and prove installation plus unattended refresh. | **Released.** Repository, unattended refresh, restart, upgrade and rollback evidence is recorded; remaining Provider truth work moved to 0.5. | Build, package, release smoke and visible local recovery evidence pass without weakening Unknown semantics. |
| 0.5 Data Trust | Audit declared Provider capabilities against authoritative sources and real accounts, then publish a reusable Provider Adapter Kit. | **Implemented, live evidence pending.** The kit and conformance fixtures are released; open real-account gates remain tracked per Provider. | First-wave Providers have redacted live evidence; second-wave Providers have an authoritative source or an explicit unsupported result; UI, CLI, and API agree at one `dataRevision`; an external contributor can use the adapter kit. |
| 0.6 RC | Stabilize Local API v1 compatibility, run a public beta, and enforce measured performance budgets. | **Current public pre-release.** v0.6.0 and Local API v1 are published; external qualification is 0 / 5 and the 30-day clock is not started. | N-1 API compatibility passes; external participants verify install, upgrade, rollback and diagnostics; performance meets the recorded baseline; the product remains fully usable without Loom. |
| 0.7 Infrastructure Boundary | Separate the OS-neutral fact contract from the macOS distribution, retire Card-first core paths, and freeze producer interoperability. | **In progress.** PR #53 merged ADR 0001, the OS-neutral contract and machine-readable release state. WQ-19A direct Provider fact collection has passed the complete local release gates; WQ-19B, `openusage-export/v1` and the separate Runtime Observation proposal remain open. | Core adapters emit facts before presentation; the macOS invariant is distribution-only; `openusage-export/v1` has fixtures; any request telemetry uses separate bounded storage. |
| 0.8 Local Smart Routing | Turn resource facts and bounded Runtime observations into explainable Provider/account/model decisions, then add a separately enabled execution proxy. | **Phase A and the local Phase B proxy are implemented; public release remains open.** The deterministic engine, private stores, fact normalization, bounded evidence, Decision API/CLI, explicit connections/targets, policies, Dry Run, explanations and Shadow/Replay remain content-free. The separately enabled IPv4-loopback Chat Completions proxy adds one-time Bearer delivery with only an irreversible private verifier persisted, a default-policy `openusage/auto` model, bounded pre-stream retry, post-first-byte route locking, configuration hot reload and bilingual native management. Provider Center can now explicitly reuse MiniMax, Kimi/Moonshot and Step Plan inference credentials through server-owned site endpoints and isolated routing Keychain accounts; SwiftUI never receives the secret. The complete source build and local `0.8.3 (20)` installed-app acceptance pass, including native Chinese UI, bounded template discovery and DNS-pinned execution transport. A real `0.8.2 → 0.8.1 → 0.8.2` rollback drill also passed with deep-signature, Local API and visible-helper checks. Independent security review and public qualification remain before a public 0.8 release. The published baseline remains 0.6.0. | Content-free Decision API, deterministic reliability-first policy, Provider Center explanations and optional loopback proxy pass privacy, compatibility, performance, fault-injection and release gates without depending on Loom. |
| 1.0 Stable | Complete an external, opt-in, no-telemetry canary and publish an auditable stable release. | **Planned.** Release tooling is implemented and CI verified; the live canary has not run. | At least five external Apple Silicon Macs and five Provider configurations complete 30 days without a blocking incident; each completes N-1 upgrade and rollback; release checksum, manifest, SPDX SBOM, provenance, attestation, dependency, privacy, and data-integrity gates pass. |

Detailed task order and evidence requirements live in the
[current work queue](docs/superpowers/plans/2026-07-18-openusage-work-queue.zh-CN.md).
Published changes are recorded in the [changelog](CHANGELOG.md).
