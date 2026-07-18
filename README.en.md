<div align="center">

# OpenUsage Bar

**See AI subscription capacity, token activity, and API spend at a glance.**

A native macOS menu-bar utility. Data stays local; people read the UI and schedulers read JSON.

[![Release](https://img.shields.io/github/v/release/tttboy123/openusage-bar?include_prereleases&style=flat-square&color=0A84FF)](https://github.com/tttboy123/openusage-bar/releases)
![macOS](https://img.shields.io/badge/macOS-15%2B-111111?style=flat-square&logo=apple&logoColor=white)
![Apple Silicon](https://img.shields.io/badge/Apple%20Silicon-arm64-111111?style=flat-square)
![SwiftUI](https://img.shields.io/badge/SwiftUI-native-111111?style=flat-square&logo=swift&logoColor=white)
![Local First](https://img.shields.io/badge/Local--First-Keychain%20%2B%20SQLite-111111?style=flat-square)

[中文](README.md) | [Local API](docs/api/local-api-v1.md) | [Provider support](docs/provider-support.md) | [Install](docs/release-quick-start.md)

</div>

OpenUsage Bar is a local-first native macOS dashboard for AI subscriptions, API providers, local coding tools, and daily token activity.

<p align="center">
  <img src="docs/assets/openusage-bar-activity-demo-zh.png" width="1160" alt="OpenUsage Bar Activity view showing the yearly token heatmap and daily model trend">
</p>

<p align="center"><sub>Real SwiftUI interface rendered from an isolated synthetic ledger. No user ledger, Keychain data, or real quota was read.</sub></p>

> Current version: **0.4.2 pre-release**. Apple Silicon and macOS 15 or later are required. Developer ID notarization is not available yet; if macOS reports the app as damaged, remove the download quarantine from this app only as described below.

## What it does

- Menu bar: today token total, urgent capacity, refresh state, and details entry.
- Activity app: overview, token activity, capacity, API spend, local tools, providers, accounts, and data health.
- Provider center: add, edit, hide, restore, and manage multiple accounts without echoing credentials.
- Local automation surface: stable CLI JSON/JSONL and a private read-only Unix-socket API.
- Privacy boundary: credentials stay in macOS Keychain; prompts, responses, raw provider payloads, cookies, sessions, and direct account identity are not exported.

```mermaid
flowchart LR
  A[AI Providers and Local Tools] --> B[Bounded Python Collectors]
  K[(macOS Keychain)] --> B
  B --> D[(Local SQLite Ledger)]
  D --> E[Menu Bar Snapshot]
  D --> F[Usage Details]
  D --> G[CLI JSON and Read-only API]
```

## Quick install

[Download OpenUsage Bar v0.4.2 DMG for Apple Silicon](https://github.com/tttboy123/openusage-bar/releases/download/v0.4.2/OpenUsage-Bar-v0.4.2-macos-arm64.dmg)

1. Open the downloaded DMG.
2. Drag **OpenUsage Bar** onto **Applications**.
3. Open it from Finder's Applications folder. The app registers its login item
   and bundled collector automatically; Terminal is not required.

If macOS says **“OpenUsage Bar is damaged”**, verify that the DMG came from this
repository and matches its SHA-256 file, then remove quarantine from this app
only:

```bash
xattr -dr com.apple.quarantine "/Applications/OpenUsage Bar.app"
```

Open it again from Applications. This does not disable Gatekeeper system-wide.
The DMG also includes the same bilingual installation guide. If background
access needs approval, allow OpenUsage Bar in **System Settings > General >
Login Items**.

The ZIP, checksums, transactional installer, rollback, and uninstall scripts
remain available on the release page for advanced repair and automation. See
the [install guide](docs/release-quick-start.md).

## Build from source

```bash
scripts/bootstrap.sh
scripts/build_app.sh
scripts/install_app.sh
```

Package a release artifact:

```bash
scripts/package_release.sh
```

## Local data and API

- Ledger: `~/.local/state/openusage-bar/activity.sqlite3`
- Unix socket: `~/.local/state/openusage-bar/openusage.sock`
- Provider config: `~/.config/openusage-bar/providers.json`
- Provider visibility: `~/.config/openusage-bar/visibility.json`
- Logs: `~/Library/Logs/OpenUsageBar.*.log`

Supported read-only resources include:

```text
GET /v1/health
GET /v1/schema
GET /v1/summary
GET /v1/capabilities
GET /v1/providers
GET /v1/capacity
GET /v1/activity/daily?from=2026-07-01&to=2026-07-14
GET /v1/costs/daily?from=2026-07-01&to=2026-07-14
GET /v1/quotas/history
GET /v1/sources/status
GET /v1/changes?after=0&limit=100
```

Signed helper JSON:

```bash
APP="/Applications/OpenUsage Bar.app"
[[ -d "$APP" ]] || APP="$HOME/Applications/OpenUsage Bar.app"
HELPER="$APP/Contents/Helpers/OpenUsage Provider Settings.app/Contents/MacOS/OpenUsage Provider Settings"
"$HELPER" status --format json --offline
"$HELPER" providers --format json --offline
"$HELPER" usage --from 2026-07-01 --to 2026-07-14 --format jsonl --offline
"$HELPER" doctor --format json --offline
```

## Provider support

OpenUsage Bar is an independent repository and release. OpenUsage.sh is an optional CLI data source consumed through validated JSON only; its Go internals, credentials, and release lifecycle are not embedded here.

Version 0.4.2 includes the OpenUsage 0.23.0 provider catalog plus built-in enhancements for MiniMax, StepFun, Codex, Cursor, Kiro, OpenAI Organization, Generic HTTPS Provider, and Custom Daily Token Feed. See [Provider support](docs/provider-support.md).

## License

Apache-2.0. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
