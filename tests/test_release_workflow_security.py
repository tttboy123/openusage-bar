from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/release.yml"


def _top_level_permissions(source: str) -> str:
    match = re.search(r"(?ms)^permissions:\n(?P<body>(?:  [^\n]+\n)+)", source)
    if match is None:
        raise AssertionError("release workflow must declare top-level permissions")
    return match.group("body")


def _top_level_concurrency(source: str) -> str:
    match = re.search(r"(?ms)^concurrency:\n(?P<body>(?:  [^\n]+\n)+)", source)
    if match is None:
        raise AssertionError("release workflow must declare top-level concurrency")
    return match.group("body")


def _job(source: str, job_id: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(job_id)}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
        source,
    )
    if match is None:
        raise AssertionError(f"release workflow job is missing: {job_id}")
    return match.group(0)


def _named_step(source: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^      - name: {re.escape(name)}\n(?P<body>.*?)(?=^      - (?:name:|uses:)|\Z)",
        source,
    )
    if match is None:
        raise AssertionError(f"release workflow step is missing: {name}")
    return match.group(0)


class ReleaseWorkflowLeastPrivilegeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text(encoding="utf-8")
        cls.build = _job(cls.source, "build_audit")
        cls.publish = _job(cls.source, "publish")

    def test_read_only_build_audit_is_separate_from_protected_publish(self):
        top = _top_level_permissions(self.source)
        self.assertEqual(top.strip(), "contents: read")

        self.assertNotRegex(self.build, r"(?m)^\s+(?:contents|id-token|attestations): write$")
        self.assertNotIn("actions/attest@", self.build)
        self.assertNotIn("gh release create", self.build)
        self.assertIn("actions/upload-artifact@", self.build)

        self.assertRegex(self.publish, r"(?m)^\s+needs: build_audit$")
        self.assertRegex(self.publish, r"(?m)^\s+environment: release$")
        for permission in ("contents: write", "id-token: write", "attestations: write"):
            self.assertIn(permission, self.publish)
            self.assertEqual(self.source.count(permission), 1)
        self.assertIn("actions/download-artifact@", self.publish)

    def test_same_tag_publication_is_serialized_without_cancelling_the_owner(self):
        concurrency = _top_level_concurrency(self.source)

        self.assertIn(
            "group: prerelease-${{ github.repository }}-${{ github.ref_name }}",
            concurrency,
        )
        self.assertIn("cancel-in-progress: false", concurrency)

    def test_downloaded_bytes_are_reverified_before_the_first_side_effect(self):
        download = self.publish.index("actions/download-artifact@")
        first_side_effect = min(
            self.publish.index("actions/attest@"),
            self.publish.index("gh release create"),
        )
        barrier = self.publish[download:first_side_effect]

        self.assertRegex(barrier, r"(?:shasum -a 256 -c|sha256sum --check)")
        self.assertIn("scripts/release_artifact_audit.py", barrier)
        self.assertIn("scripts/release_dmg_audit.sh", barrier)
        self.assertIn("scripts/verify_release_metadata.py", barrier)
        self.assertIn("scripts/verify_product_version_truth.py", barrier)
        self.assertIn("--require-release-eligible", barrier)
        self.assertIn("GITHUB_REF_NAME", barrier)
        self.assertIn("GITHUB_SHA", barrier)

    def test_attestation_is_verified_before_release_and_receipt_is_online_derived(self):
        attest = self.publish.index("actions/attest@")
        verify_attestation = self.publish.index("gh attestation verify", attest)
        publish = self.publish.index("gh release create", verify_attestation)
        read_back = self.publish.index("gh release view", publish)
        receipt = self.publish.index("github-release-receipt/v1", read_back)

        self.assertLess(attest, verify_attestation)
        self.assertLess(verify_attestation, publish)
        self.assertLess(publish, read_back)
        self.assertLess(read_back, receipt)

    def test_remote_probe_and_release_creation_have_separate_recorded_outcomes(self):
        probe = _named_step(self.publish, "Probe pre-release existence")
        create = _named_step(self.publish, "Create pre-release when absent")

        self.assertRegex(probe, r"(?m)^        id: probe_prerelease$")
        self.assertIn("gh api --include --silent", probe)
        self.assertIn('elif [ "$probe_http_status" = "404" ]; then', probe)
        self.assertIn('echo "state=existing" >> "$GITHUB_OUTPUT"', probe)
        self.assertIn('echo "state=absent" >> "$GITHUB_OUTPUT"', probe)
        self.assertNotIn("gh release create", probe)

        self.assertRegex(create, r"(?m)^        id: create_prerelease$")
        self.assertIn(
            "if: steps.probe_prerelease.outputs.state == 'absent'",
            create,
        )
        self.assertIn("gh release create", create)
        self.assertNotIn("gh api", create)
        self.assertLess(self.publish.index(probe), self.publish.index(create))

        # Ownership must come from GitHub's durable step outcome, never from an
        # output written before the release mutation has actually succeeded.
        self.assertNotIn('echo "created=true" >> "$GITHUB_OUTPUT"', self.publish)
        self.assertNotIn("outputs.created", self.publish)

    def test_partial_publication_recovery_only_deletes_a_release_created_by_this_run(self):
        receipt = _named_step(
            self.publish, "Read back release and form publication receipt"
        )
        preserve = _named_step(self.publish, "Preserve public publication receipt")
        cleanup = _named_step(
            self.publish, "Delete receiptless pre-release created by this run"
        )
        unowned = _named_step(
            self.publish, "Report recovery for unconfirmed pre-release ownership"
        )
        retained = _named_step(
            self.publish, "Report recovery for retained pre-existing pre-release"
        )

        self.assertLess(self.publish.index(receipt), self.publish.index(preserve))
        self.assertLess(self.publish.index(preserve), self.publish.index(cleanup))
        self.assertLess(self.publish.index(cleanup), self.publish.index(unowned))
        self.assertLess(self.publish.index(unowned), self.publish.index(retained))
        cleanup_if = re.search(r"(?m)^        if: (?P<condition>.+)$", cleanup)
        self.assertIsNotNone(cleanup_if)
        self.assertEqual(
            cleanup_if.group("condition"),
            "failure() && steps.create_prerelease.outcome == 'success'",
        )
        self.assertNotIn("outputs.created", cleanup)
        self.assertIn('gh release delete "$GITHUB_REF_NAME"', cleanup)
        self.assertIn("--repo tttboy123/openusage-bar", cleanup)
        self.assertIn("--yes", cleanup)
        self.assertNotIn("--cleanup-tag", cleanup)
        self.assertEqual(self.publish.count("gh release delete"), 1)

        self.assertIn(
            "if: failure() && steps.probe_prerelease.outputs.state == 'absent' && steps.create_prerelease.outcome != 'success'",
            unowned,
        )
        self.assertNotIn("gh release delete", unowned)
        unowned_guidance = unowned.lower()
        for required_guidance in (
            "race",
            "network",
            "not delete",
            "inspect",
            "rerun",
            "receipt",
        ):
            self.assertIn(required_guidance, unowned_guidance)

        self.assertIn(
            "if: failure() && steps.probe_prerelease.outputs.state == 'existing'",
            retained,
        )
        self.assertNotIn("gh release delete", retained)
        guidance = retained.lower()
        for required_guidance in (
            "pre-existing pre-release",
            "rerun",
            "product truth",
            "prerelease_published",
        ):
            self.assertIn(required_guidance, guidance)

    def test_only_a_preserved_receipt_can_be_product_truth_publication_evidence(self):
        preserve = _named_step(self.publish, "Preserve public publication receipt")
        evidence = _named_step(
            self.publish, "Record product truth publication evidence"
        )

        self.assertRegex(preserve, r"(?m)^        id: preserve_publication_receipt$")
        self.assertLess(self.publish.index(preserve), self.publish.index(evidence))
        self.assertIn(
            "if: success() && steps.preserve_publication_receipt.outcome == 'success'",
            evidence,
        )
        evidence_text = evidence.lower()
        self.assertIn("receipt artifact", evidence_text)
        self.assertIn("product truth", evidence_text)
        self.assertIn("prerelease_published", evidence_text)


if __name__ == "__main__":
    unittest.main()
