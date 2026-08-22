# UsageHub 安装指南 / Install guide

UsageHub 0.8.7 RC 候选版支持 Apple Silicon Mac 和 macOS 15 或更高版本。当前
发布形态为桌面客户端（Electron 封装本地 Web 仪表盘），原生 SwiftUI 菜单栏
版本同步维护。

## 图形化安装（推荐，桌面客户端）

1. 从 [v0.8.7 发布页](https://github.com/tttboy123/usagehub/releases/tag/v0.8.7)
   下载 `UsageHub-0.8.7-mac-arm64.dmg`。
2. 双击 DMG，将 **UsageHub** 拖入 **Applications**。
3. 在访达“应用程序”中打开。App 会自动注册登录项和内置采集器，菜单栏图标
   随即显示今日用量简况。
4. 若 macOS 显示“UsageHub 已损坏”，确认下载来源和 SHA-256 后执行
   `xattr -dr com.apple.quarantine "/Applications/UsageHub.app"`，再从
   “应用程序”打开。不要全局关闭 Gatekeeper。
5. 若出现后台访问提示，在 **系统设置 > 通用 > 登录项**允许 UsageHub。

单击菜单栏图标查看今日简况（今日 Token、实测余额、额度），双击打开仪表盘。

## Install (English)

After candidate publication, download the v0.8.7 DMG, open it, drag
**UsageHub** to **Applications**, then open it from Finder. The app registers
its login item and bundled collector on first launch and shows today's usage
summary in the menu bar. If macOS says the app is damaged, verify the download
and run `xattr -dr com.apple.quarantine "/Applications/UsageHub.app"` for this
app only. Do not disable Gatekeeper system-wide. Allow it under **General >
Login Items** if macOS requests background approval.

## 可选完整性校验 / Optional checksum

将 DMG 和 `.dmg.sha256` 放在同一目录后执行：

```bash
shasum -a 256 -c UsageHub-0.8.7-mac-arm64.dmg.sha256
```

## 原生 SwiftUI 版本（可选）

原生菜单栏版本仍以 `OpenUsage Bar.app` 分发，产物名为
`OpenUsage-Bar-v0.8.7-macos-arm64.dmg`（候选发布后可用）。其 ZIP 中附带
事务式安装、回滚和卸载工具：

```bash
shasum -a 256 -c OpenUsage-Bar-v0.8.7-macos-arm64.zip.sha256
unzip OpenUsage-Bar-v0.8.7-macos-arm64.zip
cd OpenUsage-Bar-v0.8.7-macos-arm64
scripts/install_app.sh
```

安装器优先使用 `/Applications`，不可写时降级到 `~/Applications`。自定义目录：

```bash
OPENUSAGE_INSTALL_DIR="$HOME/My Apps" scripts/install_app.sh
```

GitHub 构建使用 ad-hoc 签名，未进行 Developer ID 公证。不要运行全局关闭
Gatekeeper 的命令。

Every release must use an immutable `vX.Y.Z` tag whose version and build agree
with all app bundles, the Python helper, and the matching CHANGELOG entry.
CI pins third-party Actions to verified full commit SHAs. Developer ID signing
and notarization are optional distribution conveniences, not source-release
requirements.

## Maintainer release readiness preflight

Before discussing a candidate as releasable, generate the machine-readable
readiness inventory:

```bash
python scripts/release_readiness.py --output /tmp/openusage-release-readiness.json
```

The current `0.8.7` candidate intentionally reports
`release_readiness_blocked`: local product/version and build-identity checks
pass, but `releaseEligible=false` and the required external evidence is still
missing. The report is path-free and fail-closed, but it is not a release
authorization mechanism. It always declares `decisionAuthority=inventory_only`
until the external gates are bound to real verifiers.

The public CLI does not accept self-reported external evidence. Hosted
macOS/Windows/Linux native runner evidence, protected release environment
verification, signing and notarization proof, UI parity, clean reference
performance, and separated observation/Gateway canary evidence must be checked
by their own real verifiers or protected workflow steps. A future bounded
evidence format may summarize those verifier outputs, but hand-written JSON
must never make this script return authoritative ready. Final release approval
remains owned by the protected publish workflow and the real native, signing,
performance, accessibility, and canary verifiers.

## Build from source

Install Xcode and Python 3.11 or later, then run:

```bash
scripts/bootstrap.sh
scripts/build_app.sh
scripts/install_app.sh
```

Build and package the desktop client (current release form):

```bash
cd desktop && npm run dist:mac
```

## Roll back

Before every upgrade, the installer writes a complete signed-app backup under
`~/.local/state/openusage-bar/backups/app`. Only the two newest complete,
hash-verified backups are retained. To restore the newest one:

```bash
scripts/rollback_app.sh
```

Rollback verifies the backup's bundle identity, version, signature, and full
content hash before the atomic swap. It preserves the ledger, provider
configuration, and Keychain entries. If the three Local API v1 contract routes
do not recover within 20 seconds, the rollback itself is reversed.

## Uninstall

```bash
scripts/uninstall_app.sh
```

This preserves the local ledger, configuration, and Keychain items. To remove
the local ledger and configuration as well:

```bash
scripts/uninstall_app.sh --purge-data
```

Keychain entries are deliberately not deleted automatically. Remove them in
Keychain Access only after confirming that no other local installation uses
the same service entries.

## Opt-in canary diagnostics

UsageHub sends no telemetry. A canary tester may explicitly create a redacted
aggregate for a GitHub canary report:

Before running any extracted script, verify the downloaded ZIP directly with
GitHub CLI. Then the packaged candidate verifier checks the manifest, SBOM,
checksums, every release asset and all attestations:

```bash
gh attestation verify OpenUsage-Bar-v0.8.7-macos-arm64.zip \
  --repo tttboy123/openusage-bar \
  --signer-workflow tttboy123/openusage-bar/.github/workflows/release.yml \
  --source-ref refs/tags/v0.8.7 \
  --deny-self-hosted-runners
shasum -a 256 -c OpenUsage-Bar-v0.8.7-macos-arm64.zip.sha256
unzip OpenUsage-Bar-v0.8.7-macos-arm64.zip
cd OpenUsage-Bar-v0.8.7-macos-arm64
scripts/verify_canary_candidate.py --assets-dir .. --version 0.8.7
```

After installing the verified candidate, a tester may explicitly create a
redacted aggregate:

```bash
scripts/export_diagnostics.py --output /tmp/openusage-diagnostics.json
scripts/privacy_scan.py /tmp/openusage-diagnostics.json
```

This keeps the byte-compatible aggregate diagnostics v1 as the default. For a
bounded, source-aware daily reconciliation, explicitly request v2:

```bash
scripts/export_diagnostics.py \
  --schema-version 2 \
  --from 2026-07-17 \
  --to 2026-07-18 \
  --timezone Asia/Singapore \
  --output /tmp/openusage-diagnostics-v2.json
scripts/privacy_scan.py /tmp/openusage-diagnostics-v2.json
```

Review either file before attaching it. V2 preserves source totals, marks
non-comparable or incomplete rows, separates complete `tokenTotals` from
partial `observedTokenTotals`, replaces account references with per-export
pseudonyms, and only reports duplicate candidates backed by duplicate effective
rows. `accountTotalComparison` also makes local Codex session and OpenUsage
collector coverage explicitly non-comparable with an account-wide dashboard;
undeclared source scope stays `unknown`. It cannot observe source-selection
history. The full 30-day process and the 1.0 release gate are documented in
[canary.md](canary.md).
