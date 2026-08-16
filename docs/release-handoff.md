# Local Release Handoff

Each successful native desktop CI row produces a five-file, path-free local
handoff directory:

```text
UsageHub-<version>-<platform>-<artifact-arch>.<container>
openusage-collector[.exe]
usagehub-native-evidence-<platform>-<arch>.json
usagehub-distribution-trust-<platform>-<arch>.json
usagehub-release-handoff-<platform>-<arch>.json
```

The handoff is an offline integrity and transport contract. It lets a later
job or a downloaded CI artifact re-run the full native-evidence verification
without extracting the Collector from the installer. It does not sign,
notarize, attest, publish, start a canary, or grant release eligibility.

The closed manifest contract is
[`release-handoff-v1.schema.json`](schemas/release-handoff-v1.schema.json).
It records only the four bound file names, SHA-256 digests and byte sizes,
the product version, target platform/architecture, the source identifiers
already allowlisted by native CI evidence, and the exact inline
[`artifact-build-identity/v1`](artifact-build-identity.md) binding copied from
that evidence. It contains no local paths,
repository/ref/actor identity, environment dump, native tool output, URL,
credential, request, or response data.

The identity binding contains the complete 13-field identity plus the fixed
name `product-build-identity.v1.json`, canonical byte size, and SHA-256 digest.
Because canonical bytes can be reconstructed and checked from those fields,
the handoff remains exactly five files; the identity is not a sixth payload.
On current Windows/Linux rows, the native evidence file also contains the
verified `observerSourceEvidence` object. That object is verified in place and
is likewise not copied as a sixth payload. N-1 Windows/Linux native evidence
without that object remains readable and transportable, but contains no source
promotion claim. macOS native evidence must not contain it.

`provenanceAttestation` is always `not_verified` and `releaseEligible` is
always `false`. The policy is
`release-handoff-local-nonclaim/v1`. Changing those values requires a separate
least-privilege release workflow that verifies the expected publisher,
downloaded artifact, public provenance attestation, and canary policy.

## Assembly and offline verification

Assembly refuses an existing output directory and symlinked or non-regular
inputs. It first verifies the original native evidence against the exact
Collector, final container, and distribution posture report. It then copies
those four inputs into a new private directory, writes one canonical manifest,
and verifies the copied five-file directory again. A partial or invalid
assembly is not reported as success.

```bash
python scripts/release_handoff.py assemble \
  --bundle-dir dist-handoff/mac-arm64 \
  --collector dist-collector/openusage-collector \
  --artifact dist-desktop/UsageHub-0.8.6-mac-arm64.dmg \
  --evidence dist-evidence/usagehub-native-evidence-mac-arm64.json \
  --trust-posture-report \
    dist-evidence/usagehub-distribution-trust-mac-arm64.json

python scripts/release_handoff.py verify \
  --bundle-dir dist-handoff/mac-arm64
```

Verification requires the exact five-file set, rejects symlinks and
non-canonical or duplicate-key manifests, recomputes every size and digest,
re-runs the complete native CI evidence verifier, and rechecks that the
distribution posture remains bound to the final-container bytes. It also
requires the manifest and evidence build-identity bindings to match exactly
and recomputes the identity artifact digest and size from canonical bytes.
Errors use a
small reason-code allowlist and do not echo local paths.

## Trust boundary

The manifest is not a trust root by itself. It detects corruption, omission,
wrong-row mixing, and ordinary path drift. A malicious process already running
as the same build identity could alter the checkout, tools, or files between
system calls. A future release job must run with narrower authority, verify the
downloaded handoff and expected source, and add publisher/provenance evidence
before any release policy can consume it.

This UsageHub native handoff is intentionally separate from the existing
macOS OpenUsage Bar stable-release manifest and SPDX SBOM chain. It does not
rename or weaken that release contract.

## Local contract check

```bash
python -m unittest \
  tests.test_artifact_build_identity \
  tests.test_release_handoff \
  tests.test_native_ci_evidence \
  tests.test_observer_source_native_evidence \
  tests.test_distribution_trust_posture
python scripts/verify_artifact_build_identity.py
python -m json.tool docs/schemas/release-handoff-v1.schema.json >/dev/null
```
