# Security Policy

## Supported versions

Security fixes are provided for the latest published minor release. Pre-release
builds are supported only until a newer pre-release is available.

## Reporting a vulnerability

Do not open a public issue for suspected credential disclosure, Keychain access,
local API authorization, unsafe provider endpoints, or private usage data.
Use the repository's **Security** tab to submit a private vulnerability report.

Include the affected version, macOS version, reproduction steps, expected
impact, and whether any credential may have been exposed. Do not include a real
API key, cookie, session token, prompt, response, or account identity. Use a
clearly synthetic value and redact logs before attaching them.

The project will acknowledge a complete report within seven days. There is no
bug bounty program.

## Security model

- Provider credentials stay in macOS Keychain.
- The read API binds to a user-private Unix socket, not TCP.
- Provider subprocesses are shell-free, bounded, and receive an allowlisted
  environment.
- The resident menu-bar host and collector cross a signed native `execve`
  boundary that rebuilds a minimal environment; the installer never mutates
  the user's global launchd environment.
- The SQLite ledger and exported JSON exclude credentials, prompts, responses,
  and direct account identity.
- The optional execution proxy is disabled by default, binds only to IPv4
  loopback, requires a one-time high-entropy Bearer capability, and persists
  only its SHA-256 verifier. Request and response content is forwarded in
  memory and is excluded from routing evidence, logs, diagnostics and the
  usage ledger.
- Execution credentials are isolated per routing connection in Keychain.
  Reusing a Provider Center credential requires an explicit foreground action;
  the fixed Provider endpoint and secret never cross into SwiftUI or JSON.
- Releases must pass the repository and Git-history secret scanner.

Pre-release security reviews are maintained outside the public repository.
Passing repository checks is not an independent security approval and does not
make a local candidate a public release.

## Canary diagnostics

Canary participation is manual and opt-in; the application sends no telemetry.
`scripts/export_diagnostics.py` reads only the read-only Local API snapshot and
public capability catalog, then writes an aggregate mode-`0600` JSON file. It
does not read Provider configuration or Keychain and does not export Provider
instance names, account references, source IDs, quota values, raw change
payloads, prompts, or responses.

Run `scripts/privacy_scan.py` on the file and inspect it before attaching it to
a public canary report. If diagnostics appear to contain private material, do
not attach them: delete the local export and use the repository Security tab.
