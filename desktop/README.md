# UsageHub Desktop (Electron)

Cross-platform desktop client for UsageHub. The packaged app starts or probes
one private Observer daemon, serves the bundled `web/dist` assets from an
ephemeral loopback static server, and proxies fixed Local API query routes plus
one bounded credential-free refresh command through the main process. When the
optional Gateway is explicitly running in
Advise or Gateway mode, the same main-process boundary also exposes one narrow
read-only advice operation: `POST /gateway/v1/should-send`. It never forwards a
Provider request. The main window and tray therefore share the same
owner-private Unix socket or Windows token/ACL boundary on macOS, Windows, and
Linux. Provider credentials and Local/Gateway bearer tokens never enter the
renderer.

The renderer sends a relative, credential-free four-field Should-Send request.
The main process bounds and validates the body, reconstructs canonical JSON,
injects the private Gateway token, accepts only a valid upstream `200`, and
reconstructs a closed decision response. Unknown headers/fields, raw errors,
private paths, cookies, and credentials are not forwarded in either direction.
Rejected or partial writes receive a stable problem and a bounded connection
close.

## Run locally

```bash
cd desktop
npm install --save-dev electron
npm start
```

## Development configuration

- `USAGEHUB_COLLECTOR`: optional development-only path to a Collector that
  supports the private `daemon` Local API. Packaged builds resolve only the
  bundled native Collector resource.

The standalone `openusage-bar dashboard` command on port 17822 remains a CLI
and demo compatibility surface. It is not started or read by the packaged
Electron lifecycle.

## Packaging

`electron-builder` produces the platform installers. Each package includes one
native Collector at the fixed resource path and is audited to reject tokens,
credentials, telemetry/cache databases, private paths, prompts, and responses.

On packaged Windows and Linux launches, UsageHub registers the Collector as the
current user's background service before opening a window. Windows Task
Scheduler uses the absolute Collector under the installed app resources. A
Linux AppImage atomically installs the same audited bytes at
`${XDG_DATA_HOME:-$HOME/.local/share}/usagehub/runtime/openusage-collector`
because the AppImage mount path is temporary. These services run only the
Observer daemon; Gateway remains in `observe` and is not started by desktop
lifecycle management.

The Windows uninstaller removes the Observer service before application files.
It always preserves local usage state, including default and silent uninstall
and updates. Safe explicit deletion awaits a future native helper; current
Windows packages do not advertise a local-state deletion option. Credential-
manager items are also preserved.

Linux AppImage users can remove the managed service and stable Collector copy
without opening the renderer:

```bash
./UsageHub.AppImage --usagehub-uninstall
```

Add `--delete-data` only to also invoke the confirmed local-state deletion
command. The AppImage file itself can then be deleted normally. Neither command
prints private runtime paths, credentials, or Collector output.
