# OpenUsage Bar 安装指南 / Install guide

OpenUsage Bar 0.4.2 支持 Apple Silicon Mac 和 macOS 15 或更高版本。

## 图形化安装（推荐）

1. 从 [v0.4.2 发布页](https://github.com/tttboy123/openusage-bar/releases/tag/v0.4.2)
   下载 `OpenUsage-Bar-v0.4.2-macos-arm64.dmg`。
2. 双击 DMG，将 **OpenUsage Bar** 拖入 **Applications**。
3. 在访达“应用程序”中打开。App 会自动注册登录项和内置采集器。
4. 若 macOS 拦截，进入 **系统设置 > 隐私与安全性**，仅对 OpenUsage Bar
   选择 **仍要打开**。不要全局关闭 Gatekeeper。
5. 若出现后台访问提示，在 **系统设置 > 通用 > 登录项**允许 OpenUsage Bar。

OpenUsage Bar 是菜单栏工具，不会出现在 Dock 或 Command-Tab。采集器每五分钟刷新。

## Install (English)

Download the v0.4.2 DMG, open it, drag **OpenUsage Bar** to **Applications**,
then open it from Finder. The app registers its login item and bundled collector
on first launch. If Gatekeeper blocks it, use **System Settings > Privacy &
Security > Open Anyway** for this app only. Allow it under **General > Login
Items** if macOS requests background approval.

## 可选完整性校验 / Optional checksum

将 DMG 和 `.dmg.sha256` 放在同一目录后执行：

```bash
shasum -a 256 -c OpenUsage-Bar-v0.4.2-macos-arm64.dmg.sha256
```

## 高级修复与自动化 / Advanced repair

普通用户不需要执行脚本。ZIP 中仍附带事务式安装、回滚和卸载工具：

```bash
shasum -a 256 -c OpenUsage-Bar-v0.4.2-macos-arm64.zip.sha256
unzip OpenUsage-Bar-v0.4.2-macos-arm64.zip
cd OpenUsage-Bar-v0.4.2-macos-arm64
scripts/install_app.sh
```

安装器优先使用 `/Applications`，不可写时降级到 `~/Applications`。自定义目录：

```bash
OPENUSAGE_INSTALL_DIR="$HOME/My Apps" scripts/install_app.sh
```

GitHub 构建使用 ad-hoc 签名，未进行 Developer ID 公证。不要运行全局关闭
Gatekeeper 的命令。

Every release must use an immutable `vX.Y.Z` tag whose version and build agree
with all three app bundles, the Python helper, and the matching CHANGELOG entry.
CI pins third-party Actions to verified full commit SHAs. Developer ID signing
and notarization are optional distribution conveniences, not source-release
requirements.

## Build from source

Install Xcode and Python 3.11 or later, then run:

```bash
scripts/bootstrap.sh
scripts/build_app.sh
scripts/install_app.sh
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

OpenUsage Bar sends no telemetry. A canary tester may explicitly create a
redacted aggregate for a GitHub canary report:

```bash
scripts/export_diagnostics.py --output /tmp/openusage-diagnostics.json
scripts/privacy_scan.py /tmp/openusage-diagnostics.json
```

Review the file before attaching it. The full 30-day process and the 1.0
release gate are documented in [canary.md](canary.md).
