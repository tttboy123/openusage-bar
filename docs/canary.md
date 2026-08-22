# OpenUsage Bar observation canary protocol

In `observe` mode OpenUsage Bar does not collect request telemetry. This
observation canary is a manual, opt-in evidence program: a tester runs the
checks below and explicitly submits the GitHub canary form. Credentials,
Provider responses, prompts, model responses, account identity, device serial
numbers, local Gateway databases, and raw logs are never requested.

The canary also verifies the
[Local API v1 compatibility policy](api/compatibility-v1.md); UI text is not an
automation interface.

## Observation and future Gateway cohorts

This file currently activates only the existing observation gate: five
external Apple Silicon Macs, five Provider configuration classes, and 30
consecutive days. Adding Windows, Linux, or an opt-in Gateway cohort does not
reduce or replace that gate.

The future 1.0 Gateway cohort is separate and cannot start until a release
candidate documents and verifies all of the following:

- Gateway is disabled on fresh install and upgrade, and Observer mode performs
  no Gateway Provider call or credential read;
- local `gateway-telemetry.sqlite3` contains no prompt, response, credential,
  cookie, direct identity, or raw Provider payload;
- cache is separately opted in, bounded by TTL/LRU/size, bypasses uncertain or
  side-effecting content, and has a tested clear operation;
- neither Gateway database is copied into diagnostics, canary reports, crash
  reports, or release artifacts; submitted evidence contains aggregates only;
- install, first run, upgrade, rollback, uninstall, service recovery,
  credential failure, advise mode, and Gateway mode pass their published
  Windows and Linux matrices; and
- disabling or uninstalling Gateway has documented and tested cache/telemetry
  deletion semantics without deleting the observation ledger.

The following native matrix defines the future Gateway evidence; it is not an
active cohort and a simulated platform result cannot satisfy it:

| Scenario | Windows evidence | Linux evidence | Required invariant |
| --- | --- | --- | --- |
| Clean install and first run | Signed/attested NSIS install and Task Scheduler state | Attested AppImage first run and systemd user state | Starts in `observe`; no Gateway listener, credential read, Provider call, cache, or telemetry database. |
| N-1 upgrade and rollback | Install previous candidate, upgrade, then roll back and relaunch | Run previous AppImage state, upgrade, then roll back and relaunch | Ledger revision and Local API v1 remain readable; Gateway remains disabled until the tester opts in again. |
| Uninstall and reinstall | Uninstall through the Windows package path, then reinstall | Remove AppImage/service registration, then reinstall | Executables and services are removed; observation ledger/credentials follow the published preserve policy; Gateway cache/telemetry follow the separately confirmed delete choice. |
| Advise mode | Native loopback Should-Send fixture | Native loopback Should-Send fixture | Uses recorded facts only; zero Provider calls and zero Provider credential reads. |
| Gateway mode | Five offline Provider protocol fixtures through the packaged Collector | Five offline Provider protocol fixtures through the packaged Collector | Separate bearer, credential isolation, bounded stream/cache/fallback behavior, and Observer continuity. |
| Cache and telemetry clear | Clear each store independently and restart | Clear each store independently and restart | No activity-ledger deletion; cleared content is not replayed or restored from diagnostics. |
| Credential backend absent or denied | Missing/denied Windows Credential Manager fixture | Missing/denied Secret Service fixture | Credentialed Gateway fails closed with a sanitized error; Observer and Local API stay available. |
| Diagnostics and artifact exclusion | Scan installed files, exported diagnostics, and crash fixtures | Scan installed files, exported diagnostics, and crash fixtures | No token, Provider credential, prompt, response, raw chunk, direct identity, cache DB, or telemetry DB crosses the boundary. |

Each row requires install scope, OS version, package digest, aggregate pass/fail,
and UTC timestamps. Reports do not attach the local databases or raw runtime
logs. A matrix passes only when both native columns pass the same candidate;
cross-compilation and macOS mocks remain useful CI checks but are not native
evidence.

Local bounded Gateway telemetry is functional state, not remote analytics. It
exists only after explicit opt-in, remains on the tester's machine, and is not
submitted by this protocol.

## Intake readiness and clock activation

The repository state `intake_ready` means the issue form, privacy rules,
diagnostic checks, and evidence contract are ready for testers. It does not start the 30-day clock.

Clock activation is a deliberate external coordination step. It occurs only
after maintainers have accepted five independently qualifying external
machines covering the required configuration classes and recorded the UTC
activation timestamp in the tracking issue. A green pull request, a local
machine, an unreviewed report, or repository status alone cannot activate the
clock.

This protocol alone does not recruit testers, assert an external cohort, or
start the public beta. Publishing a candidate may open intake, but until a
maintainer explicitly activates the timed cohort, the clock state is
`not_started`.

The repository is preparing the
[v0.8.7 RC candidate](https://github.com/tttboy123/openusage-bar/releases/tag/v0.8.7).
That link becomes an intake surface only after the immutable candidate is
published; preparing metadata does not activate or qualify the cohort. The
last published baseline remains v0.7.1.
Accepted-machine counts, configuration-class coverage, blocking incidents and
the eventual UTC activation timestamp are recorded in
[Canary tracking issue #33](https://github.com/tttboy123/openusage-bar/issues/33).
Publishing the candidate opens intake; it does not by itself qualify a machine
or start the clock.

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
   The first command is the trust bootstrap: verify the ZIP before executing
   files extracted from it. Use the single candidate version below only after
   the candidate assets have been published and checksummed.

   The current candidate version is `0.8.7`:
   ```bash
   gh attestation verify OpenUsage-Bar-v0.8.7-macos-arm64.zip \
     --repo tttboy123/openusage-bar \
     --signer-workflow \
       tttboy123/openusage-bar/.github/workflows/release.yml \
     --source-ref refs/tags/v0.8.7 \
     --deny-self-hosted-runners
   shasum -a 256 -c OpenUsage-Bar-v0.8.7-macos-arm64.zip.sha256
   unzip OpenUsage-Bar-v0.8.7-macos-arm64.zip
   cd OpenUsage-Bar-v0.8.7-macos-arm64
   scripts/verify_canary_candidate.py --assets-dir .. --version 0.8.7
   ```

   The packaged verifier requires the expected version and exactly one release
   manifest, validates all
   five published assets against its SHA-256 and size, checks both checksum
   files and the SPDX 2.3 product identity, then verifies all six GitHub
   attestations. It pins the repository, release workflow, tag ref, source
   commit and GitHub-hosted runner policy. Missing GitHub CLI, missing assets,
   malformed metadata, hash drift, timeout or any failed attestation is a hard
   failure. It prints only the version and aggregate pass counts.
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
