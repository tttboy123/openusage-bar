# UsageHub Desktop (Electron)

Cross-platform desktop client for UsageHub. The packaged app starts or probes
one private Observer daemon, serves the bundled `web/dist` assets from an
ephemeral loopback static server, and proxies fixed read-only Local API routes
through the main process. When the optional Gateway is explicitly running in
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
