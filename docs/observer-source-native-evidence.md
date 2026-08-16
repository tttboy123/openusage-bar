# Observer source native evidence

OpenUsage Bar keeps platform support claims tied to source-level evidence. The
temporary `observer-source-native-evidence/v1` probe exercises two narrow
Observer candidates on a real GitHub-hosted Windows or Linux runner:

- `moonshot/moonshot_official_api` checks the platform-native credential
  backend and the production `moonshot.balance` fact parser with a synthetic
  fixture client.
- `codex/codex_local_log` checks the default-home file shape plus the
  production `codex.local_rate_limits` and `codex.local_sessions` parsers.

The sources are evaluated independently. For example, a Linux runner without a
usable Secret Service may report Moonshot as `unverified` while Codex remains
`verified`. `verifiedSourceCount` must always equal the number of verified
source records. Required seams are reported independently: a successful native
credential round trip with a malformed Moonshot fixture reports
`credentialBackend=verified` and `factParser=not_verified`; a parser is never
marked verified merely because another seam failed.

## Trust boundary

The probe derives its platform from `sys.platform` and runs only when both
`GITHUB_ACTIONS=true` and `RUNNER_ENVIRONMENT=github-hosted` are present. It has
no platform, home-directory, secret, status, or result override. Off-host use
fails closed without creating the requested evidence file or echoing its path.

Evidence contains only fixed capability identifiers, seam states, reason codes,
and counts. The closed schema rejects additional fields, paths, raw provider
payloads, credentials, account identity, and crossed source states. Canonical
output is deterministic ASCII, newline-terminated, and bounded to 16 KiB.

## Native fixtures and cleanup

The Moonshot probe refuses to overwrite an existing probe credential. On a
clean backend it writes one random synthetic value, reads it through the real
adapter, validates a bounded fixture response, and deletes the value in a
`finally` path. An existing credential or a cleanup failure invalidates the
whole probe. A partial native write is detected by reading back the reserved
account and is removed only when its value still matches the probe's synthetic
secret; a concurrent value change is preserved and invalidates the probe.

The Codex probe refuses existing or symlinked target state. It creates a single
synthetic session under `.codex/sessions`, exercises both quota and usage
parsers, verifies the generated cache, and removes only paths it owns. It never
recursively deletes a home, `.codex`, or state directory; unexpected state or a
cleanup failure invalidates the probe. File cleanup is bound to the device and
inode captured by the probe, so a concurrently replaced file is preserved and
the evidence fails closed.

## Commands

Render the committed schema:

```bash
python scripts/observer_source_native_evidence.py schema \
  --output docs/schemas/observer-source-native-evidence-v1.schema.json
```

Run the probe on an eligible native runner:

```bash
python scripts/observer_source_native_evidence.py probe \
  --output observer-source-native-evidence.json
```

Verify a canonical payload offline before embedding it:

```bash
python scripts/observer_source_native_evidence.py verify \
  --evidence observer-source-native-evidence.json
```

Offline verification rejects symlinks, files over 16 KiB, non-ASCII or
non-canonical JSON, duplicate keys, unknown fields, and crossed count/seam
states. Argument and validation errors use fixed reason codes and never echo
the supplied path.

Validate the implementation locally:

```bash
python -m unittest tests.test_observer_source_native_evidence
python -m unittest \
  tests.test_moonshot \
  tests.test_codex_daily \
  tests.test_codex_subscription \
  tests.test_keychain
```

Hosted-runner operating-system prerequisites and their non-promotion boundary
are documented in
[`observer-native-runner-prerequisites.md`](observer-native-runner-prerequisites.md).
Windows runs the native Credential Manager path directly. Linux alone uses a
pinned, private, disposable D-Bus/Secret Service session; package installation
or service startup never marks a source verified.

## Promotion rule

This temporary document is not a release handoff file and does not by itself
increase Windows or Linux support counts. The desktop workflow now probes and
verifies it on each Windows/Linux hosted runner, then embeds the exact payload
as `native-ci-evidence-v1.observerSourceEvidence`; it does not add a sixth
release-handoff artifact. New Windows/Linux native evidence requires the
embedded object, macOS forbids it, and N-1 Windows/Linux evidence without it is
accepted only for offline compatibility. Only a retained payload produced by
the real hosted runner and accepted by the combined native-evidence validator
may feed an atomic promotion of the corresponding catalog source. Local mocks,
temporary directories, unit tests, package builds, legacy evidence, release
handoffs, and static workflow checks remain non-promoting evidence.
