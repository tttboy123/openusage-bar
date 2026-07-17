# OpenUsage Bar release quick start

OpenUsage Bar 0.2 supports Apple Silicon Macs running macOS 15 or later.

## Install

1. Download the macOS arm64 ZIP and matching `.sha256` file from Releases.
2. Verify the download:

   ```bash
   shasum -a 256 -c OpenUsage-Bar-v0.2.0-macos-arm64.zip.sha256
   ```

3. Unzip it, open Terminal in the extracted directory, and run:

   ```bash
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

The initial GitHub pre-release may be ad-hoc signed. Until a notarized Developer
ID build is attached, build from source or explicitly allow the downloaded app
in **System Settings > Privacy & Security**. Never run a command that disables
Gatekeeper globally.

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
