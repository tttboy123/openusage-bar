# OpenUsage Bar 1.0 canary protocol

OpenUsage Bar does not collect telemetry. The 1.0 canary is a manual,
opt-in evidence program: a tester runs the checks below and explicitly submits
the GitHub canary form. Credentials, Provider responses, prompts, model
responses, account identity, device serial numbers, and raw logs are never
requested.

The canary also verifies the
[Local API v1 compatibility policy](api/compatibility-v1.md); UI text is not an
automation interface.

## Intake readiness and clock activation

The repository state `intake_ready` means the issue form, privacy rules,
diagnostic checks, and evidence contract are ready for testers. It does not start the 30-day clock.

Clock activation is a deliberate external coordination step. It occurs only
after maintainers have accepted five independently qualifying external
machines covering the required configuration classes and recorded the UTC
activation timestamp in the tracking issue. A green pull request, a local
machine, an unreviewed report, or repository status alone cannot activate the
clock.

This protocol prepares the intake path but does not recruit testers, assert an
external cohort, or start the public beta. Until a maintainer explicitly
activates it, the clock state is `not_started`.

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
3. Refresh and wait through one scheduled refresh interval. Before restarting
   the Mac, capture a private runtime baseline; after login, verify that a real
   new boot restored launchd, the local API, the ledger, and a subsequent
   scheduled collection without invoking Refresh:

   ```bash
   scripts/verify_reboot_recovery.py capture
   # Restart macOS normally, then return to the checkout.
   scripts/verify_reboot_recovery.py verify --timeout 360
   ```

   The baseline is a short-lived canary artifact; capture it for the current
   reboot attempt, start the new boot within six hours of capture, and run the
   verifier within six hours of that boot. The baseline pins the app bundle,
   both native launchers, and both long-lived runtime executables by
   code-signature hash, so a same-version replacement cannot satisfy the
   reboot check. A successful
   verifier intentionally reports `visualMenuCheck=pending`. Separately confirm
   that the menu-bar item is visibly present and opens its popover; process or
   launchd state is not visual evidence.
4. Upgrade from the previous published pre-release. Confirm the SQLite
   integrity check, history counts, and change cursor do not decrease.
5. Run `scripts/rollback_app.sh`, confirm Local API v1 recovers, then reinstall
   the candidate.
6. Confirm menu-bar, Usage Details, Provider Center, CLI JSON, and Local API
   describe the same `dataRevision` and source health. From the extracted
   release or source checkout, run the privacy-safe surface verifier:

   ```bash
   scripts/verify_canary_surfaces.py \
     --output /tmp/openusage-canary-surfaces.json
   scripts/privacy_scan.py /tmp/openusage-canary-surfaces.json
   ```

   The verifier first deep-verifies the installed app signature, then reads
   `/v1/snapshot` before and after the installed collector's canonical offline
   snapshot. It retries bounded revision drift,
   permits only top-level `generatedAt` and the capacity rows'
   render-time `freshnessSeconds` to differ, and writes a mode-`0600` report
   containing no usage values, Provider identifiers, account references,
   credentials, paths, or raw snapshots. A passing report records
   `visualMenu: pending_manual`: it does not replace the visual menu-bar check.
   Separately confirm the menu-bar item, Usage Details, and Provider Center
   visibly represent that revision and source health. Validate the N-1 reader
   behavior described in the
   [Local API v1 compatibility policy](api/compatibility-v1.md).
7. Record every Unknown, stale, authentication, upgrade, rollback, crash,
   credential, or data-integrity incident. Do not wait until day 30 to report a
   security or data-loss issue.

## Optional redacted diagnostics

From the extracted release or a source checkout:

```bash
scripts/export_diagnostics.py --output /tmp/openusage-diagnostics.json
scripts/privacy_scan.py /tmp/openusage-diagnostics.json
```

The default schema remains diagnostics v1. It reads only `/v1/snapshot` and
`/v1/capabilities`, then writes a mode-`0600` aggregate containing
product/build, macOS/architecture, schema and data revision, aggregate
fact/source counts, aggregate Balance state/quality/stale counts, sanitized
error-code counts, and the public capability catalog. Capability sources retain
only public `factFamilies`, authority, account/model scope and verification
metadata. The export never includes Balance amounts, currencies, source IDs,
Provider instances or account references.

When the candidate exporter captures a private N-1 baseline, an older
capability source may lack the complete additive evidence group
(`factFamilies`, authority, account/model scope and verification). The exporter
keeps that source but labels the missing metadata `unknown` / `unverified` and
uses an empty fact-family list; it never infers current support from legacy
provenance. If only part of the evidence group is present, export fails closed
as malformed instead of silently completing it.

For an explicit daily reconciliation, choose diagnostics v2 and a bounded
local-calendar range:

```bash
scripts/export_diagnostics.py \
  --schema-version 2 \
  --from 2026-07-17 \
  --to 2026-07-18 \
  --timezone Asia/Singapore \
  --output /tmp/openusage-diagnostics-v2.json
scripts/privacy_scan.py /tmp/openusage-diagnostics-v2.json
```

V2 also reads `/v1/activity/daily` and `/v1/sources/status`. It preserves each
source-reported total and, only when the declared counting convention is
arithmetically comparable, reports an expected total and delta. Duplicate
issues are emitted only when duplicate effective rows are actually present;
source-selection history before those effective rows is explicitly not
observable. Complete aggregate totals are emitted only for fully covered or
explicitly covered-zero ranges; partial exports keep their available subtotal
under `observedTokenTotals`. Each row also includes an
`accountTotalComparison`: Codex session files are identified as
`local_device_sessions`, OpenUsage daily data as `local_collector`, and both
are explicitly not comparable with an account-wide dashboard that may include
other devices, web or mobile activity, and unavailable local history. Sources
without a declared account scope remain `unknown`. Account references are
replaced with per-export pseudonyms. It does not read Provider configuration,
Keychain, prompts, responses, or raw Provider payloads. Review the JSON
yourself before attaching it.

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
