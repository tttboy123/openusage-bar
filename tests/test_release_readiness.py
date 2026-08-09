from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import release_readiness


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/release_readiness.py"


def complete_external_evidence() -> dict[str, object]:
    return {
        "canary": {
            "gatewayCohort": True,
            "observationCohort": True,
        },
        "cleanReferencePerformance": True,
        "nativeRunnerEvidence": {
            "linux": True,
            "mac": True,
            "windows": True,
        },
        "protectedReleaseEnvironment": True,
        "schemaVersion": "release-readiness-evidence/v1",
        "signing": {
            "linuxSignatureVerified": True,
            "macDeveloperIdNotarized": True,
            "windowsAuthenticodePublisherPinned": True,
        },
        "uiParity": {
            "electron": True,
            "screenReader": True,
            "swift": True,
            "web": True,
        },
    }


class ReleaseReadinessTests(unittest.TestCase):
    def test_current_candidate_is_machine_readable_blocked_without_path_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "readiness.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--root",
                    str(ROOT),
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertEqual(
                result.stdout,
                "release_readiness_blocked blockers=9\n",
            )
            self.assertEqual(result.stderr, "")
            self.assertNotIn(str(ROOT), result.stdout + result.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["schemaVersion"], "release-readiness/v1")
            self.assertEqual(report["object"], "release.readiness")
            self.assertEqual(report["decisionAuthority"], "inventory_only")
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(
                report["product"],
                {
                    "candidateBuild": "28",
                    "candidateVersion": "0.8.6",
                    "channel": "rc",
                    "name": "UsageHub",
                    "publishedBaseline": "v0.7.1",
                },
            )
            self.assertEqual(
                report["localChecks"],
                {
                    "artifactBuildIdentity": "passed",
                    "productVersionTruth": "passed",
                },
            )
            self.assertEqual(
                report["blockers"],
                [
                    "authoritative_release_verifier_missing",
                    "candidate_not_release_eligible",
                    "canary_evidence_missing",
                    "clean_reference_performance_missing",
                    "native_runner_evidence_missing",
                    "protected_release_environment_unverified",
                    "signing_notarization_evidence_missing",
                    "ui_parity_evidence_missing",
                    "windows_linux_native_evidence_missing",
                ],
            )

    def test_complete_external_evidence_still_cannot_override_candidate_gate(self):
        report = release_readiness.build_report(
            ROOT,
            complete_external_evidence(),
        )

        self.assertEqual(
            report["blockers"],
            [
                "authoritative_release_verifier_missing",
                "candidate_not_release_eligible",
            ],
        )
        self.assertEqual(report["decisionAuthority"], "inventory_only")
        self.assertEqual(report["status"], "blocked")
        self.assertIs(report["releaseEligible"], False)

    def test_self_reported_evidence_never_promotes_ready_candidate_to_authoritative_ready(self):
        ready_truth = {
            "candidate": {
                "build": "29",
                "channel": "rc",
                "publicationReceipt": None,
                "publicationStatus": "not_published",
                "releaseEligible": True,
                "releaseStage": "prerelease_ready",
                "version": "0.8.7",
            },
            "product": {"displayName": "UsageHub"},
            "publishedBaseline": {"tag": "v0.8.6"},
        }

        with (
            patch.object(
                release_readiness,
                "verify_product_version_truth",
                return_value=ready_truth,
            ),
            patch.object(
                release_readiness,
                "verify_repository_artifact_build_identity",
                return_value=None,
            ),
        ):
            report = release_readiness.build_report(
                ROOT,
                complete_external_evidence(),
            )

        self.assertEqual(report["status"], "blocked")
        self.assertIs(report["releaseEligible"], True)
        self.assertEqual(report["decisionAuthority"], "inventory_only")
        self.assertEqual(
            report["blockers"],
            ["authoritative_release_verifier_missing"],
        )

    def test_cli_rejects_self_reported_evidence_without_echoing_private_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "private-self-reported-release-evidence.json"
            output = root / "readiness.json"
            evidence.write_text(
                json.dumps(
                    complete_external_evidence(),
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--root",
                    str(ROOT),
                    "--evidence",
                    str(evidence),
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotIn(str(evidence), result.stdout + result.stderr)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertEqual(
                result.stderr,
                "release_readiness_invalid reason=arguments_invalid\n",
            )
            self.assertFalse(output.exists())

    def test_invalid_evidence_and_existing_output_fail_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_marker = root / "private-release-marker"
            evidence = root / "evidence.json"
            output = root / "readiness.json"
            evidence.write_text(
                json.dumps(
                    {
                        "schemaVersion": "release-readiness-evidence/v1",
                        "nativeRunnerEvidence": {"mac": True, "windows": True},
                        "privatePath": str(private_marker),
                    }
                ),
                encoding="utf-8",
            )
            output.write_text("existing", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--root",
                    str(ROOT),
                    "--evidence",
                    str(evidence),
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertEqual(
                result.stderr,
                "release_readiness_invalid reason=output_exists\n",
            )
            self.assertNotIn(str(private_marker), result.stdout + result.stderr)

    def test_evidence_symlink_is_rejected_without_path_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_marker = root / "private-evidence-target"
            evidence = root / "evidence-link.json"
            output = root / "readiness.json"
            private_marker.write_text(
                json.dumps(complete_external_evidence()),
                encoding="utf-8",
            )
            try:
                evidence.symlink_to(private_marker)
            except OSError as error:
                self.skipTest(f"symlink unavailable on this platform: {error}")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--root",
                    str(ROOT),
                    "--evidence",
                    str(evidence),
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertEqual(
                result.stderr,
                "release_readiness_invalid reason=arguments_invalid\n",
            )
            self.assertNotIn(str(private_marker), result.stdout + result.stderr)
            self.assertFalse(output.exists())

    def test_output_symlink_is_rejected_before_writing_without_path_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_marker = root / "private-output-target"
            output = root / "readiness-link.json"
            private_marker.write_text("existing", encoding="utf-8")
            try:
                output.symlink_to(private_marker)
            except OSError as error:
                self.skipTest(f"symlink unavailable on this platform: {error}")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--root",
                    str(ROOT),
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertEqual(
                result.stderr,
                "release_readiness_invalid reason=output_exists\n",
            )
            self.assertEqual(private_marker.read_text(encoding="utf-8"), "existing")
            self.assertNotIn(str(private_marker), result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
