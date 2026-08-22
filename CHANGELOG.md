# Changelog

This project follows [Semantic Versioning](https://semver.org/).

## 0.8.7 - 2026-08-22

### Changed

- Menu-bar data refreshes automatically every 30 minutes, while manual refresh and opening the
  dashboard trigger a fresh bounded query instead of only repainting cached values.
- Codex quota collection now follows the official app-server rate-limit query path, with bounded
  fallback and explicit stale-data handling when the upstream service cannot be reached.
- GPT, Codex, and Cursor capacity displays now preserve live changes and avoid duplicate balance
  rows or misleading low estimates caused by mixed-age snapshots.

### Security

- Dependency checks now pin the build environment to `pip 26.2` and retain the existing release
  artifact audit boundary.

### Fixed

- Cross-platform tests no longer invoke POSIX-only process-group behavior or hard-code `/bin/sh`
  on Windows.
- Long Provider refreshes no longer cross the old three-minute desktop deadline and get reported
  as failed shortly before the background refresh succeeds.
- The 30-minute menu-bar schedule now waits one interval before its first background refresh and
  defers provider credential initialization until refresh is due, so app launch does not compete
  with an immediate user or Capacity-page refresh.
- Refresh failures keep the last known-good quota visible with a clear freshness state instead of
  silently presenting it as current data.

## 0.8.6 - 2026-08-16

### Added

- 桌面客户端（Electron）菜单栏简况：品牌图标旁实时显示今日 Token（🟢/🟡/🔴 状态点，60 秒自动刷新）；单击托盘图标弹出简况菜单（今日 Token、实测余额、额度），双击打开仪表盘；启用品牌托盘模板图标与 App/DMG 图标。
- Provider 配置预设已与各 Provider 官网逐一核对并对齐（DeepSeek V4、Kimi K3、MiniMax M3、SiliconFlow、OpenCode Zen、OpenRouter 等），并补充真实品牌图标；`codex` 显示为 **ChatGPT**。
- The loopback dashboard can run one bounded user-triggered refresh
  (`POST /v1/refresh` plus a `GET /v1/refresh/status` probe) through the same
  headless refresher the daemon uses; when no refresher is available the route
  fails closed with `unavailable`.
- `ActivityCollector.refresh(history_days=...)` (and `LedgerRefresher`) can
  force the full 364-day history window for usage and cost sources even when
  incremental history already exists, so opening UsageHub can backfill the
  complete ledger instead of only the trailing window.
- A machine-readable release state now keeps the public version, build, Local
  API version, release channel, and external Canary clock under one strict
  validation boundary.
- ADR 0001 freezes ownership of durable resource facts, bounded request
  telemetry, scheduler reservations, and policy decisions.
- Cross-platform Observer packaging foundations for macOS, Windows, and Linux,
  including a self-contained native Collector and renderer-isolated Desktop
  proxy.
- An optional, disabled-by-default Gateway Core with Should-Send, five Provider
  adapters, PII-safe caching, bounded fallback, and pull-driven Gateway-native
  streaming.
- Observer-first Automation and Data Health capability surfaces with explicit
  unknown, disabled, partial, degraded, and last-good states.

### Changed

- Opening UsageHub asks the local host for one bounded refresh with a full
  history backfill and refetches every page once it completes; the manual
  Refresh button triggers the same host refresh instead of only remounting
  the active page. The desktop renderer boundary may reject the refresh hint,
  in which case pages still refetch read-only data.
- Providers now offers a grid/list density toggle (persisted locally) so the
  list view matches CC Switch's compact provider rows: small avatar, name,
  console URL, right-aligned status, and in-flow hover actions.
- Provider brand avatars pick the higher-contrast foreground (dark text on
  light brand colors such as MiniMax/Hermes) instead of always white.
- Readability floor: the smallest sidebar/status/meta text is raised toward
  12 px, the English relative-time copy pluralizes ("day(s) ago"), and
  page-level provider refresh no longer marks every card as syncing.
- Cross-page readability pass: KPI labels, table headers, status pills, quota
  and local-tool labels move from 11.2 px to 11.5 px, secondary meta to
  12 px, and metric badges from 10.9 px to 11.5 px; chart axis ticks keep
  their recharts default. Mobile (<640 px) segmented controls grow to 40 px
  touch targets and full-width.
- Provider source contracts are OS-neutral; the OpenUsage Bar distribution
  separately verifies that every source registered in its shipped catalog
  supports macOS.
- The public roadmap keeps the published v0.7.1 baseline, the v0.8.6 RC
  candidate, and the 0 / 5 external Canary state distinct from repository test
  results.
- Product branding: user-facing docs now use the **UsageHub** name (formerly
  OpenUsage Bar). The `openusage` technical namespace (config paths, sockets,
  `openusage-bar` CLI, Local API v1 contract) remains compatible; app-bundle
  renaming is scheduled with the cross-platform release.

### Security

- Gateway JSON and SSE are rebuilt from closed versioned contracts; live source
  events are validated lazily before wire delivery and rejected material is
  never reflected.
- Windows Local API and Desktop token reads require verified owner/System ACLs;
  packaged artifacts reject credentials, token files, private databases,
  prompts, responses, and private home paths.

### Fixed

- Desktop packaging: PyInstaller helper bundles now include all 12 resource
  files (the spec previously dropped them, crashing the bundled Collector on
  startup).
- Menu-bar tray icon: `trayIcon()` hardcoded the legacy native app path, so the
  tray fell back to a 1×1 transparent pixel and was invisible; it now loads the
  bundled brand template image, and the app ships a proper brand app/DMG icon.
- Open-source audit: sanitized author-private absolute paths in docs and test
  fixtures, and added `CODE_OF_CONDUCT.md`.

### Release status

- `0.8.6` RC pre-release published on GitHub (unsigned, ad-hoc signed). The
  external Canary clock remains **not started (0 / 5)**; this heading does not
  assert a stable publication or an activated Canary.

## 0.7.1 - 2026-08-04

- Native UI: render model trend points on partial days (chart was empty)
- Native UI: keep Provider configuration stable when switching Spaces or apps
- Native UI: add per-Provider Token Usage to API Spend, Get API Key links,
  Data Health summary, and clearer Local Tools empty state

## 0.7.0 - 2026-08-04

- Settings helper GUI: fix PyObjC selector crash and launcher naming
- Release audit: verify CFBundleExecutable matches the on-disk launcher
- Version bump across bundles, release state, and canary docs

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
- A privacy-safe Canary surface verifier proves that the installed CLI and
  Local API expose the same revision and source-health facts while leaving
  menu-bar visibility as an explicit manual check.

### Changed

- Local API v1 has an executable N-1 compatibility policy: additive fields,
  including `balances`, remain optional to older clients.
- Kiro's OpenUsage-backed local Token activity is now marked as validated with
  a live account after a bounded privacy-safe acceptance run. Its official AWS
  subscription quota remains a separate fact and source.
- Pre-release builds now run the same dependency audit and isolated install,
  upgrade, rollback, and uninstall gates as pull-request CI.
- Updated the pinned GitHub Actions baseline to `actions/checkout@v7.0.1`,
  `actions/setup-python@v7.0.0`, and `actions/upload-artifact@v7.0.1`, with
  every workflow reference still bound to an approved immutable commit.
- Local and GitHub builds now verify every official GitHub Action against a
  committed pin manifest, requiring both an immutable 40-character commit SHA
  and its exact human-readable release tag.

### Fixed

- The packaged collector now accepts the documented read-only `snapshot`
  command, so CLI and Local API consumers can retrieve the same revisioned
  resource snapshot from an installed app.
- Headless Step Plan refreshes now use a bounded, killable read-only Keychain
  boundary instead of waiting indefinitely for an interactive Security prompt.
  The private write helper accepts only Step Plan session updates over stdin
  and cannot read or return credentials.
- Advanced and Repair now offers an explicit foreground Keychain authorization
  flow for ad-hoc signed upgrades. It checks only fixed application-owned
  accounts, discards credential output, reports counts instead of values, and
  leaves the five-second headless fail-closed boundary unchanged.
- Transactional upgrades now stop and reopen both visible helpers, so an
  already-open Provider Settings window cannot keep executing the previous app
  image after the bundle has been replaced. Collector daemon arguments remain
  outside the exact process match.
- Cursor enrichment detects OpenUsage's optional exact-provider export and
  polls only Cursor when available; the local integration measured 3.60
  seconds instead of timing out during an all-provider scan. Older OpenUsage
  builds retain the bounded 75-second direct fallback, and the complete
  interactive refresh envelope remains 160 seconds.
- OpenUsage compatibility health now recognizes the independently reviewed
  `c63a47c` provider-filter development build after also verifying the pinned
  base version and exact 35-provider catalog. Unknown development revisions
  remain unsupported and fail closed.
- Step Plan source health now distinguishes Keychain access and network
  failures from invalid upstream responses. Provider Center treats Keychain
  failures as connection actions and keeps the last-good quota visible.
- Provider Center now assigns source health to an explicitly configured
  connection before a colliding OpenUsage-discovered Provider identity, so
  repair actions remain attached to the editable account.
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
