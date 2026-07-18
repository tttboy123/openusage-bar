# OpenUsage Bar Roadmap

OpenUsage Bar is a standalone, local-first macOS product. Loom and other local
schedulers are optional read-only API consumers and do not gate any OpenUsage
Bar release.

Current published baseline: **v0.4.2** (2026-07-18). Its repository gates are
implemented and CI verified; clean-machine, real-account, accessibility, and
external-canary evidence is still pending.

| Version | Goal | Status | Exit gates |
| --- | --- | --- | --- |
| 0.4.x Hardening | Align public release metadata, freeze Token accounting semantics, add daily reconciliation, and prove installation plus unattended refresh. | **Current.** v0.4.2 is published; repository implementation and CI are verified, with live verification pending. | Build, package, and release-smoke gates pass; one real Token discrepancy is explained; clean install, automatic refresh, restart, N-1 upgrade, and rollback pass visible verification. |
| 0.5 Data Trust | Audit declared Provider capabilities against authoritative sources and real accounts, then publish a reusable Provider Adapter Kit. | **Planned.** Synthetic conformance coverage exists; real-account evidence is pending. | First-wave Providers have redacted live evidence; second-wave Providers have an authoritative source or an explicit unsupported result; UI, CLI, and API agree at one `dataRevision`; an external contributor can use the adapter kit. |
| 0.6 RC | Stabilize Local API v1 compatibility, run a public beta, and enforce measured performance budgets. | **Planned.** `/v1/snapshot` and `/v1/schema.json` are implemented and CI verified; compatibility and external-beta evidence is pending. | N-1 API compatibility passes; external participants verify install, upgrade, rollback, and diagnostics; performance meets the recorded baseline; the product remains fully usable without Loom. |
| 1.0 Stable | Complete an external, opt-in, no-telemetry canary and publish an auditable stable release. | **Planned.** Release tooling is implemented and CI verified; the live canary has not run. | At least five external Apple Silicon Macs and five Provider configurations complete 30 days without a blocking incident; each completes N-1 upgrade and rollback; release checksum, manifest, SPDX SBOM, provenance, attestation, dependency, privacy, and data-integrity gates pass. |

Detailed task order and evidence requirements live in the
[current work queue](docs/superpowers/plans/2026-07-18-openusage-work-queue.zh-CN.md).
Published changes are recorded in the [changelog](CHANGELOG.md).
