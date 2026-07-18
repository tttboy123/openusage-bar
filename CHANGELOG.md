# Changelog

This project follows [Semantic Versioning](https://semver.org/).

## Unreleased

## 0.4.0 - 2026-07-18

### Added

- Native bilingual control center with menu-bar capacity, Usage Details,
  Provider Center, onboarding, and automation diagnostics.
- Versioned read-only local API and CLI snapshots for independent resource
  consumers; Loom remains optional and out of process.
- Provider runtime registry with isolated quota, Token usage, and monetary cost
  fact pipelines.
- Multi-window quota provenance, opaque multi-account isolation, and discovery
  aliases for GLM, Kimi, Qwen, Claude, and OpenCode families.
- Version 2 custom quota, daily Token, and daily monetary cost feeds with
  version 1 migration compatibility.
- Synthetic Provider Conformance Kit covering failure isolation, last-good
  preservation, source priority, pagination bounds, privacy, and Unknown-not-zero.

### Changed

- All app and helper bundles now share release version 0.4.0 and build 4.
- Release metadata is verified against immutable tags, build history, and the
  CHANGELOG; GitHub Actions are pinned to full official commit SHAs.

### Fixed

- Provider-level refresh failures no longer suppress independent facts.
- Keychain lookup expressions no longer trigger false-positive literal-secret
  scans while hard-coded credentials remain blocked.

## 0.3.0 - 2026-07-18

### Added

- Chinese-first README with a concise English companion README.
- Inline Provider Center credential editing for managed providers.
- Multi-account provider management without credential echoing.
- Simplified Step Plan management through the Provider interface.
- Simplified Chinese localization for provider management.
- Reproducible source bootstrap and public release verification.
- Repository and Git-history credential scanning.
- GitHub Actions gates for Python, Swift, privacy, and clean-checkout builds.

### Fixed

- Public release documentation excludes internal research notes and real local
  usage screenshots.
- Provider and source health are reported separately.
- Installer waits for the collector during app replacement.
- Swift tests keep isolated preferences in memory instead of leaking plist
  files.

## 0.2.0 - 2026-07-17

### Added

- Native SwiftUI menu-bar host and Activity application.
- Canonical local SQLite activity ledger and read-only Unix-socket API.
- Provider catalog covering OpenUsage families plus MiniMax and Step Plan.
- Subscription capacity for Codex, MiniMax, Cursor, Kiro, and StepFun where
  authoritative local or official data is available.
- Daily token activity, model trends, quota history, API spend, provider
  visibility, custom HTTPS providers, and custom daily token feeds.
- Keychain-backed credentials, bounded provider subprocesses, last-good data,
  atomic installation, rollback, privacy scans, and 80 percent coverage gates.
