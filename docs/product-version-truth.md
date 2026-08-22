# Product and version truth

`openusage_bar/resources/product-version-truth.v1.json` is the closed,
machine-readable identity contract shared by release, client, UI, and test
work. It does not bump a version or make a release claim.

The current repository has two deliberately separate truths:

- published stable baseline: `v0.7.1`;
- development candidate: `UsageHub 0.8.7 RC (build 29)`, not published and
  not release eligible.

`releaseStage=candidate` describes the candidate's lifecycle. The independent
`publicationStatus=not_published` field states whether it has actually been
published. A package version, Git tag, API version, CI artifact, native CI
evidence report, or local release handoff must never be used to infer a stable
publication.

Candidate channels are limited to `alpha`, `beta`, and `rc`; `stable` is not a
valid candidate channel. Candidate publication is a closed three-state union:

| `releaseStage` | `publicationStatus` | `releaseEligible` | receipt |
| --- | --- | --- | --- |
| `candidate` | `not_published` | `false` | `null` |
| `prerelease_ready` | `not_published` | `true` | `null` |
| `prerelease_published` | `published_prerelease` | `false` | verified, closed receipt |

No crossed or partial row is valid. Eligibility is a one-use authorization to
attempt a prerelease; it is necessary but never sufficient evidence of
publication. A published candidate is no longer eligible for a second publish.
The tag release workflow adds the stricter gate:

```bash
python scripts/verify_product_version_truth.py --require-release-eligible
```

It runs before attestation or publication and can additionally bind the expected
tag and source SHA. The current candidate intentionally fails because
`releaseEligible=false`; ordinary consistency verification still passes so
development and CI can continue without implying publication.

The release workflow separates a read-only build/audit job from a protected
`release` environment job. Only the latter receives `contents`, OIDC, and
attestation write permissions. It downloads the audited artifact, rechecks its
checksums and packaging policy, binds the eligibility decision to the tag and
source commit, attests and verifies every asset, publishes a prerelease, and
then reads it back online. The read-back creates a path-free
`github-release-receipt/v1` containing only the public repository/release ID,
tag, source SHA, UTC publication time, manifest and asset-set SHA-256 digests,
and verified-prerelease facts. Receipt fields never enter renderer projections.

Failure ownership, automatic rollback boundaries, retained-release recovery,
and the manual receipt-to-product-truth audit are defined in the
[pre-release recovery runbook](release-recovery.md).

A future stable promotion must use a separate policy and verified stable
release receipt. It updates `publishedBaseline` while atomically starting a
strictly newer candidate; changing a candidate channel to `stable` is never a
promotion mechanism.

## Compatible identities

- `UsageHub` is the user-facing product name.
- `OpenUsage Bar` remains only where existing installations and automation
  require it: the legacy macOS app bundle, executable names, CLI, paths,
  socket, bundle identifier, and legacy macOS release assets.
- `OpenUsage-Bar-v{version}-macos-arm64.{ext}` belongs to the legacy native
  macOS release track.
- `UsageHub-{version}-{platform}-{artifactArch}.{ext}` belongs to the new
  cross-platform native CI evidence/handoff track. Its current role is local
  CI handoff only; it is not a public-release claim.

Product versions and interface versions are separate. Local API `1.0`,
Gateway API `gateway.openusage/v1`, and runtime capability
`runtime-capability.openusage/v1` cannot be presented as the UsageHub product
version or as proof that Gateway is enabled or published.

Run the portable consistency gate with:

```bash
python scripts/verify_product_version_truth.py
```

The command prints only a fixed failure message when any bound source drifts,
so paths or file contents are not reflected into CI logs.

For release inventory, run the non-authoritative readiness preflight:

```bash
python scripts/release_readiness.py --output /tmp/openusage-release-readiness.json
```

This report combines the local product/version and artifact identity gates with
an explicit inventory of external-evidence blockers. It is intentionally
`decisionAuthority=inventory_only` and must not be used as the final release
authorization gate. Missing hosted native runner evidence, protected release
environment proof, signing/notarization proof, UI parity, clean reference
performance, or canary evidence keeps the inventory blocked. The public CLI does
not accept self-reported external evidence; authoritative release approval
requires the protected publish workflow and real platform/signing/canary
verifiers.
