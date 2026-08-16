# Pre-release recovery

This runbook covers failures after the protected `publish` job begins creating
or reading back a GitHub pre-release. It does not authorize a release. The
current repository truth is `candidate / not_published / releaseEligible=false`,
so the release eligibility barrier intentionally prevents publication.

## Recovery invariant

Runs for the same repository and tag are serialized by the workflow concurrency
group `prerelease-${repository}-${tag}`. An in-progress owner is never cancelled
by a later run. This removes ordinary same-tag overlap, while the recovery rules
still fail closed for API races and network ambiguity.

The workflow records remote observation and mutation as separate steps:

- `probe_prerelease.state=existing` is written only after an explicit HTTP 200.
  A later failure retains that pre-existing pre-release.
- `probe_prerelease.state=absent` is written only after an explicit HTTP 404.
  Any other response or transport result stops publication.
- `create_prerelease.outcome=success` is the only proof that this workflow run
  owns the newly created Release for cleanup. Ownership is never declared by an
  output written before `gh release create` completes.

If release readback, receipt generation, or receipt artifact preservation fails
after `create_prerelease.outcome=success`, the workflow deletes only the GitHub
Release for the current, previously verified tag in the fixed
`tttboy123/openusage-bar` repository. It retains the Git tag. A missing Release
is already a successful cleanup state; authentication, network, probe, and real
deletion failures still fail the recovery step instead of being hidden.

## Newly created pre-release

When the workflow reports that it deleted a receiptless pre-release:

1. Confirm the cleanup step succeeded. Do not infer success from the failed
   publish or receipt step.
2. Fix the readback, receipt, or artifact-preservation failure without changing
   the tag or source commit.
3. Rerun the same protected workflow. It must pass the eligibility, artifact,
   attestation, online readback, receipt-generation, and receipt-preservation
   gates again.
4. Keep product truth unchanged until the receipt artifact described below is
   available and reviewed.

The cleanup never removes the Git tag; deleting or moving the immutable tag is
outside this recovery path.

## Unconfirmed creation ownership

If the probe returned 404 but `create_prerelease` did not finish successfully,
the remote state is uncertain. A race may have created the Release between the
probe and mutation, or a network failure may have hidden the result of the
creation request. The workflow does not delete anything in this state.

1. Inspect the GitHub pre-release for the same immutable tag and source commit.
2. Compare its exact remote asset set with the audited release candidate.
3. Resolve the race, network, or asset mismatch without advancing product
   truth.
4. Rerun the protected workflow. Only a preserved and validated publication
   receipt can support the product-truth transition.

## Retained pre-existing pre-release

When `probe_prerelease.state=existing`, a later failure emits an explicit
recovery summary and leaves the pre-existing pre-release untouched:

1. Inspect the failing receipt stage and the public Release assets. Do not
   delete or replace the Release automatically.
2. Resolve any asset-set, digest, tag, source-SHA, prerelease-state, or UTC
   publication-time mismatch.
3. Rerun the workflow for the same tag. The online receipt generator must
   validate the exact remote asset set against the audited candidate.
4. If the mismatch cannot be reconciled without mutating published state, stop
   and perform a separate human-reviewed incident decision. This workflow does
   not claim ownership of the existing Release.

## Advancing product truth

Publication evidence exists only when the `Preserve public publication receipt`
step succeeds and the workflow retains the
`openusage-github-release-receipt` artifact. A step summary, release URL, tag,
package version, or successful upload is not a substitute.

After that successful run, a human-audited pull request may:

1. obtain the retained `github-release-receipt/v1` artifact from the successful
   workflow run;
2. verify its repository, release ID, tag, source SHA, UTC `publishedAt`,
   prerelease flag, manifest digest, asset-set digest, and verified provenance;
3. update canonical product truth as one atomic state transition to
   `releaseStage=prerelease_published`,
   `publicationStatus=published_prerelease`, `releaseEligible=false`, with the
   verified receipt attached; and
4. run the product-truth schema and consistency gates before merging.

No automated workflow mutation of the source tree is permitted. Until that
audit pull request is reviewed and merged, repository product truth remains the
pre-publication state even if a public pre-release exists.
