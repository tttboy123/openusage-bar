# Native CI Evidence

The desktop packaging workflow runs one fixed six-row matrix: macOS, Windows,
and Linux on x64 and arm64. A push, pull request, or input-free manual dispatch
always runs the complete matrix. There is no input that can silently reduce the
set of targets.

On every hosted-runner row, `desktopRuntimeCapabilitySmoke` exercises the
Collector built on that runner through the desktop bridge before packaging.
This is a native runtime check for that row. Local unit tests and static
workflow/schema checks are useful contract validation, but they are not native
evidence: only a completed matrix row with its paired installer and evidence
bundle retained by CI qualifies.

Each successful row uploads one verified five-file handoff containing the final
installer/container, standalone built Collector, one `native-ci-evidence-v1`
JSON document, one observation-only `distribution-trust-posture/v1` JSON
document, and one path-free handoff manifest. The native document binds the source commit
and GitHub run identifiers to the requested hosted-runner label, observed
operating system and architecture, pinned toolchain versions, native Collector
identity, final-container identity, distribution-posture report identity, gate
outcomes, byte sizes, and SHA-256 digests. It also embeds the complete
[`artifact-build-identity/v1`](artifact-build-identity.md) projection and binds
its fixed packaged name, canonical byte size, and SHA-256 digest. Its schema is
[`docs/schemas/native-ci-evidence-v1.schema.json`](schemas/native-ci-evidence-v1.schema.json).

Every newly generated Windows or Linux document must also embed one canonical,
platform-matched `observer-source-native-evidence/v1` payload as
`observerSourceEvidence`. The workflow probes and verifies that payload on the
same hosted runner immediately before native evidence generation. macOS rows
must not contain the field. The verifier and release handoff continue to accept
N-1 Windows/Linux documents that predate the field, but those legacy documents
carry no source-support or promotion claim. The embedded payload remains part
of the native evidence JSON rather than becoming a sixth handoff file.
Windows uses Credential Manager directly. Linux prepares a pinned, ephemeral
D-Bus/Secret Service session only around the source probe, as documented in
[`observer-native-runner-prerequisites.md`](observer-native-runner-prerequisites.md).
That bootstrap is not serialized as a native-evidence check and is not a
support claim.

The upload unit is now the verified
[`release-handoff/v1`](release-handoff.md) directory. It also retains the
standalone built Collector and one path-free handoff manifest, so the exact
native evidence can be re-verified after download without executing or first
extracting the installer. The handoff remains a local non-claim and does not
change any distribution trust value.

The generator rejects a self-hosted runner, an invalid target/runner pairing,
unexpected skipped gates, a symlink or non-regular input, a wrong Collector
format or architecture, an unexpected final filename or basic container
header/architecture, and malformed or non-canonical evidence. The stronger
container proof comes from the preceding platform gate: read-only DMG mount,
non-executing NSIS extraction, or non-executing AppImage payload extraction.
The verifier reopens the Collector, final container, and posture report
immediately before upload, validates that the posture report still binds the
same platform and final-container bytes, and recomputes all three identities
and digests. `productVersionTruth` records the independent source contract gate;
`packagedBuildIdentity` can pass only after the selected platform's final
container audit succeeds and the canonical identity is re-verified. The final
package audit checks the fixed identity bytes and native version metadata on
macOS, Windows, and Linux; final AppImages additionally require their single
desktop-entry identity. Each paired CI bundle has an explicit 30-day
artifact retention period. This is bounded short-term traceability, not the
long-term release archive.

On every platform, the final-container identity is a private read-only snapshot,
not a later lookup of the mutable build-output path. macOS mounts the DMG
snapshot, Windows extracts the NSIS snapshot, and Linux extracts the AppImage
snapshot. Each row stores the initial snapshot digest, rechecks it after the
container and posture audit, and passes that same path to the native evidence
generator/verifier and artifact upload.

Evidence contains an allowlist of safe fields. It deliberately omits actor,
repository, ref, runner name, host name, user name, workspace paths, URLs,
logs, requests, responses, credentials, and environment dumps. The evidence
records that the final extracted or mounted package passed the dedicated
`release-artifact-audit/v1` policy; it does not claim that the generator scans
compressed container bytes directly. That release audit includes private-name,
home-path, and raw-content canary rules, where the canary rule is not a general
DLP guarantee. The two zero network/read counters refer only to the
deterministic frozen Gateway smoke; they do not claim that dependency
installation or the entire CI job is offline.

## Local contract check

```bash
python -m unittest \
  tests.test_artifact_build_identity \
  tests.test_distribution_trust_posture \
  tests.test_native_ci_evidence \
  tests.test_observer_source_native_evidence \
  tests.test_release_handoff \
  tests.test_desktop_packaging_contract -v
python scripts/verify_artifact_build_identity.py
python scripts/verify_action_pins.py
python -m json.tool docs/schemas/artifact-build-identity-v1.schema.json >/dev/null
python -m json.tool docs/schemas/native-ci-evidence-v1.schema.json >/dev/null
python -m json.tool \
  docs/schemas/observer-source-native-evidence-v1.schema.json >/dev/null
python -m json.tool \
  docs/schemas/distribution-trust-posture-v1.schema.json >/dev/null
```

`generate` and `verify` are implemented by
`scripts/native_ci_evidence.py`. Their errors use stable reason codes and do
not echo local paths.

## Trust boundary

This JSON is a CI-run and artifact-integrity record, not a cryptographic
signature, notarization result, or public supply-chain attestation. Native
support is only proven after the corresponding hosted runner completes and its
paired artifact/evidence bundle is retained. Signing, notarization, long-term
evidence retention, canary validation, and release attestation remain separate
release gates; none is implied by the desktop runtime capability smoke.

Embedding source evidence does not directly change the Provider catalog or the
Local API supported-source count. Only a retained, real hosted-runner document
whose embedded source record is verified may be consumed by a separate atomic
catalog-promotion change. Local fixtures, containers, static workflow checks,
legacy evidence without the field, and release-handoff verification are not
promotion evidence.

The job assumes its pinned hosted-runner actions and checked-out build code are
not already executing a hostile same-identity background process. Snapshot
digests catch normal path drift and substitution across the declared gates;
they cannot turn a compromised build identity into a trusted release
environment. Isolation from the build job and downloaded-artifact/provenance
verification remain requirements of the future release gate.

Every native evidence document also carries a closed `distributionTrust`
record under the `native-distribution-trust-nonclaim/v1` policy. It records
`platformCodeSigning: "not_verified"`,
`provenanceAttestation: "not_verified"`, and `releaseEligible: false` on every
platform. `platformNotarization` is `"not_verified"` on macOS and
`"not_applicable"` on Windows and Linux. These values are a conservative
non-claim and trust posture, not the result of signature detection. A package
may still contain an ad-hoc or other signature, but this evidence cannot be
used to claim Apple Developer ID signing, Authenticode signing, notarization,
or provenance attestation.

Changing those trust facts requires the appropriate platform credentials and
services plus an independent release workflow. This evidence neither satisfies
those external gates nor starts or proves a canary rollout.

The separate
[`distribution-trust-posture/v1`](distribution-trust-posture.md) report is the
read-only detector output. Its final-container digest and size are verified
before upload; the report's own name, size, and digest are then embedded in the
native evidence. `distributionTrustPosture: "passed"` records only that this
closed observation gate ran successfully. The detector's dynamic enum never
overwrites the conservative `distributionTrust` values above. In particular,
ad-hoc validity, an unpinned Developer ID or Authenticode result, and a stapled
ticket still cannot make `releaseEligible` true without the independent
publisher and provenance release policy.
