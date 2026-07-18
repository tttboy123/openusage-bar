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
- The SQLite ledger and exported JSON exclude credentials, prompts, responses,
  and direct account identity.
- Releases must pass the repository and Git-history secret scanner.

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
