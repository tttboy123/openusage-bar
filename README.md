# OpenUsage Bar

OpenUsage Bar is a local, native macOS usage surface for AI subscriptions,
API Providers, and local coding tools. The menu bar is for quick human
decisions; the Activity app, CLI JSON, and private local API expose the same
canonical ledger for detailed inspection and local schedulers.

> **Pre-release:** version 0.2 supports Apple Silicon Macs running macOS 15 or
> later. Developer ID notarization is not yet available; read the Gatekeeper
> note before installing a downloaded build.

This is an independent repository and release. OpenUsage is an optional,
released CLI data source consumed only through validated JSON; its source code,
Go internals, credentials, and release lifecycle are not embedded here.

## Quick install

Download the macOS arm64 ZIP and matching checksum from GitHub Releases, verify
it, unzip it, and run the bundled installer:

```bash
shasum -a 256 -c OpenUsage-Bar-v0.2.0-macos-arm64.zip.sha256
scripts/install_app.sh
```

For a user-local installation without administrator access:

```bash
OPENUSAGE_INSTALL_DIR="$HOME/Applications" scripts/install_app.sh
```

See [the release quick start](docs/release-quick-start.md) for Gatekeeper,
first-run, update, and uninstall details.

## Installed architecture

- `OpenUsage Bar.app` is the single SwiftUI status host. Its B v3 popover shows
  Today Token, urgency-sorted Capacity, and a small footer. It does not repeat
  risk or live/stale summary cards.
- `OpenUsage Activity.app` is a regular SwiftUI app with Overview, Activity,
  Capacity, API Spend, Local Tools, Providers and Accounts, and Data Health.
  Daily totals, stacked model trends, and the square-cell annual heatmap read
  the canonical SQLite ledger without credentials.
- `OpenUsage Provider Settings.app` is a regular settings window. The same
  signed Python runtime also provides strictly allowlisted headless collector
  commands; headless mode never creates an AppKit status item.
- `com.lune.openusagebar` runs only the native status host with `--background`.
- `com.lune.openusagebar.collector` runs only the headless collector and its
  read-only Unix-socket API. There is no TCP listener by default.

Provider credentials remain in macOS Keychain. The ledger, JSON/JSONL, API,
UI, and logs contain canonical display facts rather than keys, cookies,
authorization headers, prompts, responses, or direct account identity.

## Build from source

The canonical scripts use the checked-in source, a reproducible local virtual
environment, Swift Package Manager, Xcode command-line tools, and native macOS utilities.
They do not download an installer or third-party UI package.

```bash
scripts/bootstrap.sh
scripts/build_app.sh
scripts/install_app.sh
```

The build runs the Python and Swift tests, rejects a stale generated Provider
catalog, enforces at least 80 percent line coverage for every explicitly listed
Phase 5 Python module and for Swift product sources,
scans catalog and bundle metadata for credential material, builds both Swift
products in Release with warnings as errors, creates `dist/OpenUsage Bar.app`,
signs nested helpers before the main bundle, and verifies the result. The installer makes a
timestamped complete backup under ignored `deployed-backups/`, copies through
an `.app.new-<pid>` path, replaces on the same filesystem, installs the two
LaunchAgents, and verifies the private API. A failed install automatically
restores the prior app and agents; backups are retained.

## Open the interfaces

- Click the OpenUsage Bar menu item for Today Token and Capacity.
- Choose **Open Usage Details** for the regular Activity window.
- Choose **Settings** to add, edit, hide, or restore Providers.
- Opening `/Applications/OpenUsage Bar.app` from Finder or Spotlight while the
  background host is running opens Data Health as a recovery route.

Hidden Providers disappear from the status and Activity presentation without
deleting credentials or ledger history.

## Local data and API

- Ledger: `~/.local/state/openusage-bar/activity.sqlite3`
- Unix socket: `~/.local/state/openusage-bar/openusage.sock`
- Provider config: `~/.config/openusage-bar/providers.json`
- Provider visibility: `~/.config/openusage-bar/visibility.json`
- Logs: `~/Library/Logs/OpenUsageBar.*.log`
- LaunchAgents: `~/Library/LaunchAgents/com.lune.openusagebar*.plist`

The socket directory is mode `0700` and the socket is `0600`. Supported
read-only resources include:

```text
GET /v1/health
GET /v1/schema
GET /v1/summary
GET /v1/capabilities
GET /v1/providers
GET /v1/providers?providerIds=codex,minimax-primary
GET /v1/capacity
GET /v1/activity/daily?from=2026-07-01&to=2026-07-14
GET /v1/quotas/history
GET /v1/sources/status
GET /v1/changes?after=0&limit=100
```

In `/v1/capabilities`, `familyId` is the canonical family key. Schema v1 also
retains `providerId` as a deprecated compatibility alias with the same value.

The settings helper also exposes direct, shell-free CLI JSON for local use:

```bash
HELPER="/Applications/OpenUsage Bar.app/Contents/Helpers/OpenUsage Provider Settings.app/Contents/MacOS/OpenUsage Provider Settings"
"$HELPER" status --format json --offline
"$HELPER" providers --format json --offline
"$HELPER" usage --from 2026-07-01 --to 2026-07-14 --format jsonl --offline
"$HELPER" doctor --format json --offline
```

Use `--offline` for low-latency scheduler reads. An explicit `--fresh` request
and the menu-bar Refresh action share one 90-second interactive attempt limit.
The limit is not a promise that every configured source will finish; when it is
reached, OpenUsage Bar keeps serving the last-good ledger and reports the
refresh as unavailable instead of replacing facts with zero.

`openusage-bar` is the logical CLI program name shown in help and API
documentation; this build deliberately does not install a global executable.
Use the signed helper path above in scripts and scheduler jobs. From a source
checkout, the equivalent development invocation is
`.build-venv/bin/python openusage_settings.py providers --format json --offline`.

The API contract is documented in [docs/api/local-api-v1.md](docs/api/local-api-v1.md).

## Provider notes

- The version-one catalog contains exactly the 35 families registered by
  OpenUsage 0.23.0 plus the built-in MiniMax and Step Plan families. Python
  reads the checked-in JSON manifest; Swift reads a deterministic generated
  file that the build verifies against that manifest.
- A `providerId` identifies one configured or detected instance. Its exact
  `familyId` selects catalog capabilities. Unknown future family IDs remain
  readable through the generic API path and never inherit a built-in family by
  prefix or substring matching.
- `/v1/capabilities` enumerates static families. `/v1/providers` and the
  `providers --format json --offline` command enumerate only sanitized dynamic
  instances; neither surface contains raw OpenUsage attributes, account names,
  paths, emails, keys, cookies, or session values.
- OpenUsage-native cards and daily history use bounded, shell-free `openusage`
  exports with a minimal allowlisted child environment.
- MiniMax, Codex, Cursor, Kiro, and StepFun expose subscription or quota facts
  when their local or official source supports them. Unknown quota is never
  fabricated as zero.
- MiniMax daily model activity reuses the delayed platform billing feed under
  the distinct `minimax.billing` source; current-day absence is not presented
  as a live zero.
- StepFun supports China and International sites as separate accounts and
  retains only the required session values. See
  [docs/stepfun-quick-start.md](docs/stepfun-quick-start.md).
- Generic HTTPS Providers validate endpoints, redirects, response size, and
  configured JSON paths before showing remaining capacity.
- Custom Daily Token Feed supports range-aware HTTPS JSON, fixed field mapping,
  bounded pagination, and Keychain authentication when neither OpenUsage nor an
  official adapter can provide history. See
  [docs/research/2026-07-17-custom-daily-feed-boundary.md](docs/research/2026-07-17-custom-daily-feed-boundary.md).

The complete support model—including all catalog families, built-in adapters,
custom integrations, and the distinction between detection, Token history,
billing, and subscription quota—is documented in
[Provider support](docs/provider-support.md).

## Stop or roll back

To stop the two installed jobs temporarily:

```bash
launchctl bootout gui/$(id -u)/com.lune.openusagebar
launchctl bootout gui/$(id -u)/com.lune.openusagebar.collector
```

For a manual rollback, boot out both jobs, restore the timestamped app and
plist copies from `deployed-backups/<timestamp>/`, then bootstrap the restored
plist files. A same-filesystem previous stage exists only while a transaction
can still roll back and is cleaned after all install health gates pass. Do not
delete the durable timestamped backup until the installed version has been
used successfully.

## Contributing and security

Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing an adapter or exported
fact. Report credential, Keychain, provider endpoint, and local API issues using
the private process in [SECURITY.md](SECURITY.md), never a public issue.

## License

OpenUsage Bar is released under the [Apache License 2.0](LICENSE). Bundled
runtime dependencies and interoperability boundaries are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
