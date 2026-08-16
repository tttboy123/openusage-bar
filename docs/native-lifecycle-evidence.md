# Native install lifecycle evidence

`native-lifecycle-evidence/v1` is the closed, path-free record for one real
Windows x64 or Linux x64 final-container lifecycle. Its schema is
[`docs/schemas/native-lifecycle-evidence-v1.schema.json`](schemas/native-lifecycle-evidence-v1.schema.json),
and its validator/generator is `scripts/native_lifecycle_evidence.py`.

The record binds the source commit and final NSIS/AppImage name, size, and
SHA-256 digest to nine observed lifecycle checks: install, first run,
observation-only default, user-service registration, preserve uninstall,
service removal, reinstall, confirmed delete uninstall, and final state
removal. It also fixes Gateway startup to `observe`, requires the canonical
default Gateway endpoint plus canonical cache and telemetry artifacts to remain
absent, and records zero Provider credential reads and Provider network calls.
The `gateway.listenerActive` field refers only to that canonical default Gateway
endpoint; it does not prove custom Gateway endpoints absent.

The production generator is deliberately fail-closed. It accepts no client
supplied pass/fail JSON and creates no report unless its built-in external
platform driver performs and observes the complete lifecycle. Linux lifecycle
mutation is reachable through an identity-bound start/stop process lease and
the preserve/delete rollback transactions. The privacy event authority remains
unavailable, however, so the built-in Linux driver cannot produce a lifecycle
report. This document remains a contract foundation rather than hosted
lifecycle evidence.

An optional `nativeLifecycleEvidence` object may be embedded in a Windows x64
or Linux x64 `native-ci-evidence-v1` document after the lifecycle report is
independently verified against the same final container and source commit. The
field is forbidden on macOS and arm64 rows. Legacy native documents without the
field remain valid. It remains inside the existing native evidence JSON and
does not create a sixth release-handoff file.

## Local contract check

```bash
python -m unittest \
  tests.test_native_lifecycle_evidence \
  tests.test_native_lifecycle_runner \
  tests.test_platform_services \
  tests.test_native_ci_evidence -v
python -m json.tool \
  docs/schemas/native-lifecycle-evidence-v1.schema.json >/dev/null
```

## Non-claims

Every record fixes `synthetic: false` and `releaseEligible: false`. Even a
verified record proves only the named candidate, target, artifact, and bounded
lifecycle observations. It does not prove arm64 or macOS lifecycle support,
signing, notarization, provenance, SBOM publication, reference-machine
performance, native screen-reader parity, canary completion, or release
eligibility. Unit executors and final-container self-tests cannot create hosted
lifecycle evidence.
