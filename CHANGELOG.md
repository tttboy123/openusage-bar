# Changelog

This project follows [Semantic Versioning](https://semver.org/).

## Unreleased

## 0.6.0 - 2026-07-30

### Added

- Provider capability evidence now records fact families, source authority,
  account and model scope, and whether validation used a live account, fixture,
  or upstream declaration.
- Moonshot/Kimi official account balances are available as a distinct Balance
  fact and are never presented as subscription capacity.
- A standalone Provider Adapter Kit includes declarative quota, daily Token,
  and daily cost templates plus a reusable conformance command.
- No-telemetry Canary diagnostics include aggregate Balance health and public
  capability evidence without exporting amounts, account references, Provider
  instances, or source identifiers.

### Changed

- Local API v1 has an executable N-1 compatibility policy: additive fields,
  including `balances`, remain optional to older clients.
- Pre-release builds now run the same dependency audit and isolated install,
  upgrade, rollback, and uninstall gates as pull-request CI.
- Updated the pinned GitHub Actions baseline to `actions/checkout@v7.0.1`,
  `actions/setup-python@v7.0.0`, and `actions/upload-artifact@v7.0.1`, with
  every workflow reference still bound to an approved immutable commit.
- Local and GitHub builds now verify every official GitHub Action against a
  committed pin manifest, requiring both an immutable 40-character commit SHA
  and its exact human-readable release tag.

### Fixed

- Codex local Token history no longer double-counts unchanged cumulative
  events, and parser-contract changes trigger one bounded historical backfill.
- MiniMax regional sources, Cursor fallback, Kiro quota, and OpenAI
  Organization usage/cost pagination preserve independent fact health,
  Last-good data, and account scope instead of combining or replacing facts.
- Supported Billing or Cost capabilities must have a matching `api_spend`
  source fact, preventing capability declarations from drifting away from
  runtime evidence.

## 0.4.4 - 2026-07-19

### Changed

- The menu-bar login item and background collector now cross a signed native
  `execve` boundary that rebuilds a minimal non-secret environment before any
  long-lived Swift or Python runtime starts.

### Fixed

- Provider-shaped variables present in the user launchd context are no longer
  inherited by resident OpenUsage Bar processes; global launchd state is never
  modified and credentials continue to be read only from Keychain.
- A day with neither model rows nor explicit coverage remains unavailable in
  the local API and menu bar instead of being rendered as zero Token usage.

## 0.4.3 - 2026-07-19

### Added

- An explicit Token-counting convention shared by Python, SQLite, CLI, Local
  API, and Swift, with rollback-compatible sidecar storage for existing
  schema-v5 ledgers.
- Opt-in diagnostics v2 for bounded daily reconciliation, including source
  totals, component counters, coverage, quality, freshness, and conservative
  duplicate-row evidence.

### Changed

- Usage Details presents Total, Input, Output, Cache Read, Cache Creation, and
  Reasoning independently and explains whether cache is inclusive, disjoint,
  provider-reported, mixed, or unknown.
- Reconciliation exports keep observed subtotals separate from complete totals
  and use per-export account pseudonyms.

### Fixed

- Old app versions can still read and roll back a ledger after the new Token
  convention metadata has been written.
- Provider mutation helpers no longer inherit unrelated parent-process secret
  environment variables.
- Partial or missing coverage is no longer representable as a trustworthy
  complete aggregate total in diagnostics.

## 0.4.2 - 2026-07-18

### Added

- A drag-to-install DMG with a standard Applications shortcut for normal macOS
  installation; unnotarized downloads may require the scoped command below.
- A bilingual guide inside the DMG with the scoped quarantine-removal command
  required when an unnotarized download is reported as damaged by macOS.
- First-launch registration of the menu-bar login item and bundled collector
  through Apple's Service Management framework.

### Changed

- Initial and login launches stay in the menu bar; explicitly reopening the app
  continues to open recovery details when needed.
- Existing script-installed LaunchAgents now suppress false Service Management
  `notFound` packaging alerts during an upgrade.
- The ZIP and transactional scripts remain available for advanced repair,
  rollback, and automation instead of being the primary install path.
- Gatekeeper documentation uses a scoped quarantine-removal command for this
  app and never asks users to disable system protection globally.

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

- OpenUsage daily scans now allow 60 seconds so large local histories are not
  discarded by the previous 30-second process timeout.
- Providers with an official daily usage adapter now select the official result
  first and atomically fall back to OpenUsage only when the official source
  fails. Fallback rows are marked with source `openusage.daily` and quality
  `fallback`; the two sources are never added together.
- Codex daily Token history again uses the shared OpenUsage collector as its
  primary source, while its local rate-limit adapter remains responsible only
  for subscription capacity.
- All app and helper bundles now share release version 0.4.0 and build 4.
- Release metadata is verified against immutable tags, build history, and the
  CHANGELOG; GitHub Actions are pinned to full official commit SHAs.

### Fixed

- Empty or failed OpenUsage scans no longer replace a last-good range with
  covered zero usage. Source health records `empty_result` or the sanitized
  failure while preserving the prior rows and coverage.
- Daily activity details now expose each day's raw source IDs, quality IDs, and
  collection time in the chart tooltip and accessibility summary.
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
