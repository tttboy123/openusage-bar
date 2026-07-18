# OpenUsage Bar release quick start

OpenUsage Bar 0.4 supports Apple Silicon Macs running macOS 15 or later.

## Install

1. Download the macOS arm64 ZIP and matching `.sha256` file from Releases into
   the same directory.
2. Verify the download:

   ```bash
   shasum -a 256 -c OpenUsage-Bar-v0.4.0-macos-arm64.zip.sha256
   ```

3. Unzip it, enter the extracted directory, and run the bundled installer:

   ```bash
   unzip OpenUsage-Bar-v0.4.0-macos-arm64.zip
   cd OpenUsage-Bar-v0.4.0-macos-arm64
   scripts/install_app.sh
   ```

   To install without administrator access:

   ```bash
   OPENUSAGE_INSTALL_DIR="$HOME/Applications" scripts/install_app.sh
   ```

4. Open **OpenUsage Bar** from Applications once. The menu-bar item starts at
   login and the collector refreshes every five minutes.
5. Choose **Settings** to add provider credentials. Credentials are written to
   macOS Keychain; provider configuration stores only non-secret metadata.

The GitHub convenience build is ad-hoc signed. Build from source or explicitly
allow the downloaded app
in **System Settings > Privacy & Security**. Never run a command that disables
Gatekeeper globally.

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
