import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import release_handoff as handoff_module
from tests import test_native_ci_evidence as native


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/release_handoff.py"
SCHEMA = ROOT / "docs/schemas/release-handoff-v1.schema.json"
WORKFLOW = ROOT / ".github/workflows/desktop-build.yml"


def _write_target(
    root: Path,
    *,
    platform: str,
    arch: str,
) -> tuple[Path, Path, Path, Path]:
    collector_name, artifact_name = native.TARGET_FILES[(platform, arch)]
    collector = root / collector_name
    artifact = root / artifact_name
    evidence = root / f"usagehub-native-evidence-{platform}-{arch}.json"
    if platform == "mac":
        native._write_macho(collector, arch=arch)
        native._write_dmg(artifact)
    elif platform == "win":
        native._write_pe(collector, arch=arch, pe_plus=True)
        native._write_pe(artifact, arch=arch, pe_plus=True)
    else:
        native._write_elf(collector, arch=arch)
        native._write_elf(artifact, arch=arch)
    observer_source_evidence = None
    if platform != "mac":
        observer_source_evidence = (
            root / f"observer-source-native-evidence-{platform}-{arch}.json"
        )
        native._write_source_evidence(
            observer_source_evidence,
            platform="windows" if platform == "win" else "linux",
        )
    generated = subprocess.run(
        native._generate_command(
            collector=collector,
            artifact=artifact,
            output=evidence,
            platform=platform,
            arch=arch,
            observer_source_evidence=observer_source_evidence,
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    if generated.returncode != 0:
        raise AssertionError(generated.stderr)
    posture = native._posture_report_path(evidence, platform, arch)
    return collector, artifact, evidence, posture


def _assemble_command(
    *,
    bundle: Path,
    collector: Path,
    artifact: Path,
    evidence: Path,
    posture: Path,
) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "assemble",
        "--bundle-dir",
        str(bundle),
        "--collector",
        str(collector),
        "--artifact",
        str(artifact),
        "--evidence",
        str(evidence),
        "--trust-posture-report",
        str(posture),
    ]


def _verify_command(bundle: Path) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "verify",
        "--bundle-dir",
        str(bundle),
    ]


def _assemble_fixture(
    root: Path,
    *,
    platform: str = "mac",
    arch: str = "x64",
    bundle_name: str = "bundle",
) -> tuple[Path, dict[str, object]]:
    source = root / f"source-{bundle_name}"
    source.mkdir()
    collector, artifact, evidence, posture = _write_target(
        source,
        platform=platform,
        arch=arch,
    )
    bundle = root / bundle_name
    assembled = subprocess.run(
        _assemble_command(
            bundle=bundle,
            collector=collector,
            artifact=artifact,
            evidence=evidence,
            posture=posture,
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    if assembled.returncode != 0:
        raise AssertionError(assembled.stderr)
    manifest = bundle / f"usagehub-release-handoff-{platform}-{arch}.json"
    return bundle, json.loads(manifest.read_text(encoding="utf-8"))


class ReleaseHandoffTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "Windows locks the open manifest")
    def test_verify_rejects_lstat_to_open_manifest_inode_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle, _payload = _assemble_fixture(root)
            manifest = bundle / "usagehub-release-handoff-mac-x64.json"
            replacement = root / "private-replacement-manifest.json"
            backup = root / "private-original-manifest-backup.json"
            replacement.write_bytes(manifest.read_bytes())
            original_open = handoff_module.os.open
            swapped = False

            def replace_for_manifest_open(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                *args: object,
                **kwargs: object,
            ):
                nonlocal swapped
                if Path(path) == manifest and not swapped:
                    swapped = True
                    os.replace(manifest, backup)
                    os.replace(replacement, manifest)
                    try:
                        descriptor = original_open(path, *args, **kwargs)
                    finally:
                        os.replace(manifest, replacement)
                        os.replace(backup, manifest)
                    return descriptor
                return original_open(path, *args, **kwargs)

            with mock.patch.object(
                handoff_module.os,
                "open",
                replace_for_manifest_open,
            ):
                with self.assertRaises(handoff_module.ReleaseHandoffError):
                    handoff_module.verify_release_handoff(bundle)
            self.assertTrue(swapped)

    def test_verify_rechecks_exact_names_during_final_bundle_rescan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle, payload = _assemble_fixture(root)
            collector = bundle / payload["files"]["collector"]["name"]
            unexpected = bundle / "unexpected-private-marker.bin"
            original_bundle_entries = handoff_module._bundle_entries
            scans = 0

            def rename_before_final_scan(path: Path):
                nonlocal scans
                scans += 1
                if scans == 2:
                    collector.rename(unexpected)
                return original_bundle_entries(path)

            with mock.patch.object(
                handoff_module,
                "_bundle_entries",
                rename_before_final_scan,
            ):
                with self.assertRaises(handoff_module.ReleaseHandoffError):
                    handoff_module.verify_release_handoff(bundle)
            self.assertEqual(scans, 2)

    def test_schema_is_closed_and_keeps_the_nonclaim(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(
            schema["$schema"],
            "https://json-schema.org/draft/2020-12/schema",
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["required"]),
            {
                "buildIdentity",
                "files",
                "object",
                "policy",
                "product",
                "provenanceAttestation",
                "releaseEligible",
                "schemaVersion",
                "source",
                "target",
            },
        )
        self.assertEqual(
            schema["properties"]["schemaVersion"],
            {"const": "release-handoff/v1", "type": "string"},
        )
        self.assertEqual(
            schema["properties"]["object"],
            {"const": "release.handoff_bundle", "type": "string"},
        )
        self.assertEqual(
            schema["properties"]["policy"],
            {
                "const": "release-handoff-local-nonclaim/v1",
                "type": "string",
            },
        )
        self.assertEqual(
            schema["properties"]["provenanceAttestation"],
            {"const": "not_verified", "type": "string"},
        )
        self.assertEqual(
            schema["properties"]["releaseEligible"],
            {"const": False, "type": "boolean"},
        )
        files = schema["properties"]["files"]
        self.assertFalse(files["additionalProperties"])
        self.assertEqual(
            set(files["required"]),
            {
                "collector",
                "distributionTrustPosture",
                "finalContainer",
                "nativeCiEvidence",
            },
        )

    def test_assemble_is_deterministic_path_free_and_fully_offline_verifiable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source-private-marker"
            source.mkdir()
            collector, artifact, evidence, posture = _write_target(
                source,
                platform="mac",
                arch="x64",
            )
            manifests: list[bytes] = []
            for index in range(2):
                bundle = root / f"bundle-{index}"
                assembled = subprocess.run(
                    _assemble_command(
                        bundle=bundle,
                        collector=collector,
                        artifact=artifact,
                        evidence=evidence,
                        posture=posture,
                    ),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(assembled.returncode, 0, assembled.stderr)
                self.assertEqual(
                    assembled.stdout,
                    "release_handoff_assembled mac/x64 files=5 "
                    "releaseEligible=false\n",
                )
                manifest = bundle / "usagehub-release-handoff-mac-x64.json"
                raw = manifest.read_text(encoding="utf-8")
                payload = json.loads(raw)
                self.assertEqual(
                    raw,
                    json.dumps(
                        payload,
                        allow_nan=False,
                        ensure_ascii=True,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                )
                self.assertNotIn(str(root), raw)
                self.assertNotIn("private-marker", raw)
                self.assertEqual(
                    set(path.name for path in bundle.iterdir()),
                    {
                        collector.name,
                        artifact.name,
                        evidence.name,
                        posture.name,
                        manifest.name,
                    },
                )
                self.assertEqual(payload["schemaVersion"], "release-handoff/v1")
                self.assertEqual(payload["object"], "release.handoff_bundle")
                self.assertEqual(
                    payload["policy"],
                    "release-handoff-local-nonclaim/v1",
                )
                self.assertEqual(
                    payload["product"],
                    {"name": "UsageHub", "version": "0.8.7"},
                )
                self.assertEqual(
                    payload["buildIdentity"],
                    {
                        "artifact": {
                            "name": "product-build-identity.v1.json",
                            "sha256": native.EXPECTED_BUILD_IDENTITY_SHA256,
                            "sizeBytes": native.EXPECTED_BUILD_IDENTITY_SIZE,
                        },
                        "identity": native.EXPECTED_IDENTITY,
                    },
                )
                self.assertEqual(
                    payload["source"],
                    {
                        "event": "workflow_dispatch",
                        "runAttempt": "2",
                        "runId": "12345",
                        "sha": "a" * 40,
                    },
                )
                self.assertEqual(
                    payload["target"],
                    {"arch": "x64", "platform": "mac"},
                )
                self.assertEqual(payload["provenanceAttestation"], "not_verified")
                self.assertIs(payload["releaseEligible"], False)
                for record in payload["files"].values():
                    bundled = bundle / record["name"]
                    self.assertEqual(record["sizeBytes"], bundled.stat().st_size)
                    self.assertEqual(
                        record["sha256"],
                        hashlib.sha256(bundled.read_bytes()).hexdigest(),
                    )
                verified = subprocess.run(
                    _verify_command(bundle),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(verified.returncode, 0, verified.stderr)
                self.assertEqual(
                    verified.stdout,
                    "release_handoff_verified mac/x64 files=5 "
                    "releaseEligible=false\n",
                )
                manifests.append(manifest.read_bytes())
            self.assertEqual(manifests[0], manifests[1])

    def test_assemble_and_verify_accept_representative_three_platform_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for platform, arch in (
                ("mac", "arm64"),
                ("win", "x64"),
                ("linux", "arm64"),
            ):
                with self.subTest(platform=platform, arch=arch):
                    bundle, payload = _assemble_fixture(
                        root,
                        platform=platform,
                        arch=arch,
                        bundle_name=f"bundle-{platform}-{arch}",
                    )
                    self.assertEqual(
                        payload["target"],
                        {"arch": arch, "platform": platform},
                    )
                    self.assertEqual(
                        {path.name for path in bundle.iterdir()},
                        {
                            record["name"]
                            for record in payload["files"].values()
                        }
                        | {
                            f"usagehub-release-handoff-{platform}-{arch}.json"
                        },
                    )
                    self.assertEqual(len(tuple(bundle.iterdir())), 5)
                    native_evidence = json.loads(
                        (
                            bundle
                            / payload["files"]["nativeCiEvidence"]["name"]
                        ).read_text(encoding="ascii")
                    )
                    if platform == "mac":
                        self.assertNotIn(
                            "observerSourceEvidence", native_evidence
                        )
                    else:
                        self.assertEqual(
                            native_evidence["observerSourceEvidence"]
                            ["platform"],
                            "windows" if platform == "win" else "linux",
                        )
                    verified = subprocess.run(
                        _verify_command(bundle),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_handoff_keeps_n_minus_one_windows_evidence_readable_without_a_sixth_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy-source"
            source.mkdir()
            collector, artifact, evidence, posture = _write_target(
                source,
                platform="win",
                arch="x64",
            )
            legacy = json.loads(evidence.read_text(encoding="ascii"))
            del legacy["observerSourceEvidence"]
            evidence.write_bytes(
                (
                    json.dumps(legacy, indent=2, sort_keys=True) + "\n"
                ).encode("ascii")
            )
            bundle = root / "legacy-bundle"

            assembled = subprocess.run(
                _assemble_command(
                    bundle=bundle,
                    collector=collector,
                    artifact=artifact,
                    evidence=evidence,
                    posture=posture,
                ),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(assembled.returncode, 0, assembled.stderr)
            self.assertEqual(len(tuple(bundle.iterdir())), 5)
            verified = subprocess.run(
                _verify_command(bundle),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            bundled_native = next(
                path
                for path in bundle.iterdir()
                if path.name.startswith("usagehub-native-evidence-")
            )
            bundled_payload = json.loads(
                bundled_native.read_text(encoding="ascii")
            )
            self.assertNotIn("observerSourceEvidence", bundled_payload)
            self.assertNotIn("supportedSourceCount", bundled_payload)
            self.assertNotIn("promotionEligible", bundled_payload)

    def test_verify_rejects_each_tampered_bound_file(self):
        roles = (
            "collector",
            "distributionTrustPosture",
            "finalContainer",
            "nativeCiEvidence",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for role in roles:
                with self.subTest(role=role):
                    bundle, payload = _assemble_fixture(
                        root,
                        bundle_name=f"bundle-{role}",
                    )
                    path = bundle / payload["files"][role]["name"]
                    path.write_bytes(path.read_bytes() + b"tampered")
                    verified = subprocess.run(
                        _verify_command(bundle),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(verified.returncode, 1)
                    self.assertRegex(
                        verified.stderr,
                        r"^release_handoff_invalid reason=[a-z_]+\n$",
                    )
                    self.assertNotIn(str(root), verified.stderr)
                    self.assertNotIn("Traceback", verified.stderr)

    def test_verify_rejects_build_identity_drift_from_native_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle, payload = _assemble_fixture(root)
            manifest = bundle / "usagehub-release-handoff-mac-x64.json"
            identity = payload["buildIdentity"]["identity"]
            identity["candidateBuild"] = "30"
            encoded_identity = (
                json.dumps(
                    identity,
                    allow_nan=False,
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("ascii")
            payload["buildIdentity"]["artifact"]["sha256"] = hashlib.sha256(
                encoded_identity
            ).hexdigest()
            payload["buildIdentity"]["artifact"]["sizeBytes"] = len(
                encoded_identity
            )
            manifest.write_bytes(
                (
                    json.dumps(
                        payload,
                        allow_nan=False,
                        ensure_ascii=True,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("ascii")
            )

            verified = subprocess.run(
                _verify_command(bundle),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(verified.returncode, 1)
            self.assertEqual(
                verified.stderr,
                "release_handoff_invalid reason=manifest_binding_invalid\n",
            )
            self.assertNotIn(str(root), verified.stderr)

    def test_verify_rejects_missing_extra_and_symlinked_bundle_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle, payload = _assemble_fixture(root, bundle_name="bundle-extra")
            (bundle / "extra.txt").write_text("extra", encoding="utf-8")
            self.assertEqual(
                subprocess.run(
                    _verify_command(bundle),
                    capture_output=True,
                    text=True,
                    check=False,
                ).returncode,
                1,
            )

            bundle, payload = _assemble_fixture(root, bundle_name="bundle-missing")
            (bundle / payload["files"]["collector"]["name"]).unlink()
            self.assertEqual(
                subprocess.run(
                    _verify_command(bundle),
                    capture_output=True,
                    text=True,
                    check=False,
                ).returncode,
                1,
            )

            if sys.platform != "win32":
                bundle, payload = _assemble_fixture(
                    root,
                    bundle_name="bundle-symlink",
                )
                collector = bundle / payload["files"]["collector"]["name"]
                target = root / "outside-private-marker"
                target.write_bytes(collector.read_bytes())
                collector.unlink()
                collector.symlink_to(target)
                result = subprocess.run(
                    _verify_command(bundle),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 1)
                self.assertNotIn(str(target), result.stderr)

    def test_assemble_rejects_symlink_input_and_existing_output_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            collector, artifact, evidence, posture = _write_target(
                source,
                platform="mac",
                arch="x64",
            )
            existing = root / "existing"
            existing.mkdir()
            marker = existing / "keep.txt"
            marker.write_text("user-owned", encoding="utf-8")
            result = subprocess.run(
                _assemble_command(
                    bundle=existing,
                    collector=collector,
                    artifact=artifact,
                    evidence=evidence,
                    posture=posture,
                ),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(marker.read_text(encoding="utf-8"), "user-owned")

            if sys.platform != "win32":
                link = root / "collector-link-private-marker"
                link.symlink_to(collector)
                output = root / "rejected"
                result = subprocess.run(
                    _assemble_command(
                        bundle=output,
                        collector=link,
                        artifact=artifact,
                        evidence=evidence,
                        posture=posture,
                    ),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 1)
                self.assertFalse(output.exists())
                self.assertNotIn(str(link), result.stderr)

    def test_verify_rejects_forged_duplicate_and_noncanonical_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for variant in ("forged", "duplicate", "noncanonical"):
                with self.subTest(variant=variant):
                    bundle, payload = _assemble_fixture(
                        root,
                        bundle_name=f"bundle-{variant}",
                    )
                    manifest = bundle / "usagehub-release-handoff-mac-x64.json"
                    if variant == "forged":
                        payload["releaseEligible"] = True
                        raw = (
                            json.dumps(
                                payload,
                                ensure_ascii=True,
                                indent=2,
                                sort_keys=True,
                            )
                            + "\n"
                        )
                    elif variant == "duplicate":
                        canonical = manifest.read_text(encoding="utf-8")
                        raw = canonical.replace(
                            '  "object": "release.handoff_bundle",',
                            '  "object": "release.handoff_bundle",\n'
                            '  "object": "release.handoff_bundle",',
                            1,
                        )
                    else:
                        raw = json.dumps(payload, sort_keys=True) + "\n"
                    manifest.write_bytes(raw.encode("ascii"))
                    result = subprocess.run(
                        _verify_command(bundle),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertRegex(
                        result.stderr,
                        r"^release_handoff_invalid reason=[a-z_]+\n$",
                    )
                    self.assertNotIn("Traceback", result.stderr)

    def test_cli_error_does_not_echo_private_bundle_path(self):
        with tempfile.TemporaryDirectory(
            prefix="release-handoff-private-marker-"
        ) as directory:
            bundle = Path(directory) / "missing-private-bundle"
            result = subprocess.run(
                _verify_command(bundle),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertRegex(
                result.stderr,
                r"^release_handoff_invalid reason=[a-z_]+\n$",
            )
            self.assertNotIn(str(bundle), result.stderr)
            self.assertNotIn("private-marker", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_workflow_builds_and_uploads_only_the_verified_handoff_directory(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("tests.test_release_handoff", source)
        self.assertIn('"scripts/release_handoff.py"', source)
        self.assertIn('"tests/test_release_handoff.py"', source)
        native_verify = source.index("- name: Verify native CI evidence")
        handoff = source.index("- name: Assemble and verify local release handoff")
        upload = source.index("- name: Upload artifact and evidence")
        self.assertLess(native_verify, handoff)
        self.assertLess(handoff, upload)
        handoff_step = source[handoff:upload]
        self.assertIn("scripts/release_handoff.py assemble", handoff_step)
        self.assertIn("scripts/release_handoff.py verify", handoff_step)
        self.assertIn(
            '--collector "./dist-collector/${{ matrix.collector }}"',
            handoff_step,
        )
        self.assertIn(
            '--artifact "${{ steps.audited_artifact.outputs.path }}"',
            handoff_step,
        )
        upload_step = source[upload : source.index("\n  gateway-performance:")]
        self.assertIn("${{ steps.release_handoff.outputs.path }}", upload_step)
        self.assertNotIn(
            "${{ steps.audited_artifact.outputs.path }}",
            upload_step,
        )
        self.assertNotIn("${{ steps.evidence.outputs.path }}", upload_step)
        self.assertNotIn(
            "${{ steps.distribution_trust_posture.outputs.path }}",
            upload_step,
        )
        self.assertNotIn("*", upload_step)


if __name__ == "__main__":
    unittest.main()
