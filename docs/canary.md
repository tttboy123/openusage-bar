# OpenUsage Bar 1.0 canary protocol

OpenUsage Bar does not collect telemetry. The 1.0 canary is a manual,
opt-in evidence program: a tester runs the checks below and explicitly submits
the GitHub canary form. Credentials, Provider responses, prompts, model
responses, account identity, device serial numbers, and raw logs are never
requested.

## Required cohort

The gate runs for 30 consecutive calendar days after the fifth qualifying
machine joins. It requires at least five external Apple Silicon Macs and five
distinct Provider configuration classes. The cohort must cover:

- macOS 15 and the latest macOS release supported by OpenUsage Bar;
- both `/Applications` and `~/Applications` installation scopes;
- empty setup, local-client-only setup, one quota Provider, a custom daily
  usage/cost feed, and a multi-Provider setup;
- a real N-1 upgrade and one deliberate rollback drill per machine.

A tester-chosen random canary label may correlate reports from one machine.
Do not use a username, email address, hostname, hardware serial number, or
Provider account name as that label.

## Per-machine checks

Record pass/fail and UTC date for each event:

1. Verify the ZIP checksum, manifest, SBOM, and GitHub artifact attestation.
2. Perform a clean install and observe the first trustworthy fact. `Unknown`
   is acceptable when the source explicitly reports why; numeric zero is not a
   substitute for missing data.
3. Refresh, wait through one scheduled refresh interval, and restart both the
   collector and the Mac.
4. Upgrade from the previous published pre-release. Confirm the SQLite
   integrity check, history counts, and change cursor do not decrease.
5. Run `scripts/rollback_app.sh`, confirm Local API v1 recovers, then reinstall
   the candidate.
6. Confirm menu-bar, Usage Details, Provider Center, CLI JSON, and Local API
   describe the same revision and source health.
7. Record every Unknown, stale, authentication, upgrade, rollback, crash,
   credential, or data-integrity incident. Do not wait until day 30 to report a
   security or data-loss issue.

## Optional redacted diagnostics

From the extracted release or a source checkout:

```bash
scripts/export_diagnostics.py --output /tmp/openusage-diagnostics.json
scripts/privacy_scan.py /tmp/openusage-diagnostics.json
```

The exporter reads only `/v1/snapshot` and `/v1/capabilities`. It writes a
mode-`0600` aggregate containing product/build, macOS/architecture, schema and
data revision, aggregate fact/source counts, sanitized error-code counts, and
the public capability catalog. It does not read Provider configuration or
Keychain. Review the JSON yourself before attaching it.

## Incident definitions

The following reset the 30-day zero-incident clock and block 1.0:

- any ledger fact, quota history, configuration, or change-cursor loss;
- any credential, cookie, account identity, prompt, response, raw Provider
  payload, or absolute home path in an artifact or diagnostic;
- any UI/API conversion of unavailable or unknown data into numeric zero;
- an upgrade that cannot complete or automatically restore the previous app;
- a High or Critical known dependency vulnerability without an upstream fix.

Ordinary Provider authentication expiry, a correctly labelled unsupported
capability, or a documented upstream outage does not automatically reset the
clock, but it must remain visible as source health and must recover correctly.

## 1.0 release gate

Release 1.0 only when all conditions hold simultaneously:

- 30 consecutive days with zero blocking incidents;
- five external Apple Silicon Macs and five distinct configuration classes
  complete install, refresh, restart, upgrade, and rollback;
- every candidate has an immutable tag, checksum, manifest, SPDX SBOM, and
  GitHub artifact attestation;
- dependency audit reports zero known High/Critical vulnerabilities;
- Python product modules and deterministic Swift product logic remain at least
  80% line coverage; declarative SwiftUI composition is verified by native
  hosting tests across every route and state instead of compiler-dependent
  generated line counters;
- N-1 upgrade, automatic rollback, and Local API v1 compatibility pass in CI;
- documentation still accurately describes the source-first, ad-hoc signed
  distribution and optional Developer ID path.

Until the timed cohort completes, the project remains a pre-release even when
all repository checks are green.
