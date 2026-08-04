# UsageHub Desktop (Electron)

Cross-platform desktop client for UsageHub. It starts the local loopback web
dashboard (`openusage-bar dashboard` on `127.0.0.1:17822`) and shows it in a
native window with a tray icon, so the same application client works on
macOS, Windows, and Linux without depending on a browser tab.

## Run locally

```bash
cd desktop
npm install --save-dev electron
npm start
```

## Configuration

- `USAGEHUB_DASHBOARD_URL`: override the dashboard URL (default
  `http://127.0.0.1:17822`).
- `USAGEHUB_COLLECTOR`: path to the collector executable that supports the
  `dashboard` subcommand (defaults to the installed OpenUsage Bar bundle's
  Provider Settings helper).

## Packaging

Add `electron-builder` (or `electron-forge`) to produce per-platform
installers; the app itself is a thin shell and contains no credentials.
