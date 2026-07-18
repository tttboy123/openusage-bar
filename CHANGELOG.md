# Changelog

This project follows [Semantic Versioning](https://semver.org/).

## Unreleased

### Changed

- OpenUsage daily scans now allow 60 seconds so large local histories are not
  discarded by the previous 30-second process timeout.
- Providers with an official daily usage adapter now select the official result
  first and atomically fall back to OpenUsage only when the official source
  fails. Fallback rows are marked with source `openusage.daily` and quality
  `fallback`; the two sources are never added together.
- Codex daily Token history again uses the shared OpenUsage collector as its
  primary source, while its local rate-limit adapter remains responsible only
  for subscription capacity.

### Fixed

- Empty or failed OpenUsage scans no longer replace a last-good range with
  covered zero usage. Source health records `empty_result` or the sanitized
  failure while preserving the prior rows and coverage.
- Daily activity details now expose each day's raw source IDs, quality IDs, and
  collection time in the chart tooltip and accessibility summary.

## 0.4.2 - 2026-07-18

### Added

- A drag-to-install DMG with a standard Applications shortcut for normal macOS
  installation without Terminal commands.
- First-launch registration of the menu-bar login item and bundled collector
  through Apple's Service Management framework.

### Changed

- Initial and login launches stay in the menu bar; explicitly reopening the app
  continues to open recovery details when needed.
- The ZIP and transactional scripts remain available for advanced repair,
  rollback, and automation instead of being the primary install path.
- Gatekeeper documentation uses per-app Privacy & Security approval and never
  asks users to disable system protection globally.

## 0.4.1 - 2026-07-18

### Changed

- Release and CI builds use the same Xcode 26.6 toolchain and keep the 80%
  coverage gate focused on deterministic Swift product logic; native hosting
  tests continue to render every SwiftUI route and state.
- The frozen Provider Settings helper no longer ships Python development
  headers, compiler Makefiles, package-manager metadata, or test modules.

### Fixed

- Release metadata tests now isolate their temporary repositories from GitHub
  tag environment variables, allowing immutable-tag builds to run correctly.
- Artifact-audit failures report only safe rule identifiers and validated member
  names while continuing to reject build-machine home paths.

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

- Installation now prefers the standard Finder `/Applications` directory,
  falls back to `~/Applications` when necessary, preserves that location for
  updates, rollback, and uninstall, and reveals the installed app in Finder.
- Codex daily activity now reads local session deltas incrementally, assigns
  events to the local calendar day, and treats cached input as a subset of
  input instead of adding it to the total a second time.
- Codex history no longer remains stuck on a partial OpenUsage daily snapshot
  when the upstream aggregation times out on large session archives.
- Local Codex activity is committed before slower network quota refreshes, so
  one unavailable Provider cannot delay the daily Token ledger.
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
