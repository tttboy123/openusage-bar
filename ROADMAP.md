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
| 0.7 Infrastructure Boundary | Separate the OS-neutral fact contract from the macOS distribution, retire Card-first core paths, and freeze producer interoperability. | **Planned.** ADR 0001 and the first P0 implementation slice are in progress; Runtime Observation remains a separate proposal. | Core adapters emit facts before presentation; the macOS invariant is distribution-only; `openusage-export/v1` has fixtures; any request telemetry uses separate bounded storage. |
| 1.0 Stable | Complete an external, opt-in, no-telemetry canary and publish an auditable stable release. | **Planned.** Release tooling is implemented and CI verified; the live canary has not run. | At least five external Apple Silicon Macs and five Provider configurations complete 30 days without a blocking incident; each completes N-1 upgrade and rollback; release checksum, manifest, SPDX SBOM, provenance, attestation, dependency, privacy, and data-integrity gates pass. |

Detailed task order and evidence requirements live in the
[current work queue](docs/superpowers/plans/2026-07-18-openusage-work-queue.zh-CN.md).
Published changes are recorded in the [changelog](CHANGELOG.md).
