# Distribution Trust Posture

The desktop build produces a separate, observation-only distribution trust
posture report for every final container. The report answers what the hosted
runner could observe about that exact artifact. It does not grant release
eligibility and it does not replace signing, notarization, provenance, or
canary release gates.

The closed contract is
[`distribution-trust-posture-v1.schema.json`](schemas/distribution-trust-posture-v1.schema.json).
Every report contains only the target platform, a final-container name,
SHA-256 digest and byte size, closed signing/notarization enums, the fixed
`not_verified` provenance state, and `releaseEligible: false`. It omits local
paths, Team IDs, certificate subjects or fingerprints, native tool output,
runner identity, environment data, and credentials.

Before any final-container audit, each platform row copies the resolved build
output to a private, read-only snapshot with the same filename. The row stores
that snapshot's initial SHA-256 digest, performs extraction or mounting,
package/runtime audit, and posture inspection against the snapshot, then
requires the digest to remain unchanged. Posture verification, native-evidence
generation/verification, and upload all consume the same audited snapshot.

## Platform observations

- macOS accepts a package root only when `hdiutil info -plist` proves that it is
  `UsageHub.app` on a non-writeable mount backed by the exact DMG being hashed.
  Replacing the original build path after the mount therefore cannot substitute
  a different uploaded container.
  It uses
  `/usr/bin/codesign --display --verbose=4` only to distinguish unsigned,
  ad-hoc, Developer ID, and other signature styles. Formal validity comes from
  `/usr/bin/codesign --verify --deep --strict`; display success alone is never
  treated as validity. `/usr/bin/xcrun stapler validate` observes whether the
  final DMG carries a stapled notarization ticket.
- Windows asks the module-qualified PowerShell
  `Microsoft.PowerShell.Security\Get-AuthenticodeSignature` command for the
  final NSIS installer's status. `Valid`, `NotSigned`, invalid statuses, and
  tool/protocol failures normalize to a closed enum. This is an observation;
  no publisher certificate is pinned by this policy.
- Linux does not execute the AppImage and does not invent a platform signing
  convention. Code signing and notarization are both `not_applicable` until a
  detached-signature policy and repository-pinned public key are frozen.

An observed `ad_hoc_strict_valid`, `developer_id_style_strict_valid`, or
Windows `valid` result is still not Gatekeeper acceptance or publisher
authorization. The macOS `strict` suffix means only that `codesign` completed
its formal deep/strict verification; it does not represent OS policy. The
native CI evidence keeps its separate
conservative `distributionTrust` non-claim, and every posture report remains
ineligible until an independent release workflow verifies the expected
publisher, public provenance attestation, and release/canary policy.

## Failure and privacy behavior

Artifact and package-root symlinks are rejected before any platform tool is
called. The platform-specific filename and basic DMG, PE/NSIS, or ELF/AppImage
container structure must also agree with `targetPlatform`. Platform commands
use fixed argument arrays, no shell, a bounded runtime, and bounded output.
Tool exceptions, timeouts, malformed output, and overflow become `unknown` in
the programmatic observation result; the CLI refuses to persist an operational
CI report while a required probe remains unknown. Raw output is discarded.
Report verification uses
strict duplicate-key, size, closed-field, canonical-JSON, platform-enum, and
artifact-identity checks. CLI failures return one generic message without
echoing a path or native diagnostic.

The snapshot and digest chain detects workflow-visible path drift and binds the
normal hosted-runner build flow. It is not an isolation boundary against
malicious code already executing as the same runner identity: such a process
could alter the checkout, tools, files, or action inputs between system calls.
That stronger threat requires a separate least-privilege release job, expected
publisher verification, and public provenance attestation. This detector keeps
`releaseEligible: false` precisely because those gates are not present here.

## Local contract check

```bash
python -m unittest tests.test_distribution_trust_posture -v
python -m json.tool \
  docs/schemas/distribution-trust-posture-v1.schema.json >/dev/null
```

Inspection and immediate binding verification use:

```bash
python scripts/distribution_trust_posture.py inspect \
  --platform linux \
  --package-root dist-desktop/linux-unpacked \
  --artifact dist-desktop/UsageHub-0.8.6-linux-x86_64.AppImage \
  --output dist-evidence/usagehub-distribution-trust-linux-x64.json

python scripts/distribution_trust_posture.py verify \
  --report dist-evidence/usagehub-distribution-trust-linux-x64.json \
  --platform linux \
  --artifact dist-desktop/UsageHub-0.8.6-linux-x86_64.AppImage
```

The CLI intentionally has no option for supplying signing status,
notarization status, provenance, certificate identity, or release eligibility.
