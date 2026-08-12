import ast
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from fnmatch import fnmatchcase
from pathlib import Path
from unittest import mock

try:
    from jsonschema import Draft202012Validator
except ModuleNotFoundError:  # pragma: no cover - optional release oracle
    Draft202012Validator = None

from scripts import native_ci_evidence as evidence_module
from scripts import observer_source_native_evidence as source_evidence_module


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/native_ci_evidence.py"
WORKFLOW = ROOT / ".github/workflows/desktop-build.yml"
ACTION_PINS = ROOT / ".github/action-pins.json"
SCHEMA = ROOT / "docs/schemas/native-ci-evidence-v1.schema.json"
SOURCE_EVIDENCE_SCHEMA = (
    ROOT / "docs/schemas/observer-source-native-evidence-v1.schema.json"
)
BUILD_IDENTITY = ROOT / "openusage_bar/resources/artifact-build-identity.v1.json"
PRODUCT_TRUTH = ROOT / "openusage_bar/resources/product-version-truth.v1.json"
EXPECTED_BUILD_IDENTITY_SHA256 = (
    "73aafbd28ea8cc553a5dc14c3610c8455964c39eaaafe66dc468d6b37ee16c56"
)
EXPECTED_BUILD_IDENTITY_SIZE = 424
EXPECTED_IDENTITY = {
    "canaryClock": "not_started",
    "canaryQualifiedMachines": 0,
    "canaryTargetMachines": 5,
    "candidateBuild": "28",
    "candidateVersion": "0.8.6",
    "channel": "rc",
    "displayName": "UsageHub",
    "publicationStatus": "not_published",
    "publishedBaselineTag": "v0.7.1",
    "publishedBaselineVersion": "0.7.1",
    "releaseEligible": False,
    "releaseStage": "candidate",
    "schemaVersion": "artifact-build-identity/v1",
}

CHECK_OUTCOMES = {
    "actionPins": "success",
    "productVersionTruth": "success",
    "runnerArchitecture": "success",
    "pythonContracts": "success",
    "windowsNativeProcessTests": "skipped",
    "webDependencyAudit": "success",
    "webTests": "success",
    "webBuild": "success",
    "desktopDependencyAudit": "success",
    "desktopTests": "success",
    "collectorStatus": "success",
    "gatewaySelfTest": "success",
    "desktopRuntimeCapabilitySmoke": "success",
    "unpackedPackageAudit": "success",
    "distributionTrustPosture": "success",
    "packagedBuildIdentity": "success",
    "finalMacContainerAudit": "success",
    "finalWindowsContainerAudit": "skipped",
    "finalLinuxContainerAudit": "skipped",
}

RUNNERS = {
    ("mac", "x64"): ("macos-15-intel", "macOS", "X64"),
    ("mac", "arm64"): ("macos-15", "macOS", "ARM64"),
    ("win", "x64"): ("windows-2025", "Windows", "X64"),
    ("win", "arm64"): ("windows-11-arm", "Windows", "ARM64"),
    ("linux", "x64"): ("ubuntu-24.04", "Linux", "X64"),
    ("linux", "arm64"): ("ubuntu-24.04-arm", "Linux", "ARM64"),
}

TARGET_FILES = {
    ("mac", "x64"): ("openusage-collector", "UsageHub-0.8.6-mac-x64.dmg"),
    ("mac", "arm64"): ("openusage-collector", "UsageHub-0.8.6-mac-arm64.dmg"),
    ("win", "x64"): ("openusage-collector.exe", "UsageHub-0.8.6-win-x64.exe"),
    ("win", "arm64"): ("openusage-collector.exe", "UsageHub-0.8.6-win-arm64.exe"),
    ("linux", "x64"): (
        "openusage-collector",
        "UsageHub-0.8.6-linux-x86_64.AppImage",
    ),
    ("linux", "arm64"): (
        "openusage-collector",
        "UsageHub-0.8.6-linux-arm64.AppImage",
    ),
}


def _write_ascii(path: Path, content: str) -> None:
    path.write_bytes(content.encode("ascii"))


def _distribution_trust(platform: str) -> dict[str, object]:
    return {
        "platformCodeSigning": "not_verified",
        "platformNotarization": (
            "not_verified" if platform == "mac" else "not_applicable"
        ),
        "policy": "native-distribution-trust-nonclaim/v1",
        "provenanceAttestation": "not_verified",
        "releaseEligible": False,
    }


def _write_macos_x64_collector(path: Path) -> None:
    _write_macho(path, arch="x64")


def _write_macho(path: Path, *, arch: str) -> None:
    path.write_bytes(
        struct.pack(
            "<IiiIIIII",
            0xFEEDFACF,
            0x01000007 if arch == "x64" else 0x0100000C,
            3,
            2,
            0,
            0,
            0,
            0,
        )
        + b"collector-payload"
    )


def _write_dmg(path: Path) -> None:
    path.write_bytes(b"dmg-payload" + b"\0" * 501 + b"koly" + b"\0" * 508)


def _write_pe(path: Path, *, arch: str, pe_plus: bool) -> None:
    payload = bytearray(512)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 60, 128)
    payload[128:132] = b"PE\0\0"
    struct.pack_into("<H", payload, 132, 0x8664 if arch == "x64" else 0xAA64)
    struct.pack_into("<H", payload, 152, 0x20B if pe_plus else 0x10B)
    path.write_bytes(payload)


def _write_elf(path: Path, *, arch: str) -> None:
    payload = bytearray(64)
    payload[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<H", payload, 18, 62 if arch == "x64" else 183)
    path.write_bytes(payload)


def _write_native_row_files(
    collector: Path,
    artifact: Path,
    *,
    platform: str,
    arch: str,
) -> None:
    if platform == "mac":
        _write_macho(collector, arch=arch)
        _write_dmg(artifact)
    elif platform == "win":
        _write_pe(collector, arch=arch, pe_plus=True)
        _write_pe(artifact, arch=arch, pe_plus=True)
    else:
        _write_elf(collector, arch=arch)
        _write_elf(artifact, arch=arch)


def _check_outcomes(platform: str) -> dict[str, str]:
    outcomes = dict(CHECK_OUTCOMES)
    outcomes["windowsNativeProcessTests"] = (
        "success" if platform == "win" else "skipped"
    )
    outcomes["finalMacContainerAudit"] = (
        "success" if platform == "mac" else "skipped"
    )
    outcomes["finalWindowsContainerAudit"] = (
        "success" if platform == "win" else "skipped"
    )
    outcomes["finalLinuxContainerAudit"] = (
        "success" if platform == "linux" else "skipped"
    )
    return outcomes


def _posture_report_path(output: Path, platform: str, arch: str) -> Path:
    return output.parent / f"usagehub-distribution-trust-{platform}-{arch}.json"


def _write_posture_report(
    path: Path,
    *,
    platform: str,
    artifact: Path,
) -> None:
    payload = artifact.read_bytes()
    signing = {
        "mac": "unsigned",
        "win": "unsigned",
        "linux": "not_applicable",
    }[platform]
    notarization = "not_stapled" if platform == "mac" else "not_applicable"
    report = {
        "artifact": {
            "name": artifact.name,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "sizeBytes": len(payload),
        },
        "object": "distribution.trust_posture",
        "platformCodeSigning": signing,
        "platformNotarization": notarization,
        "policy": "distribution-trust-posture/v1",
        "provenanceAttestation": "not_verified",
        "releaseEligible": False,
        "schemaVersion": "distribution-trust-posture/v1",
        "targetPlatform": platform,
    }
    path.write_bytes(
        (
            json.dumps(
                report,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
    )


def _write_source_evidence(
    path: Path,
    *,
    platform: str,
    moonshot_verified: bool = True,
    codex_verified: bool = True,
) -> dict[str, object]:
    payload = source_evidence_module._payload(
        platform,
        moonshot_verified=moonshot_verified,
        codex_verified=codex_verified,
    )
    path.write_bytes(
        source_evidence_module.canonical_evidence_json(payload).encode("ascii")
    )
    return payload


def _write_lifecycle_evidence(
    path: Path,
    *,
    artifact: Path,
    platform: str,
    source_commit: str = "a" * 40,
) -> dict[str, object]:
    artifact_bytes = artifact.read_bytes()
    payload: dict[str, object] = {
        "schemaVersion": "native-lifecycle-evidence/v1",
        "object": "native.lifecycle",
        "synthetic": False,
        "releaseEligible": False,
        "observedAt": "2026-08-11T01:02:03.000000Z",
        "sourceCommit": source_commit,
        "target": {
            "platform": platform,
            "arch": "x64",
            "serviceManager": (
                "task_scheduler" if platform == "win" else "systemd_user"
            ),
        },
        "artifact": {
            "name": artifact.name,
            "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            "sizeBytes": len(artifact_bytes),
        },
        "checks": {
            "install": "passed",
            "firstRun": "passed",
            "observeDefault": "passed",
            "serviceRegistered": "passed",
            "uninstallPreserve": "passed",
            "serviceRemoved": "passed",
            "reinstall": "passed",
            "uninstallDelete": "passed",
            "finalStateRemoved": "passed",
        },
        "gateway": {
            "defaultMode": "observe",
            "listenerActive": False,
            "cacheCreated": False,
            "telemetryCreated": False,
        },
        "privacy": {
            "providerCredentialReads": 0,
            "providerNetworkCalls": 0,
        },
        "persistence": {
            "ledgerOnPreserve": "preserved",
            "credentialsOnPreserve": "not_created",
            "gatewayCacheOnPreserve": "not_created",
            "gatewayTelemetryOnPreserve": "not_created",
            "stateAfterDelete": "removed",
        },
    }
    path.write_bytes(
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
    return payload


def _generate_command(
    *,
    collector: Path,
    artifact: Path,
    output: Path,
    platform: str = "mac",
    arch: str = "x64",
    build_identity: Path = BUILD_IDENTITY,
    product_truth: Path = PRODUCT_TRUTH,
    observer_source_evidence: Path | None = None,
    native_lifecycle_evidence: Path | None = None,
) -> list[str]:
    posture_report = _posture_report_path(output, platform, arch)
    _write_posture_report(
        posture_report,
        platform=platform,
        artifact=artifact,
    )
    runner_label, runner_os, runner_arch = RUNNERS[(platform, arch)]
    command = [
        sys.executable,
        str(SCRIPT),
        "generate",
        "--platform",
        platform,
        "--arch",
        arch,
        "--runner-label",
        runner_label,
        "--runner-environment",
        "github-hosted",
        "--runner-os",
        runner_os,
        "--runner-arch",
        runner_arch,
        "--collector",
        str(collector),
        "--artifact",
        str(artifact),
        "--trust-posture-report",
        str(posture_report),
        "--build-identity",
        str(build_identity),
        "--product-truth",
        str(product_truth),
        "--version",
        "0.8.6",
        "--source-sha",
        "a" * 40,
        "--source-event",
        "workflow_dispatch",
        "--run-id",
        "12345",
        "--run-attempt",
        "2",
        "--python-version",
        "3.13.7",
        "--node-version",
        "24.16.0",
        "--pyinstaller-version",
        "6.22.0",
        "--electron-builder-version",
        "26.15.3",
        "--output",
        str(output),
    ]
    for name, outcome in _check_outcomes(platform).items():
        command.extend(("--check", f"{name}={outcome}"))
    if observer_source_evidence is not None:
        command.extend(
            ("--observer-source-evidence", str(observer_source_evidence))
        )
    if native_lifecycle_evidence is not None:
        command.extend(
            ("--native-lifecycle-evidence", str(native_lifecycle_evidence))
        )
    return command


def _verify_command(
    *,
    evidence: Path,
    collector: Path,
    artifact: Path,
    platform: str = "mac",
    arch: str = "x64",
) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "verify",
        "--evidence",
        str(evidence),
        "--collector",
        str(collector),
        "--artifact",
        str(artifact),
        "--trust-posture-report",
        str(_posture_report_path(evidence, platform, arch)),
    ]


class NativeCiEvidenceTests(unittest.TestCase):
    def test_cli_rejects_private_invalid_arguments_without_echoing_them(self):
        private_marker = "/tmp/native-ci-private-marker-secret"
        commands = (
            [
                sys.executable,
                str(SCRIPT),
                "generate",
                "--platform",
                private_marker,
            ],
            [
                sys.executable,
                str(SCRIPT),
                "verify",
                "--unexpected",
                private_marker,
            ],
        )
        for command in commands:
            with self.subTest(command=command[2]):
                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(
                    result.stderr,
                    "native_ci_evidence_invalid reason=arguments_invalid\n",
                )
                self.assertNotIn(private_marker, result.stderr)
                self.assertNotIn("usage:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_payload_rejects_lstat_to_read_same_path_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector = root / "openusage-collector"
            replacement = root / "private-replacement-collector"
            artifact = root / "UsageHub-0.8.6-mac-x64.dmg"
            evidence = root / "usagehub-native-evidence-mac-x64.json"
            invalid_collector = b"invalid-original-collector"
            collector.write_bytes(invalid_collector)
            _write_macho(replacement, arch="x64")
            _write_dmg(artifact)
            parsed = evidence_module._parser().parse_args(
                _generate_command(
                    collector=collector,
                    artifact=artifact,
                    output=evidence,
                )[2:]
            )

            original_open = evidence_module.os.open
            swapped = False

            def replace_for_first_read(
                path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                *args: object,
                **kwargs: object,
            ):
                nonlocal swapped
                if Path(path) == collector and not swapped:
                    swapped = True
                    collector.unlink()
                    collector.symlink_to(replacement)
                    descriptor = original_open(path, *args, **kwargs)
                    collector.unlink()
                    restored = original_open(
                        collector,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                    try:
                        os.write(restored, invalid_collector)
                    finally:
                        os.close(restored)
                    return descriptor
                return original_open(path, *args, **kwargs)

            with mock.patch.object(
                evidence_module.os,
                "open",
                replace_for_first_read,
            ):
                with self.assertRaises(evidence_module.EvidenceError):
                    evidence_module._payload(parsed)
            self.assertTrue(swapped)

    def test_documented_schema_keeps_the_generator_allowlist_and_pinned_toolchain(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["schemaVersion"], {"const": 1, "type": "integer"})
        self.assertIn("buildIdentity", schema["required"])
        self.assertIn("distributionTrust", schema["required"])
        trust = schema["properties"]["distributionTrust"]
        self.assertFalse(trust["additionalProperties"])
        self.assertEqual(
            set(trust["required"]),
            {
                "platformCodeSigning",
                "platformNotarization",
                "policy",
                "provenanceAttestation",
                "releaseEligible",
            },
        )
        self.assertEqual(
            trust["properties"]["platformCodeSigning"],
            {"const": "not_verified", "type": "string"},
        )
        self.assertEqual(
            trust["properties"]["platformNotarization"],
            {
                "enum": ["not_applicable", "not_verified"],
                "type": "string",
            },
        )
        self.assertEqual(
            trust["properties"]["provenanceAttestation"],
            {"const": "not_verified", "type": "string"},
        )
        self.assertEqual(
            trust["properties"]["releaseEligible"],
            {"const": False, "type": "boolean"},
        )
        self.assertEqual(
            trust["properties"]["policy"],
            {
                "const": "native-distribution-trust-nonclaim/v1",
                "type": "string",
            },
        )
        self.assertEqual(
            set(schema["properties"]["checks"]["required"]),
            set(CHECK_OUTCOMES),
        )
        self.assertEqual(
            set(schema["properties"]["artifacts"]["required"]),
            {"collector", "distributionTrustPosture", "finalContainer"},
        )
        toolchain = schema["properties"]["toolchain"]["properties"]
        self.assertEqual(toolchain["node"]["const"], "24.16.0")
        self.assertEqual(toolchain["pyInstaller"]["const"], "6.22.0")
        self.assertEqual(toolchain["electronBuilder"]["const"], "26.15.3")
        self.assertEqual(
            set(schema["properties"]["privacy"]["required"]),
            {
                "finalExtractedPackageAudit",
                "finalExtractedPackageAuditPolicy",
                "frozenSmokeRealCredentialReads",
                "frozenSmokeRealProviderNetworkCalls",
                "policy",
            },
        )
        self.assertEqual(
            [entry["$ref"] for entry in schema["allOf"][0]["oneOf"]],
            [
                "#/$defs/macX64Row",
                "#/$defs/macArm64Row",
                "#/$defs/winX64Row",
                "#/$defs/winArm64Row",
                "#/$defs/linuxX64Row",
                "#/$defs/linuxArm64Row",
            ],
        )
        source_schema = json.loads(
            SOURCE_EVIDENCE_SCHEMA.read_text(encoding="utf-8")
        )
        observer_source = schema["properties"]["observerSourceEvidence"]
        self.assertEqual(observer_source["type"], "object")
        self.assertFalse(observer_source["additionalProperties"])
        self.assertEqual(
            set(observer_source["required"]),
            set(source_schema["required"]),
        )
        self.assertEqual(
            observer_source["properties"]["schemaVersion"],
            source_schema["properties"]["schemaVersion"],
        )
        self.assertNotIn("observerSourceEvidence", schema["required"])
        self.assertEqual(
            schema["$defs"]["macRow"]["not"],
            {
                "anyOf": [
                    {"required": ["nativeLifecycleEvidence"]},
                    {"required": ["observerSourceEvidence"]},
                ]
            },
        )
        for row_name, source_platform in (
            ("winRow", "windows"),
            ("linuxRow", "linux"),
        ):
            with self.subTest(row=row_name):
                self.assertEqual(
                    schema["$defs"][row_name]["properties"]
                    ["observerSourceEvidence"]["properties"]["platform"],
                    {"const": source_platform},
                )

    @unittest.skipIf(
        Draft202012Validator is None,
        "jsonschema is unavailable for native evidence schema validation",
    )
    def test_schema_accepts_legacy_and_current_rows_but_rejects_crossed_source_shapes(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for platform, source_platform in (
                ("mac", None),
                ("win", "windows"),
                ("linux", "linux"),
            ):
                with self.subTest(platform=platform):
                    row = root / platform
                    row.mkdir()
                    collector_name, artifact_name = TARGET_FILES[
                        (platform, "x64")
                    ]
                    collector = row / collector_name
                    artifact = row / artifact_name
                    evidence = row / "native.json"
                    _write_native_row_files(
                        collector,
                        artifact,
                        platform=platform,
                        arch="x64",
                    )
                    source_evidence = None
                    source_payload = None
                    lifecycle_evidence = None
                    if source_platform is not None:
                        source_evidence = row / "observer-source.json"
                        source_payload = _write_source_evidence(
                            source_evidence,
                            platform=source_platform,
                        )
                        lifecycle_evidence = row / "native-lifecycle.json"
                        _write_lifecycle_evidence(
                            lifecycle_evidence,
                            artifact=artifact,
                            platform=platform,
                        )
                    generated = subprocess.run(
                        _generate_command(
                            collector=collector,
                            artifact=artifact,
                            output=evidence,
                            platform=platform,
                            observer_source_evidence=source_evidence,
                            native_lifecycle_evidence=lifecycle_evidence,
                        ),
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(
                        generated.returncode, 0, generated.stderr
                    )
                    payload = json.loads(evidence.read_text(encoding="ascii"))
                    self.assertTrue(validator.is_valid(payload))

                    if source_platform is None:
                        crossed = json.loads(json.dumps(payload))
                        crossed["observerSourceEvidence"] = (
                            source_evidence_module._payload(
                                "windows",
                                moonshot_verified=True,
                                codex_verified=True,
                            )
                        )
                        self.assertFalse(validator.is_valid(crossed))
                        continue

                    legacy = json.loads(json.dumps(payload))
                    del legacy["observerSourceEvidence"]
                    del legacy["nativeLifecycleEvidence"]
                    self.assertTrue(validator.is_valid(legacy))

                    swapped = json.loads(json.dumps(payload))
                    swapped["observerSourceEvidence"]["platform"] = (
                        "linux" if source_platform == "windows" else "windows"
                    )
                    self.assertFalse(validator.is_valid(swapped))

                    private = json.loads(json.dumps(payload))
                    private["observerSourceEvidence"]["homePath"] = (
                        "/Users/private-schema-marker"
                    )
                    self.assertFalse(validator.is_valid(private))
                    self.assertEqual(
                        payload["observerSourceEvidence"], source_payload
                    )

    def test_generate_embeds_exact_platform_bound_source_evidence_on_windows_and_linux(self):
        for platform, source_platform, moonshot_verified in (
            ("win", "windows", True),
            ("linux", "linux", False),
        ):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                collector_name, artifact_name = TARGET_FILES[(platform, "x64")]
                collector = root / collector_name
                artifact = root / artifact_name
                evidence = root / f"native-{platform}.json"
                source_evidence = root / f"observer-source-{platform}.json"
                _write_native_row_files(
                    collector,
                    artifact,
                    platform=platform,
                    arch="x64",
                )
                expected_source = _write_source_evidence(
                    source_evidence,
                    platform=source_platform,
                    moonshot_verified=moonshot_verified,
                )

                generated = subprocess.run(
                    _generate_command(
                        collector=collector,
                        artifact=artifact,
                        output=evidence,
                        platform=platform,
                        observer_source_evidence=source_evidence,
                    ),
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(generated.returncode, 0, generated.stderr)
                raw = evidence.read_text(encoding="ascii")
                payload = json.loads(raw)
                self.assertEqual(
                    payload["observerSourceEvidence"],
                    expected_source,
                )
                self.assertEqual(
                    raw,
                    json.dumps(payload, indent=2, sort_keys=True) + "\n",
                )
                self.assertNotIn(str(root), raw)
                self.assertNotIn("supportedSourceCount", payload)
                self.assertNotIn("promotionEligible", payload)

                verified = subprocess.run(
                    _verify_command(
                        evidence=evidence,
                        collector=collector,
                        artifact=artifact,
                        platform=platform,
                    ),
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_generate_embeds_and_verifies_exact_x64_native_lifecycle_evidence(self):
        for platform, source_platform in (
            ("win", "windows"),
            ("linux", "linux"),
        ):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                collector_name, artifact_name = TARGET_FILES[(platform, "x64")]
                collector = root / collector_name
                artifact = root / artifact_name
                evidence = root / f"native-{platform}.json"
                source_evidence = root / f"observer-source-{platform}.json"
                lifecycle_evidence = root / f"native-lifecycle-{platform}.json"
                _write_native_row_files(
                    collector,
                    artifact,
                    platform=platform,
                    arch="x64",
                )
                _write_source_evidence(
                    source_evidence,
                    platform=source_platform,
                )
                expected_lifecycle = _write_lifecycle_evidence(
                    lifecycle_evidence,
                    artifact=artifact,
                    platform=platform,
                )

                generated = subprocess.run(
                    _generate_command(
                        collector=collector,
                        artifact=artifact,
                        output=evidence,
                        platform=platform,
                        observer_source_evidence=source_evidence,
                        native_lifecycle_evidence=lifecycle_evidence,
                    ),
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(generated.returncode, 0, generated.stderr)
                raw = evidence.read_text(encoding="ascii")
                payload = json.loads(raw)
                self.assertEqual(
                    payload["nativeLifecycleEvidence"],
                    expected_lifecycle,
                )
                self.assertNotIn(str(root), raw)

                verified = subprocess.run(
                    _verify_command(
                        evidence=evidence,
                        collector=collector,
                        artifact=artifact,
                        platform=platform,
                    ),
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_generate_rejects_hostile_or_crossed_native_lifecycle_evidence(self):
        private_canary = "PRIVATE_NATIVE_LIFECYCLE_CANARY"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector = root / "openusage-collector.exe"
            artifact = root / "UsageHub-0.8.6-win-x64.exe"
            source_evidence = root / "observer-source.json"
            lifecycle_evidence = root / "native-lifecycle.json"
            _write_native_row_files(
                collector,
                artifact,
                platform="win",
                arch="x64",
            )
            _write_source_evidence(source_evidence, platform="windows")
            valid = _write_lifecycle_evidence(
                lifecycle_evidence,
                artifact=artifact,
                platform="win",
            )

            mutations = {
                "additive_private": lambda payload: payload.__setitem__(
                    "privatePath", private_canary
                ),
                "source_swap": lambda payload: payload.__setitem__(
                    "sourceCommit", "c" * 40
                ),
                "artifact_swap": lambda payload: payload["artifact"].__setitem__(
                    "sha256", "d" * 64
                ),
                "platform_swap": lambda payload: payload.__setitem__(
                    "target",
                    {
                        "platform": "linux",
                        "arch": "x64",
                        "serviceManager": "systemd_user",
                    },
                ),
            }
            for label, mutate in mutations.items():
                with self.subTest(label=label):
                    payload = json.loads(json.dumps(valid))
                    mutate(payload)
                    lifecycle_evidence.write_bytes(
                        (
                            json.dumps(payload, indent=2, sort_keys=True) + "\n"
                        ).encode("ascii")
                    )
                    evidence = root / f"native-{label}.json"
                    generated = subprocess.run(
                        _generate_command(
                            collector=collector,
                            artifact=artifact,
                            output=evidence,
                            platform="win",
                            observer_source_evidence=source_evidence,
                            native_lifecycle_evidence=lifecycle_evidence,
                        ),
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                        check=False,
                    )

                    self.assertEqual(generated.returncode, 1)
                    self.assertEqual(generated.stdout, "")
                    self.assertEqual(
                        generated.stderr,
                        "native_ci_evidence_invalid "
                        "reason=native_lifecycle_evidence_invalid\n",
                    )
                    self.assertNotIn(private_canary, generated.stderr)
                    self.assertNotIn(str(root), generated.stderr)
                    self.assertFalse(evidence.exists())

    def test_native_lifecycle_evidence_is_not_applicable_to_arm64_rows(self):
        for platform, source_platform in (
            ("win", "windows"),
            ("linux", "linux"),
        ):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                collector_name, artifact_name = TARGET_FILES[(platform, "arm64")]
                collector = root / collector_name
                artifact = root / artifact_name
                evidence = root / f"native-{platform}-arm64.json"
                source_evidence = root / "observer-source.json"
                lifecycle_artifact_name = TARGET_FILES[(platform, "x64")][1]
                lifecycle_artifact = root / lifecycle_artifact_name
                lifecycle_evidence = root / "native-lifecycle.json"
                _write_native_row_files(
                    collector,
                    artifact,
                    platform=platform,
                    arch="arm64",
                )
                _write_native_row_files(
                    root / ("lifecycle-collector.exe" if platform == "win" else "lifecycle-collector"),
                    lifecycle_artifact,
                    platform=platform,
                    arch="x64",
                )
                _write_source_evidence(
                    source_evidence,
                    platform=source_platform,
                )
                _write_lifecycle_evidence(
                    lifecycle_evidence,
                    artifact=lifecycle_artifact,
                    platform=platform,
                )

                generated = subprocess.run(
                    _generate_command(
                        collector=collector,
                        artifact=artifact,
                        output=evidence,
                        platform=platform,
                        arch="arm64",
                        observer_source_evidence=source_evidence,
                        native_lifecycle_evidence=lifecycle_evidence,
                    ),
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(generated.returncode, 1)
                self.assertEqual(generated.stdout, "")
                self.assertEqual(
                    generated.stderr,
                    "native_ci_evidence_invalid "
                    "reason=native_lifecycle_evidence_not_applicable\n",
                )
                self.assertFalse(evidence.exists())

    def test_generate_requires_source_evidence_for_new_windows_and_linux_rows(self):
        for platform in ("win", "linux"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                collector_name, artifact_name = TARGET_FILES[(platform, "x64")]
                collector = root / collector_name
                artifact = root / artifact_name
                evidence = root / f"native-{platform}.json"
                _write_native_row_files(
                    collector,
                    artifact,
                    platform=platform,
                    arch="x64",
                )

                generated = subprocess.run(
                    _generate_command(
                        collector=collector,
                        artifact=artifact,
                        output=evidence,
                        platform=platform,
                    ),
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(generated.returncode, 1)
                self.assertEqual(generated.stdout, "")
                self.assertEqual(
                    generated.stderr,
                    "native_ci_evidence_invalid "
                    "reason=observer_source_evidence_required\n",
                )
                self.assertFalse(evidence.exists())

    def test_macos_keeps_the_n_minus_one_shape_and_rejects_source_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector = root / "openusage-collector"
            artifact = root / "UsageHub-0.8.6-mac-x64.dmg"
            evidence = root / "native-mac.json"
            source_evidence = root / "observer-source.json"
            _write_native_row_files(
                collector,
                artifact,
                platform="mac",
                arch="x64",
            )
            _write_source_evidence(source_evidence, platform="windows")

            generated = subprocess.run(
                _generate_command(
                    collector=collector,
                    artifact=artifact,
                    output=evidence,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            self.assertEqual(payload["schemaVersion"], 1)
            self.assertNotIn("observerSourceEvidence", payload)
            verified = subprocess.run(
                _verify_command(
                    evidence=evidence,
                    collector=collector,
                    artifact=artifact,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)

            forbidden_output = root / "native-mac-with-source.json"
            forbidden = subprocess.run(
                _generate_command(
                    collector=collector,
                    artifact=artifact,
                    output=forbidden_output,
                    observer_source_evidence=source_evidence,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(forbidden.returncode, 1)
            self.assertEqual(
                forbidden.stderr,
                "native_ci_evidence_invalid "
                "reason=observer_source_evidence_not_applicable\n",
            )
            self.assertFalse(forbidden_output.exists())

    def test_generate_rejects_noncanonical_private_and_platform_swapped_source_evidence(self):
        private_marker = "/Users/private-observer-source-marker"
        cases: list[tuple[str, str, str]] = []
        valid_windows = source_evidence_module._payload(
            "windows",
            moonshot_verified=True,
            codex_verified=True,
        )
        noncanonical = json.dumps(valid_windows, sort_keys=True) + "\n"
        private = dict(valid_windows)
        private["homePath"] = private_marker
        private_json = json.dumps(private, indent=2, sort_keys=True) + "\n"
        swapped = source_evidence_module.canonical_evidence_json(
            source_evidence_module._payload(
                "linux",
                moonshot_verified=True,
                codex_verified=True,
            )
        )
        cases.extend(
            (
                ("noncanonical", noncanonical, "observer_source_evidence_invalid"),
                ("private", private_json, "observer_source_evidence_invalid"),
                (
                    "platform_swapped",
                    swapped,
                    "observer_source_evidence_platform_invalid",
                ),
            )
        )

        for name, source_raw, reason in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                collector = root / "openusage-collector.exe"
                artifact = root / "UsageHub-0.8.6-win-x64.exe"
                evidence = root / "native-win.json"
                source_evidence = root / "observer-source.json"
                _write_native_row_files(
                    collector,
                    artifact,
                    platform="win",
                    arch="x64",
                )
                _write_ascii(source_evidence, source_raw)

                generated = subprocess.run(
                    _generate_command(
                        collector=collector,
                        artifact=artifact,
                        output=evidence,
                        platform="win",
                        observer_source_evidence=source_evidence,
                    ),
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(generated.returncode, 1)
                self.assertEqual(
                    generated.stderr,
                    f"native_ci_evidence_invalid reason={reason}\n",
                )
                self.assertNotIn(private_marker, generated.stderr)
                self.assertNotIn(str(root), generated.stderr)
                self.assertFalse(evidence.exists())

    def test_verify_accepts_legacy_windows_linux_rows_without_promoting_them(self):
        for platform, source_platform in (("win", "windows"), ("linux", "linux")):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                collector_name, artifact_name = TARGET_FILES[(platform, "x64")]
                collector = root / collector_name
                artifact = root / artifact_name
                evidence = root / f"native-{platform}.json"
                source_evidence = root / "observer-source.json"
                _write_native_row_files(
                    collector,
                    artifact,
                    platform=platform,
                    arch="x64",
                )
                _write_source_evidence(
                    source_evidence,
                    platform=source_platform,
                )
                generated = subprocess.run(
                    _generate_command(
                        collector=collector,
                        artifact=artifact,
                        output=evidence,
                        platform=platform,
                        observer_source_evidence=source_evidence,
                    ),
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(generated.returncode, 0, generated.stderr)
                legacy = json.loads(evidence.read_text(encoding="utf-8"))
                del legacy["observerSourceEvidence"]
                _write_ascii(
                    evidence,
                    json.dumps(legacy, indent=2, sort_keys=True) + "\n",
                )

                verified = subprocess.run(
                    _verify_command(
                        evidence=evidence,
                        collector=collector,
                        artifact=artifact,
                        platform=platform,
                    ),
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(verified.returncode, 0, verified.stderr)
                self.assertEqual(
                    verified.stdout,
                    f"native_ci_evidence_ok {platform}/x64\n",
                )
                loaded = evidence_module.verify_native_ci_evidence(
                    evidence=evidence,
                    collector=collector,
                    artifact=artifact,
                    trust_posture_report=_posture_report_path(
                        evidence, platform, "x64"
                    ),
                )
                self.assertNotIn("observerSourceEvidence", loaded)
                self.assertNotIn("supportedSourceCount", loaded)
                self.assertNotIn("promotionEligible", loaded)

    def test_verify_rejects_forged_private_swapped_or_noncanonical_embedded_source_evidence(self):
        private_marker = "/Users/private-observer-source-marker"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector = root / "openusage-collector.exe"
            artifact = root / "UsageHub-0.8.6-win-x64.exe"
            evidence = root / "native-win.json"
            source_evidence = root / "observer-source.json"
            _write_native_row_files(
                collector,
                artifact,
                platform="win",
                arch="x64",
            )
            _write_source_evidence(source_evidence, platform="windows")
            generated = subprocess.run(
                _generate_command(
                    collector=collector,
                    artifact=artifact,
                    output=evidence,
                    platform="win",
                    observer_source_evidence=source_evidence,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            original = json.loads(evidence.read_text(encoding="ascii"))

            hostile: list[tuple[str, dict[str, object], str]] = []
            forged = json.loads(json.dumps(original))
            forged["observerSourceEvidence"]["verifiedSourceCount"] = 1
            hostile.append(
                ("forged_count", forged, "observer_source_evidence_invalid")
            )
            private = json.loads(json.dumps(original))
            private["observerSourceEvidence"]["homePath"] = private_marker
            hostile.append(
                ("private_field", private, "observer_source_evidence_invalid")
            )
            swapped = json.loads(json.dumps(original))
            swapped["observerSourceEvidence"]["platform"] = "linux"
            hostile.append(
                (
                    "platform_swapped",
                    swapped,
                    "observer_source_evidence_platform_invalid",
                )
            )

            for name, payload, reason in hostile:
                with self.subTest(case=name):
                    _write_ascii(
                        evidence,
                        json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    )
                    verified = subprocess.run(
                        _verify_command(
                            evidence=evidence,
                            collector=collector,
                            artifact=artifact,
                            platform="win",
                        ),
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(verified.returncode, 1)
                    self.assertEqual(
                        verified.stderr,
                        f"native_ci_evidence_invalid reason={reason}\n",
                    )
                    self.assertNotIn(private_marker, verified.stderr)
                    self.assertNotIn(str(root), verified.stderr)

            _write_ascii(
                evidence,
                json.dumps(original, separators=(",", ":"), sort_keys=True)
                + "\n",
            )
            noncanonical = subprocess.run(
                _verify_command(
                    evidence=evidence,
                    collector=collector,
                    artifact=artifact,
                    platform="win",
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(noncanonical.returncode, 1)
            self.assertEqual(
                noncanonical.stderr,
                "native_ci_evidence_invalid reason=evidence_not_canonical\n",
            )

    def test_generate_writes_canonical_path_free_evidence_and_verify_accepts_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector = root / "openusage-collector"
            artifact = root / "UsageHub-0.8.6-mac-x64.dmg"
            evidence = root / "usagehub-native-evidence-mac-x64.json"
            _write_macos_x64_collector(collector)
            _write_dmg(artifact)

            generated = subprocess.run(
                _generate_command(
                    collector=collector,
                    artifact=artifact,
                    output=evidence,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(generated.returncode, 0, generated.stderr)
            raw = evidence.read_text(encoding="utf-8")
            payload = json.loads(raw)
            self.assertEqual(
                raw,
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
            )
            self.assertEqual(payload["schemaVersion"], 1)
            self.assertEqual(
                payload["buildIdentity"],
                {
                    "artifact": {
                        "name": "product-build-identity.v1.json",
                        "sha256": EXPECTED_BUILD_IDENTITY_SHA256,
                        "sizeBytes": EXPECTED_BUILD_IDENTITY_SIZE,
                    },
                    "identity": EXPECTED_IDENTITY,
                },
            )
            self.assertEqual(
                payload["target"],
                {
                    "arch": "x64",
                    "builderArch": "x64",
                    "collectorMember": "collector/openusage-collector",
                    "containerFormat": "dmg",
                    "platform": "mac",
                },
            )
            self.assertEqual(payload["artifacts"]["collector"]["format"], "macho64")
            self.assertEqual(payload["artifacts"]["collector"]["arch"], "x64")
            self.assertEqual(payload["distributionTrust"], _distribution_trust("mac"))
            self.assertEqual(
                payload["privacy"],
                {
                    "finalExtractedPackageAudit": "passed",
                    "finalExtractedPackageAuditPolicy": "release-artifact-audit/v1",
                    "frozenSmokeRealCredentialReads": 0,
                    "frozenSmokeRealProviderNetworkCalls": 0,
                    "policy": "native-ci-evidence-allowlist/v1",
                },
            )
            self.assertNotIn(str(root), raw)
            self.assertNotIn("/Users/", raw)

            verified = subprocess.run(
                _verify_command(
                    evidence=evidence,
                    collector=collector,
                    artifact=artifact,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertEqual(verified.stdout, "native_ci_evidence_ok mac/x64\n")

    def test_generate_rejects_noncanonical_build_identity_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector = root / "openusage-collector"
            artifact = root / "UsageHub-0.8.6-mac-x64.dmg"
            evidence = root / "usagehub-native-evidence-mac-x64.json"
            tampered_identity = root / "artifact-build-identity.v1.json"
            _write_macos_x64_collector(collector)
            _write_dmg(artifact)
            tampered_identity.write_bytes(BUILD_IDENTITY.read_bytes() + b" ")

            generated = subprocess.run(
                _generate_command(
                    collector=collector,
                    artifact=artifact,
                    output=evidence,
                    build_identity=tampered_identity,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(generated.returncode, 1)
            self.assertEqual(
                generated.stderr,
                "native_ci_evidence_invalid reason=build_identity_invalid\n",
            )
            self.assertFalse(evidence.exists())
            self.assertNotIn(str(root), generated.stderr)

    def test_verify_rejects_build_identity_digest_size_and_identity_tamper(self):
        for case in ("digest", "size", "identity"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                collector = root / "openusage-collector"
                artifact = root / "UsageHub-0.8.6-mac-x64.dmg"
                evidence = root / "usagehub-native-evidence-mac-x64.json"
                _write_macos_x64_collector(collector)
                _write_dmg(artifact)
                generated = subprocess.run(
                    _generate_command(
                        collector=collector,
                        artifact=artifact,
                        output=evidence,
                    ),
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(generated.returncode, 0, generated.stderr)
                payload = json.loads(evidence.read_text(encoding="utf-8"))
                if case == "digest":
                    payload["buildIdentity"]["artifact"]["sha256"] = "0" * 64
                elif case == "size":
                    payload["buildIdentity"]["artifact"]["sizeBytes"] += 1
                else:
                    payload["buildIdentity"]["identity"]["displayName"] = "OtherHub"
                _write_ascii(
                    evidence,
                    json.dumps(payload, indent=2, sort_keys=True) + "\n",
                )

                verified = subprocess.run(
                    _verify_command(
                        evidence=evidence,
                        collector=collector,
                        artifact=artifact,
                    ),
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(verified.returncode, 1)
                self.assertEqual(
                    verified.stderr,
                    "native_ci_evidence_invalid reason=build_identity_invalid\n",
                )
                self.assertNotIn(str(root), verified.stderr)
                self.assertNotIn("Traceback", verified.stderr)

    def test_verify_rejects_wrong_json_types_without_a_traceback_or_path_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector = root / "openusage-collector"
            artifact = root / "UsageHub-0.8.6-mac-x64.dmg"
            evidence = root / "usagehub-native-evidence-mac-x64.json"
            _write_macos_x64_collector(collector)
            _write_dmg(artifact)
            generated = subprocess.run(
                _generate_command(
                    collector=collector,
                    artifact=artifact,
                    output=evidence,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            payload["target"]["platform"] = ["mac"]
            _write_ascii(
                evidence,
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
            )

            verified = subprocess.run(
                _verify_command(
                    evidence=evidence,
                    collector=collector,
                    artifact=artifact,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(verified.returncode, 1)
            self.assertEqual(
                verified.stderr,
                "native_ci_evidence_invalid reason=field_type_invalid\n",
            )
            self.assertNotIn(str(root), verified.stderr)
            self.assertNotIn("Traceback", verified.stderr)

    def test_verify_rejects_a_forged_distribution_release_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector = root / "openusage-collector"
            artifact = root / "UsageHub-0.8.6-mac-x64.dmg"
            evidence = root / "usagehub-native-evidence-mac-x64.json"
            _write_macos_x64_collector(collector)
            _write_dmg(artifact)
            generated = subprocess.run(
                _generate_command(
                    collector=collector,
                    artifact=artifact,
                    output=evidence,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            payload["distributionTrust"]["releaseEligible"] = True
            _write_ascii(
                evidence,
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
            )

            verified = subprocess.run(
                _verify_command(
                    evidence=evidence,
                    collector=collector,
                    artifact=artifact,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(verified.returncode, 1)
            self.assertEqual(
                verified.stderr,
                "native_ci_evidence_invalid reason=distribution_trust_invalid\n",
            )
            self.assertNotIn(str(root), verified.stderr)
            self.assertNotIn("Traceback", verified.stderr)

    def test_verify_rejects_float_schema_version_even_when_it_compares_equal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector = root / "openusage-collector"
            artifact = root / "UsageHub-0.8.6-mac-x64.dmg"
            evidence = root / "usagehub-native-evidence-mac-x64.json"
            _write_macos_x64_collector(collector)
            _write_dmg(artifact)
            generated = subprocess.run(
                _generate_command(
                    collector=collector,
                    artifact=artifact,
                    output=evidence,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            payload["schemaVersion"] = 1.0
            _write_ascii(
                evidence,
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
            )

            verified = subprocess.run(
                _verify_command(
                    evidence=evidence,
                    collector=collector,
                    artifact=artifact,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(verified.returncode, 1)
            self.assertEqual(
                verified.stderr,
                "native_ci_evidence_invalid reason=schema_version_invalid\n",
            )

    def test_generate_rejects_pe32_collector_when_contract_requires_pe32_plus(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector = root / "openusage-collector.exe"
            artifact = root / "UsageHub-0.8.6-win-x64.exe"
            evidence = root / "usagehub-native-evidence-win-x64.json"
            source_evidence = root / "observer-source.json"
            _write_pe(collector, arch="x64", pe_plus=False)
            _write_pe(artifact, arch="x64", pe_plus=True)
            _write_source_evidence(source_evidence, platform="windows")

            generated = subprocess.run(
                _generate_command(
                    collector=collector,
                    artifact=artifact,
                    output=evidence,
                    platform="win",
                    arch="x64",
                    observer_source_evidence=source_evidence,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(generated.returncode, 1)
            self.assertEqual(
                generated.stderr,
                "native_ci_evidence_invalid reason=collector_format_invalid\n",
            )
            self.assertFalse(evidence.exists())

    def test_generate_and_verify_accept_exactly_each_of_the_six_native_rows(self):
        for platform, arch in RUNNERS:
            with self.subTest(platform=platform, arch=arch):
                self.assertEqual(
                    _check_outcomes(platform)["desktopRuntimeCapabilitySmoke"],
                    "success",
                )
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    collector_name, artifact_name = TARGET_FILES[(platform, arch)]
                    collector = root / collector_name
                    artifact = root / artifact_name
                    evidence = root / f"evidence-{platform}-{arch}.json"
                    _write_native_row_files(
                        collector,
                        artifact,
                        platform=platform,
                        arch=arch,
                    )
                    observer_source_evidence = None
                    if platform != "mac":
                        observer_source_evidence = (
                            root / f"observer-source-{platform}-{arch}.json"
                        )
                        _write_source_evidence(
                            observer_source_evidence,
                            platform=(
                                "windows" if platform == "win" else "linux"
                            ),
                        )

                    generated = subprocess.run(
                        _generate_command(
                            collector=collector,
                            artifact=artifact,
                            output=evidence,
                            platform=platform,
                            arch=arch,
                            observer_source_evidence=observer_source_evidence,
                        ),
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(generated.returncode, 0, generated.stderr)
                    payload = json.loads(evidence.read_text(encoding="utf-8"))
                    self.assertEqual(
                        payload["distributionTrust"],
                        _distribution_trust(platform),
                    )
                    verified = subprocess.run(
                        _verify_command(
                            evidence=evidence,
                            collector=collector,
                            artifact=artifact,
                            platform=platform,
                            arch=arch,
                        ),
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(verified.returncode, 0, verified.stderr)
                    self.assertEqual(
                        verified.stdout,
                        f"native_ci_evidence_ok {platform}/{arch}\n",
                    )

    def test_verify_rejects_final_container_hash_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector = root / "openusage-collector"
            artifact = root / "UsageHub-0.8.6-mac-x64.dmg"
            evidence = root / "evidence.json"
            _write_macho(collector, arch="x64")
            _write_dmg(artifact)
            generated = subprocess.run(
                _generate_command(
                    collector=collector,
                    artifact=artifact,
                    output=evidence,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            mutated = bytearray(artifact.read_bytes())
            mutated[0] ^= 1
            artifact.write_bytes(mutated)

            verified = subprocess.run(
                _verify_command(
                    evidence=evidence,
                    collector=collector,
                    artifact=artifact,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(verified.returncode, 1)
            self.assertEqual(
                verified.stderr,
                "native_ci_evidence_invalid "
                "reason=distribution_trust_posture_invalid\n",
            )

    def test_generate_rejects_symlinked_collector_without_echoing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_collector = root / "missing-private-user-collector"
            collector = root / "openusage-collector"
            artifact = root / "UsageHub-0.8.6-mac-x64.dmg"
            evidence = root / "evidence.json"
            collector.symlink_to(real_collector)
            _write_dmg(artifact)

            generated = subprocess.run(
                _generate_command(
                    collector=collector,
                    artifact=artifact,
                    output=evidence,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(generated.returncode, 1)
            self.assertEqual(
                generated.stderr,
                "native_ci_evidence_invalid reason=collector_not_regular\n",
            )
            self.assertNotIn("private-user-collector", generated.stderr)
            self.assertFalse(evidence.exists())

    def test_verify_rejects_a_valid_but_swapped_trust_posture_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector = root / "openusage-collector"
            artifact = root / "UsageHub-0.8.6-mac-x64.dmg"
            evidence = root / "evidence.json"
            _write_macho(collector, arch="x64")
            _write_dmg(artifact)
            generated = subprocess.run(
                _generate_command(
                    collector=collector,
                    artifact=artifact,
                    output=evidence,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            posture = _posture_report_path(evidence, "mac", "x64")
            payload = json.loads(posture.read_text(encoding="utf-8"))
            payload["platformCodeSigning"] = "ad_hoc_strict_invalid"
            _write_ascii(
                posture,
                json.dumps(
                    payload,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n",
            )

            verified = subprocess.run(
                _verify_command(
                    evidence=evidence,
                    collector=collector,
                    artifact=artifact,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(verified.returncode, 1)
            self.assertEqual(
                verified.stderr,
                "native_ci_evidence_invalid reason=evidence_mismatch\n",
            )

    def test_verify_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector = root / "openusage-collector"
            artifact = root / "UsageHub-0.8.6-mac-x64.dmg"
            evidence = root / "evidence.json"
            _write_macho(collector, arch="x64")
            _write_dmg(artifact)
            _write_ascii(evidence, '{"schemaVersion":1,"schemaVersion":1}\n')

            verified = subprocess.run(
                _verify_command(
                    evidence=evidence,
                    collector=collector,
                    artifact=artifact,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(verified.returncode, 1)
            self.assertEqual(
                verified.stderr,
                "native_ci_evidence_invalid reason=duplicate_key\n",
            )

    def test_verify_rejects_deep_json_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collector = root / "openusage-collector"
            artifact = root / "UsageHub-0.8.6-mac-x64.dmg"
            evidence = root / "evidence.json"
            _write_macho(collector, arch="x64")
            _write_dmg(artifact)
            _write_ascii(evidence, "[" * 2000 + "0" + "]" * 2000)

            verified = subprocess.run(
                _verify_command(
                    evidence=evidence,
                    collector=collector,
                    artifact=artifact,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(verified.returncode, 1)
            self.assertEqual(
                verified.stderr,
                "native_ci_evidence_invalid reason=evidence_depth_invalid\n",
            )
            self.assertNotIn("Traceback", verified.stderr)

    def test_json_depth_preflight_ignores_brackets_inside_strings(self):
        evidence_module._preflight_json_depth(
            json.dumps({"value": "[" * 100 + "\\\"" + "]" * 100})
        )
        with self.assertRaisesRegex(
            evidence_module.EvidenceError,
            "^evidence_depth_invalid$",
        ):
            evidence_module._preflight_json_depth("[" * 17 + "]" * 17)

    def test_workflow_fetches_release_tags_before_product_truth_verification(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        checkout_start = source.index("- name: Check out source")
        setup_python_start = source.index(
            "- name: Set up pinned Python",
            checkout_start,
        )
        checkout = source[checkout_start:setup_python_start]
        product_truth_start = source.index(
            "- name: Verify product and version truth",
            setup_python_start,
        )

        self.assertIn("with:", checkout)
        self.assertIn("fetch-depth: 0", checkout)
        self.assertLess(checkout_start, product_truth_start)

    def test_workflow_bootstraps_pinned_linux_secret_service_without_weakening_source_evidence(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        bootstrap_marker = "- name: Prepare pinned Linux Observer secret service"
        source_probe_marker = "- name: Probe and verify Observer source evidence"
        generate_marker = "- name: Generate native CI evidence"
        handoff_marker = "- name: Assemble and verify local release handoff"
        upload_marker = "- name: Upload artifact and evidence"

        self.assertIn(bootstrap_marker, source)
        bootstrap_start = source.index(bootstrap_marker)
        source_probe_start = source.index(source_probe_marker)
        generate_start = source.index(generate_marker)
        handoff_start = source.index(handoff_marker)
        upload_start = source.index(upload_marker)
        self.assertLess(bootstrap_start, source_probe_start)
        self.assertLess(source_probe_start, generate_start)

        bootstrap_end = source.index("- name:", bootstrap_start + 1)
        bootstrap = source[bootstrap_start:bootstrap_end]
        source_probe = source[source_probe_start:generate_start]
        generate = source[generate_start:handoff_start]
        handoff = source[handoff_start:upload_start]
        upload_end = source.index("\n  gateway-performance:", upload_start)
        upload = source[upload_start:upload_end]

        self.assertIn("if: matrix.platform == 'linux'", bootstrap)
        self.assertIn("apt-get install", bootstrap)
        for package, version in (
            ("dbus-user-session", "1.14.10-4ubuntu4.1"),
            ("gnome-keyring", "46.1-2build1"),
        ):
            with self.subTest(package=package):
                self.assertIn(f'"{package}={version}"', bootstrap)
                self.assertRegex(
                    bootstrap,
                    rf"(?s)dpkg-query.{{0,300}}{package}|"
                    rf"{package}.{{0,300}}dpkg-query",
                )
        self.assertIn("if: matrix.platform != 'mac'", source_probe)
        self.assertIn("mktemp -d", source_probe)
        for xdg_variable in (
            "XDG_RUNTIME_DIR",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
        ):
            with self.subTest(xdg_variable=xdg_variable):
                self.assertIn(xdg_variable, source_probe)
        self.assertIn("chmod 0700", source_probe)
        self.assertIn("dbus-run-session", source_probe)
        self.assertIn(
            "printf '\\n' | gnome-keyring-daemon "
            "--unlock --components=secrets >/dev/null 2>&1",
            source_probe,
        )
        self.assertIn("gnome-keyring-daemon --shutdown", source_probe)
        self.assertIn("trap", source_probe)
        self.assertRegex(
            source_probe,
            r'(?s)if \[\[ "\$\{\{ matrix\.platform \}\}" == "linux" \]\]; then'
            r".*?dbus-run-session"
            r".*?scripts/observer_source_native_evidence\.py probe"
            r".*?\n\s*else\s*\n"
            r"\s*python scripts/observer_source_native_evidence\.py probe"
            r".*?\n\s*fi\s*\n",
        )
        self.assertEqual(
            source_probe.count("scripts/observer_source_native_evidence.py probe"),
            2,
        )
        self.assertIn(
            "scripts/observer_source_native_evidence.py verify",
            source_probe,
        )
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", source_probe)

        for bootstrap_token in ("bootstrap", "dbus", "keyring"):
            with self.subTest(bootstrap_token=bootstrap_token):
                self.assertNotIn(bootstrap_token, generate.casefold())
        for derived_claim in ("supportedSourceCount", "promotionEligible"):
            with self.subTest(derived_claim=derived_claim):
                self.assertNotIn(derived_claim, source)
        self.assertNotIn("--observer-source-evidence", handoff)
        self.assertEqual(upload.count("actions/upload-artifact@"), 1)
        self.assertNotIn("observer_source_evidence", upload)

    def test_workflow_manually_runs_all_rows_then_uploads_container_with_verified_evidence(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        setup_node_sha = "820762786026740c76f36085b0efc47a31fe5020"
        self.assertIn("\n  workflow_dispatch:\n", source)
        self.assertIn(
            f"actions/setup-node@{setup_node_sha} # v7.0.0",
            source,
        )
        self.assertRegex(source, r'(?m)^\s+node-version: "24\.16\.0"$')
        self.assertRegex(
            source,
            r"(?ms)- name: Set up pinned Node.*?architecture: \$\{\{ matrix\.arch \}\}",
        )
        for tracked_path in (
            "docs/observer-native-runner-prerequisites.md",
            "docs/observer-source-native-evidence.md",
            "docs/schemas/observer-source-native-evidence-v1.schema.json",
            "scripts/observer_source_native_evidence.py",
            "tests/test_bounded_process.py",
            "tests/test_observer_source_native_evidence.py",
        ):
            with self.subTest(tracked_path=tracked_path):
                self.assertEqual(
                    source.count(f'- "{tracked_path}"'),
                    2,
                )
        manifest = json.loads(ACTION_PINS.read_text(encoding="utf-8"))
        self.assertIn(
            {
                "repository": "actions/setup-node",
                "version": "v7.0.0",
                "commit": setup_node_sha,
            },
            manifest["pins"],
        )

        required_order = [
            "- name: Verify action pins",
            "- name: Verify runner identity",
            "- name: Run portable Observer and Gateway contracts",
            "- name: Run native Windows Job contracts",
            "- name: Build bundled collector",
            "- name: Smoke bundled collector status",
            "- name: Smoke bundled Gateway modes",
            "- name: Audit packaged collector",
            "- name: Resolve final artifact",
            "- name: Audit final macOS DMG",
            "- name: Audit final Windows NSIS payload",
            "- name: Audit final Linux AppImage payload",
            "- name: Select audited final artifact",
            "- name: Verify distribution trust posture",
            "- name: Probe and verify Observer source evidence",
            "- name: Generate native CI evidence",
            "- name: Verify native CI evidence",
            "- name: Assemble and verify local release handoff",
            "- name: Upload artifact and evidence",
        ]
        positions = [source.index(marker) for marker in required_order]
        self.assertEqual(positions, sorted(positions))
        native_start = source.index("- name: Run native Windows Job contracts")
        contracts_start = source.index(
            "- name: Run portable Observer and Gateway contracts"
        )
        contracts = source[contracts_start:native_start]
        contract_lines = [line.strip() for line in contracts.splitlines()]
        self.assertIn(
            "tests.test_bounded_process \\",
            contract_lines,
        )
        self.assertIn(
            "tests.test_bounded_process_windows",
            contracts,
        )
        self.assertIn(
            "tests.test_observer_source_native_evidence",
            contracts,
        )
        native_end = source.index("- name:", native_start + 1)
        self.assertIn("if: matrix.platform == 'win'", source[native_start:native_end])
        self.assertIn(
            "tests.test_bounded_process_windows_native",
            source[native_start:native_end],
        )

        gateway_smoke = source.index("id: gateway_smoke")
        runtime_capability_smoke = source.index(
            "id: desktop_runtime_capability_smoke"
        )
        desktop_package = source.index("id: desktop_package")
        self.assertLess(gateway_smoke, runtime_capability_smoke)
        self.assertLess(runtime_capability_smoke, desktop_package)
        runtime_capability_smoke_end = source.index(
            "- name:", runtime_capability_smoke
        )
        runtime_capability_smoke_step = source[
            runtime_capability_smoke:runtime_capability_smoke_end
        ]
        self.assertIn(
            'node desktop/scripts/smoke_runtime_capability.mjs --collector '
            '"./dist-collector/${{ matrix.collector }}"',
            runtime_capability_smoke_step,
        )

        generate_start = source.index("- name: Generate native CI evidence")
        source_probe_start = source.index(
            "- name: Probe and verify Observer source evidence"
        )
        verify_start = source.index("- name: Verify native CI evidence")
        upload_start = source.index("- name: Upload artifact and evidence")
        performance_start = source.index("\n  gateway-performance:")
        generate = source[generate_start:verify_start]
        source_probe = source[source_probe_start:generate_start]
        verify = source[verify_start:upload_start]
        upload = source[upload_start:performance_start]
        performance = source[performance_start:]
        for step_id in (
            "action_pins",
            "runner_identity",
            "contracts",
            "windows_native",
            "web_audit",
            "web_tests",
            "web_build",
            "desktop_audit",
            "desktop_tests",
            "collector_status",
            "gateway_smoke",
            "desktop_runtime_capability_smoke",
            "unpacked_audit",
            "distribution_trust_posture",
            "final_mac",
            "final_win",
            "final_linux",
        ):
            with self.subTest(step_id=step_id):
                self.assertIn(f"steps.{step_id}.outcome", generate)
        self.assertIn("if: matrix.platform != 'mac'", source_probe)
        self.assertIn(
            "RUNNER_ENVIRONMENT: ${{ runner.environment }}",
            source_probe,
        )
        self.assertIn(
            "scripts/observer_source_native_evidence.py probe",
            source_probe,
        )
        self.assertIn(
            "scripts/observer_source_native_evidence.py verify",
            source_probe,
        )
        for forbidden_override in (
            "--platform",
            "--home",
            "--secret",
            "--status",
            "--result",
        ):
            with self.subTest(forbidden_override=forbidden_override):
                self.assertNotIn(forbidden_override, source_probe)
        self.assertIn("scripts/native_ci_evidence.py generate", generate)
        self.assertIn("observer_source_args=()", generate)
        self.assertIn(
            'if [[ "${{ matrix.platform }}" != "mac" ]]; then',
            generate,
        )
        self.assertIn(
            '--observer-source-evidence "${{ steps.observer_source_evidence.outputs.path }}"',
            generate,
        )
        self.assertIn('"${observer_source_args[@]}"', generate)
        self.assertIn(
            '--check "desktopRuntimeCapabilitySmoke='
            '${{ steps.desktop_runtime_capability_smoke.outcome }}"',
            generate,
        )
        self.assertIn(
            '--check "distributionTrustPosture='
            '${{ steps.distribution_trust_posture.outcome }}"',
            generate,
        )
        self.assertIn(
            '--trust-posture-report "$TRUST_POSTURE_REPORT"',
            generate,
        )
        self.assertIn(
            "scripts/distribution_trust_posture.py verify",
            source,
        )
        self.assertEqual(
            source.count("scripts/distribution_trust_posture.py inspect"),
            3,
        )
        self.assertNotIn("codesign --sign", source)
        self.assertNotIn("notarytool submit", source)
        self.assertNotIn("signtool sign", source.casefold())
        self.assertIn("scripts/native_ci_evidence.py verify", verify)
        self.assertIn(
            '--trust-posture-report "$TRUST_POSTURE_REPORT"',
            verify,
        )
        self.assertEqual(upload.count("actions/upload-artifact@"), 1)
        self.assertIn("${{ steps.release_handoff.outputs.path }}", upload)
        self.assertNotIn("${{ steps.artifact.outputs.path }}", upload)
        self.assertNotIn("${{ steps.audited_artifact.outputs.path }}", upload)
        self.assertNotIn("${{ steps.evidence.outputs.path }}", upload)
        self.assertNotIn(
            "${{ steps.distribution_trust_posture.outputs.path }}",
            upload,
        )
        self.assertNotIn("observer_source_evidence", upload)
        self.assertIn("retention-days: 30", upload)
        self.assertNotIn("*", upload)
        self.assertIn("if: github.event_name == 'workflow_dispatch'", performance)
        self.assertIn("tests.test_gateway_performance_measurement", performance)
        self.assertIn("measure_gateway_performance.py smoke", performance)
        self.assertIn("measure_gateway_performance.py run", performance)
        self.assertIn("measure_gateway_performance.py verify", performance)
        self.assertNotIn("--enforce", performance)
        self.assertEqual(performance.count("actions/upload-artifact@"), 1)
        self.assertNotIn("if: always()", source)

    def test_workflow_runs_only_the_linux_x64_onefile_local_api_canary(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        build_marker = "- name: Build bundled collector"
        canary_marker = "- name: Run onefile Local API canary"
        package_marker = "- name: Package desktop app"

        self.assertEqual(source.count(canary_marker), 1)
        build_start = source.index(build_marker)
        canary_start = source.index(canary_marker)
        package_start = source.index(package_marker)
        self.assertLess(build_start, canary_start)
        self.assertLess(canary_start, package_start)

        canary_end = source.index("\n      - name:", canary_start + 1)
        canary = source[canary_start:canary_end]
        self.assertIn("if: matrix.platform == 'linux' && matrix.arch == 'x64'", canary)
        self.assertIn("shell: bash", canary)
        self.assertIn(
            'collector_path="$(realpath --canonicalize-existing '
            '"./dist-collector/${{ matrix.collector }}")"',
            canary,
        )
        self.assertIn(
            'python -m scripts.canary_onefile_local_api --collector "$collector_path"',
            canary,
        )
        for forbidden in (
            "GITHUB_OUTPUT",
            "native_lifecycle",
            "stableDirectChild",
            "upload-artifact",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, canary)

        contracts_start = source.index(
            "- name: Run portable Observer and Gateway contracts"
        )
        contracts_end = source.index("- name: Run native Windows Job contracts")
        contracts = source[contracts_start:contracts_end]
        self.assertIn("tests.test_canary_onefile_local_api", contracts)

        push = source[source.index("  push:\n"):source.index("  pull_request:\n")]
        pull_request = source[
            source.index("  pull_request:\n"):source.index("\npermissions:")
        ]
        for event, block in (("push", push), ("pull_request", pull_request)):
            with self.subTest(event=event):
                self.assertIn('- "scripts/canary_onefile_local_api.py"', block)
                self.assertIn('- "tests/test_canary_onefile_local_api.py"', block)

        package_job = source[:source.index("\n  gateway-performance:")]
        self.assertEqual(package_job.count("actions/upload-artifact@"), 1)
        upload_start = package_job.index("- name: Upload artifact and evidence")
        upload = package_job[upload_start:]
        self.assertNotIn("onefile", upload.casefold())
        self.assertNotIn("stableDirectChild", upload)

    def test_workflow_runs_linux_x64_audited_appimage_preserve_uninstall_from_held_fd(
        self,
    ):
        source = WORKFLOW.read_text(encoding="utf-8")
        select_marker = "- name: Select audited final artifact"
        preserve_marker = "- name: Run audited AppImage preserve uninstall"
        evidence_marker = "- name: Generate native CI evidence"

        self.assertEqual(source.count(preserve_marker), 1)
        select_start = source.index(select_marker)
        preserve_start = source.index(preserve_marker)
        evidence_start = source.index(evidence_marker)
        self.assertLess(select_start, preserve_start)
        self.assertLess(preserve_start, evidence_start)

        preserve_end = source.index("\n      - name:", preserve_start + 1)
        preserve = source[preserve_start:preserve_end]
        self.assertIn(
            "if: matrix.platform == 'linux' && matrix.arch == 'x64'",
            preserve,
        )
        self.assertIn("shell: bash", preserve)
        self.assertIn(
            "FINAL_ARTIFACT: ${{ steps.audited_artifact.outputs.path }}",
            preserve,
        )
        self.assertIn("set -euo pipefail", preserve)
        user_preflight = preserve.index('preserve_user="usagehub-preserve"')
        absent_preflight = preserve.index(
            'if id "$preserve_user" >/dev/null 2>&1; then'
        )
        root_creation = preserve.index('preserve_root="$(mktemp -d ')
        self.assertLess(user_preflight, absent_preflight)
        self.assertLess(absent_preflight, root_creation)
        self.assertIn(
            'preserve_root="$(mktemp -d '
            '"/tmp/usagehub-preserve-uninstall.XXXXXX")"',
            preserve,
        )
        self.assertIn(
            'preserve_root_identity="$(/usr/bin/stat --format=\'%d:%i\' '
            '"$preserve_root")"',
            preserve,
        )
        self.assertNotIn(
            '$RUNNER_TEMP/usagehub-preserve-uninstall.',
            preserve,
        )
        for owned_directory in ("home", "home/data", "home/tmp"):
            with self.subTest(owned_directory=owned_directory):
                self.assertIn(
                    f'"$preserve_root/{owned_directory}"',
                    preserve,
                )
        self.assertIn(
            'preserve_execution="$preserve_root/execution.AppImage"',
            preserve,
        )
        self.assertIn(
            'packaged_collector_source="$RUNNER_TEMP/usagehub-appimage-'
            '${{ matrix.arch }}/resources/collector/${{ matrix.collector }}"',
            preserve,
        )
        self.assertIn(
            'packaged_collector="$preserve_root/openusage-collector"',
            preserve,
        )
        self.assertIn(
            'service_absence_source="$preserve_root/service-absence-source"',
            preserve,
        )
        self.assertIn(
            '/bin/cp -R "$GITHUB_WORKSPACE/openusage_bar"', preserve
        )
        self.assertIn(
            '"$service_absence_source/openusage_bar"', preserve
        )
        self.assertIn(
            '/bin/cp "$GITHUB_WORKSPACE/scripts/canary_linux_service_absence.py"',
            preserve,
        )
        self.assertIn(
            '"$service_absence_source/scripts/canary_linux_service_absence.py"',
            preserve,
        )
        self.assertIn(
            '"$GITHUB_WORKSPACE/openusage_bar/platform_services.py"',
            preserve,
        )
        self.assertIn(
            '"$service_absence_source/openusage_bar/platform_services.py"',
            preserve,
        )
        self.assertIn(
            '"$GITHUB_WORKSPACE/scripts/canary_linux_service_absence.py"',
            preserve,
        )
        self.assertIn(
            '"$service_absence_source/scripts/canary_linux_service_absence.py"',
            preserve,
        )
        self.assertIn(
            '/bin/cp "$packaged_collector_source" "$packaged_collector"',
            preserve,
        )
        self.assertIn(
            '/usr/bin/cmp -s "$packaged_collector_source" "$packaged_collector"',
            preserve,
        )
        self.assertIn('test -x "$packaged_collector"', preserve)
        self.assertIn('/bin/cp "$FINAL_ARTIFACT" "$preserve_execution"', preserve)
        self.assertIn(
            '/usr/bin/cmp -s "$FINAL_ARTIFACT" "$preserve_execution"',
            preserve,
        )
        self.assertIn(
            'source_digest="$(/usr/bin/sha256sum "$FINAL_ARTIFACT"',
            preserve,
        )
        self.assertIn(
            'execution_digest="$(/usr/bin/sha256sum "$preserve_execution"',
            preserve,
        )
        self.assertIn('test "$execution_digest" = "$source_digest"', preserve)
        self.assertIn('/bin/chmod 0700 "$preserve_execution"', preserve)
        self.assertIn(
            'test "$(/usr/bin/stat --format=\'%a\' "$FINAL_ARTIFACT")" = "400"',
            preserve,
        )
        self.assertIn('preserve_user="usagehub-preserve"', preserve)
        self.assertIn(
            'if id "$preserve_user" >/dev/null 2>&1; then',
            preserve,
        )
        self.assertIn(
            'sudo useradd --no-create-home --home-dir "$preserve_root/home" '
            '"$preserve_user"',
            preserve,
        )
        user_creation = preserve.index(
            'sudo useradd --no-create-home --home-dir "$preserve_root/home" '
            '"$preserve_user"'
        )
        home_creation = preserve.index(
            'sudo mkdir --mode=0700 "$preserve_root/home"'
        )
        self.assertLess(user_creation, home_creation)
        self.assertIn('current_uid="$(id -u "$preserve_user")"', preserve)
        self.assertIn(
            'test "$(getent passwd "$preserve_user" | cut -d: -f6)" '
            '= "$preserve_root/home"',
            preserve,
        )
        self.assertIn(
            'sudo chown "$preserve_user:$preserve_user"',
            preserve,
        )
        for owned_path in (
            '"$preserve_execution"',
            '"$preserve_root/home/data"',
            '"$preserve_root/home/tmp"',
        ):
            with self.subTest(owned_path=owned_path):
                self.assertIn(owned_path, preserve)
        self.assertIn('sudo -u "$preserve_user" /usr/bin/env -i', preserve)
        self.assertIn('/bin/bash -c', preserve)
        inner_start = preserve.index("/bin/bash -c '") + len("/bin/bash -c '")
        inner_end = preserve.index("\n            ' bash", inner_start)
        inner_script = preserve[inner_start:inner_end]
        syntax_check = subprocess.run(
            ["/bin/bash", "-n"],
            input=inner_script,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(syntax_check.returncode, 0)
        self.assertNotIn("printf '", inner_script)
        collector_invocation = preserve.index(
            '"$packaged_collector" desktop-service uninstall'
        )
        absence_canary = preserve.index(
            "/usr/bin/python3 -m scripts.canary_linux_service_absence"
        )
        wrapper_binding = preserve.index(
            'exec {appimage_fd}<"$preserve_execution"'
        )
        self.assertLess(collector_invocation, absence_canary)
        self.assertLess(absence_canary, wrapper_binding)
        self.assertIn('service_absence_stdout="$5"', preserve)
        self.assertIn('service_absence_stderr="$6"', preserve)
        self.assertIn('service_absence_source="$7"', preserve)
        self.assertIn(
            'sudo chown -R "$preserve_user:$preserve_user"',
            preserve,
        )
        self.assertIn('"$service_absence_source"', preserve)
        self.assertIn('cd "$service_absence_source"', preserve)
        self.assertIn(
            '"$service_absence_stderr"',
            preserve,
        )
        self.assertIn('"$service_absence_source"', preserve)
        self.assertIn(
            '>"$service_absence_stdout" 2>"$service_absence_stderr"',
            preserve,
        )
        self.assertIn('test ! -s "$service_absence_stdout"', preserve)
        self.assertIn('test ! -s "$service_absence_stderr"', preserve)
        for status, category in (
            (10, "authority"),
            (11, "runtime-peer"),
            (12, "manager-provenance"),
            (13, "manager-binary-readlink"),
            (14, "manager-binary-path-value"),
            (15, "manager-binary-metadata"),
            (16, "manager-binary-public-identity"),
            (17, "systemctl-binding"),
            (18, "unit-absence"),
            (19, "manager-query"),
            (20, "sandwich"),
            (21, "cleanup"),
        ):
            with self.subTest(service_absence_category=category):
                self.assertIn(
                    f'{status}) service_absence_category="{category}"',
                    preserve,
                )
                self.assertIn(
                    'category=service-absence-$service_absence_category',
                    preserve,
                )
        self.assertIn(
            'service_absence_category="unknown"', preserve
        )
        for private_reader in (
            'cat "$service_absence_stdout"',
            'cat "$service_absence_stderr"',
            'head "$service_absence_stdout"',
            'head "$service_absence_stderr"',
            'tail "$service_absence_stdout"',
            'tail "$service_absence_stderr"',
        ):
            with self.subTest(private_reader=private_reader):
                self.assertNotIn(private_reader, preserve)
        self.assertIn(
            '>"$collector_stdout" 2>"$collector_stderr"',
            preserve,
        )
        self.assertIn('test ! -s "$collector_stdout"', preserve)
        self.assertIn('test ! -s "$collector_stderr"', preserve)
        self.assertIn(
            "appimage_preserve_uninstall_failed category=collector",
            preserve,
        )
        self.assertIn(
            "appimage_preserve_uninstall_failed category=wrapper",
            preserve,
        )
        self.assertIn('exec {appimage_fd}<"$preserve_execution"', preserve)
        self.assertIn(
            'HOME="$preserve_root/home"',
            preserve,
        )
        self.assertIn('XDG_DATA_HOME="$preserve_root/home/data"', preserve)
        self.assertIn('TMPDIR="$preserve_root/home/tmp"', preserve)
        self.assertIn(
            'sudo systemctl start "user@$current_uid.service"',
            preserve,
        )
        private_socket_probe = (
            'sudo -u "$preserve_user" test -S '
            '"/run/user/$current_uid/systemd/private"'
        )
        self.assertEqual(preserve.count(private_socket_probe), 2)
        self.assertNotIn(
            '\n            if [[ -S "/run/user/$current_uid/systemd/private" ]]',
            preserve,
        )
        self.assertNotIn(
            '\n          test -S "/run/user/$current_uid/systemd/private"',
            preserve,
        )
        self.assertIn('XDG_RUNTIME_DIR="/run/user/$current_uid"', preserve)
        self.assertIn(
            'DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/'
            '$current_uid/systemd/private"',
            preserve,
        )
        self.assertIn('APPIMAGE_EXTRACT_AND_RUN=1', preserve)
        self.assertIn('test -x /usr/bin/xvfb-run', preserve)
        self.assertIn("/usr/bin/env -i", preserve)
        for closed_environment in (
            'PATH="/usr/bin:/bin"',
            "LANG=C",
            "LC_ALL=C",
            "PYTHONNOUSERSITE=1",
        ):
            with self.subTest(closed_environment=closed_environment):
                self.assertIn(closed_environment, preserve)
        self.assertIn(
            'timeout --signal=TERM --kill-after=10s 180s '
            '/usr/bin/xvfb-run --auto-servernum '
            '"/proc/self/fd/$appimage_fd" --usagehub-uninstall',
            preserve,
        )
        self.assertLess(
            preserve.index('exec {appimage_fd}<"$preserve_execution"'),
            preserve.index(
                '/usr/bin/xvfb-run --auto-servernum '
                '"/proc/self/fd/$appimage_fd" --usagehub-uninstall'
            ),
        )
        self.assertEqual(
            preserve.count('--no-sandbox'),
            0,
        )
        self.assertIn('test "$preserve_status" -eq 0', preserve)
        self.assertIn('>/dev/null 2>&1', preserve)
        self.assertNotIn('preserve_stdout=', preserve)
        self.assertNotIn('preserve_stderr=', preserve)
        for fixed_failure in (
            "appimage_preserve_uninstall_failed category=collector",
            "appimage_preserve_uninstall_failed category=wrapper",
        ):
            with self.subTest(fixed_failure=fixed_failure):
                self.assertIn(fixed_failure, preserve)
        for private_output_probe in (
            'cat "$collector_stdout"',
            'cat "$collector_stderr"',
            'cat "$service_absence_stdout"',
            'cat "$service_absence_stderr"',
            'head "$collector_stdout"',
            'head "$collector_stderr"',
            'head "$service_absence_stdout"',
            'head "$service_absence_stderr"',
            'tail "$collector_stdout"',
            'tail "$collector_stderr"',
            'tail "$service_absence_stdout"',
            'tail "$service_absence_stderr"',
        ):
            with self.subTest(private_output_probe=private_output_probe):
                self.assertNotIn(private_output_probe, preserve)
        self.assertIn('sudo systemctl stop "user@$current_uid.service"', preserve)
        self.assertIn(
            'sudo userdel "$preserve_user" >/dev/null 2>&1',
            preserve,
        )
        self.assertLess(
            preserve.index('sudo systemctl stop "user@$current_uid.service"'),
            preserve.index('sudo userdel "$preserve_user"'),
        )
        self.assertIn(
            'sudo loginctl terminate-user "$current_uid" >/dev/null 2>&1',
            preserve,
        )
        self.assertIn(
            'if ! /usr/bin/pgrep --uid "$current_uid" >/dev/null 2>&1; then',
            preserve,
        )
        self.assertIn(
            'if /usr/bin/pgrep --uid "$current_uid" >/dev/null 2>&1; then',
            preserve,
        )
        self.assertLess(
            preserve.index('sudo loginctl terminate-user "$current_uid"'),
            preserve.index('sudo userdel "$preserve_user"'),
        )
        self.assertNotIn("sudo rmdir", preserve)
        for wrapper_status, category in (
            (2, "wrapper-request"),
            (3, "wrapper-plan"),
            (4, "wrapper-collector"),
        ):
            with self.subTest(wrapper_status=wrapper_status):
                self.assertIn(
                    f'"$preserve_status" -eq {wrapper_status}',
                    preserve,
                )
                self.assertIn(
                    f"appimage_preserve_uninstall_failed category={category}",
                    preserve,
                )
        for cleanup_category in (
            "manager",
            "process",
            "account",
            "root-identity",
            "root-remove",
        ):
            self.assertIn(
                f"appimage_preserve_cleanup_failed category={cleanup_category}",
                preserve,
            )
        self.assertIn(
            'test "$(/usr/bin/stat --format=\'%d:%i\' "$preserve_root")" '
            '= "$preserve_root_identity"',
            preserve,
        )
        self.assertIn('sudo /bin/rm -rf --one-file-system "$preserve_root"', preserve)
        self.assertNotIn('sudo /bin/rm -f', preserve)
        self.assertLess(
            preserve.index(
                'test "$(/usr/bin/stat --format=\'%d:%i\' "$preserve_root")" '
                '= "$preserve_root_identity"'
            ),
            preserve.index('sudo /bin/rm -rf --one-file-system "$preserve_root"'),
        )
        self.assertNotIn("sudo rmdir", preserve)
        self.assertIn(
            'final_source_digest="$(/usr/bin/sha256sum "$FINAL_ARTIFACT"',
            preserve,
        )
        self.assertIn('test "$final_source_digest" = "$source_digest"', preserve)
        for forbidden in (
            "GITHUB_OUTPUT",
            "native_lifecycle_evidence",
            "native_ci_evidence",
            "--delete-data",
            "upload-artifact",
            "XDG_CONFIG_HOME",
            "DISPLAY",
            '"$preserve_root/config"',
            'cat "$collector_stdout"',
            'cat "$collector_stderr"',
            'cat "$preserve_stdout"',
            'cat "$preserve_stderr"',
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, preserve)

        contracts_start = source.index(
            "- name: Run portable Observer and Gateway contracts"
        )
        contracts_end = source.index("- name: Run native Windows Job contracts")
        contracts = source[contracts_start:contracts_end]
        self.assertIn("tests.test_canary_linux_service_absence", contracts)

        push = source[source.index("  push:\n"):source.index("  pull_request:\n")]
        pull_request = source[
            source.index("  pull_request:\n"):source.index("\npermissions:")
        ]
        for event, block in (("push", push), ("pull_request", pull_request)):
            with self.subTest(event=event):
                self.assertIn(
                    '- "scripts/canary_linux_service_absence.py"', block
                )
                self.assertIn(
                    '- "tests/test_canary_linux_service_absence.py"', block
                )

    def test_workflow_proves_observer_survives_ui_stop_before_preserve_cleanup(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        marker = "- name: Run audited AppImage preserve uninstall"
        start = source.index(marker)
        end = source.index("\n      - name:", start + 1)
        preserve = source[start:end]

        topology_copy = (
            '/bin/cp "$GITHUB_WORKSPACE/scripts/'
            'canary_linux_observer_topology.py"'
        )
        topology_run = "scripts.canary_linux_observer_topology --appimage"
        collector_run = '"$packaged_collector" desktop-service uninstall'
        wrapper_run = '"/proc/self/fd/$appimage_fd" --usagehub-uninstall'

        self.assertIn(topology_copy, preserve)
        self.assertIn(topology_run, preserve)
        self.assertLess(preserve.index(topology_copy), preserve.index(topology_run))
        self.assertLess(preserve.index(topology_run), preserve.index(collector_run))
        self.assertLess(preserve.index(topology_run), preserve.index(wrapper_run))
        self.assertIn(
            'topology_stdout="$preserve_root/topology.stdout"', preserve
        )
        self.assertIn(
            'topology_stderr="$preserve_root/topology.stderr"', preserve
        )
        self.assertIn('test ! -s "$topology_stdout"', preserve)
        self.assertIn('test ! -s "$topology_stderr"', preserve)
        self.assertIn(
            "appimage_observer_topology_failed category=$topology_category",
            preserve,
        )
        for topology_category in (
            "initial-absence",
            "launch",
            "runtime-before",
            "runtime-before-ui-exited",
            "runtime-before-service",
            "runtime-before-local",
            "runtime-before-service-after",
            "runtime-before-mapping",
            "runtime-before-local-authority",
            "runtime-before-local-socket",
            "runtime-before-local-connect-peer",
            "runtime-before-local-proc",
            "runtime-before-local-http",
            "runtime-before-local-revalidate",
            "runtime-before-local-cleanup",
            "stop",
            "runtime-after",
            "preserve",
            "final-absence",
        ):
            self.assertIn(f'topology_category="{topology_category}"', preserve)
        for forbidden in (
            "GITHUB_OUTPUT",
            "native_lifecycle",
            "releaseEligible",
            "upload-artifact",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, preserve)

        contracts_start = source.index(
            "- name: Run portable Observer and Gateway contracts"
        )
        contracts_end = source.index("- name: Run native Windows Job contracts")
        contracts = source[contracts_start:contracts_end]
        self.assertIn("tests.test_canary_linux_observer_topology", contracts)

        push = source[source.index("  push:\n"):source.index("  pull_request:\n")]
        pull_request = source[
            source.index("  pull_request:\n"):source.index("\npermissions:")
        ]
        for event, block in (("push", push), ("pull_request", pull_request)):
            with self.subTest(event=event):
                self.assertIn(
                    '- "scripts/canary_linux_observer_topology.py"', block
                )
                self.assertIn(
                    '- "tests/test_canary_linux_observer_topology.py"', block
                )

    def test_onefile_canary_documents_its_ephemeral_nonclaim_boundary(self):
        script = (ROOT / "scripts/canary_onefile_local_api.py").read_text(
            encoding="utf-8"
        )
        module_docstring = ast.get_docstring(ast.parse(script), clean=False) or ""
        for required_nonclaim in (
            "exclusive, disposable GitHub-hosted Linux/x64",
            "instantaneous diagnostic",
            "not lifecycle or release evidence",
            "no hostile concurrent same-UID namespace/PID-PGID reuse",
        ):
            with self.subTest(required_nonclaim=required_nonclaim):
                self.assertIn(required_nonclaim, module_docstring)

        workflow = WORKFLOW.read_text(encoding="utf-8")
        canary_start = workflow.index("- name: Run onefile Local API canary")
        canary_end = workflow.index("\n      - name:", canary_start + 1)
        canary = workflow[canary_start:canary_end]
        self.assertIn(
            "# Ephemeral topology gate only; no lifecycle or release evidence.",
            canary,
        )
        self.assertNotIn("GITHUB_OUTPUT", canary)
        self.assertNotIn("upload-artifact", canary)

    def test_workflow_portable_contracts_include_every_gateway_module(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        contracts = source[
            source.index("- name: Run portable Observer and Gateway contracts"):
            source.index("- name: Run native Windows Job contracts")
        ]
        modules = {
            line.strip().removesuffix("\\").strip()
            for line in contracts.splitlines()
            if line.strip().startswith("tests.")
        }
        expected = {
            f"tests.{path.stem}"
            for path in (ROOT / "tests").glob("test_gateway_*.py")
        }
        self.assertEqual(expected - modules, set())

    def test_gateway_performance_provenance_contract_stays_manual_diagnostic(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        contracts = source[
            source.index("- name: Run portable Observer and Gateway contracts"):
            source.index("- name: Run native Windows Job contracts")
        ]
        performance = source[source.index("\n  gateway-performance:"):]

        self.assertIn(
            "tests.test_gateway_performance_evidence_contract",
            contracts,
        )
        self.assertIn(
            "tests.test_gateway_performance_evidence_contract",
            performance,
        )
        self.assertIn(
            "name: gateway performance diagnostic (manual/non-blocking)",
            performance,
        )
        self.assertIn("if: github.event_name == 'workflow_dispatch'", performance)
        for promotion_marker in (
            "--enforce",
            "require-release",
            "release-evidence",
            "promotioneligible",
            "releaseeligible",
        ):
            with self.subTest(promotion_marker=promotion_marker):
                self.assertNotIn(promotion_marker, performance.casefold())

    def test_workflow_path_filters_cover_every_portable_contract_module(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        push = source[source.index("  push:\n"):source.index("  pull_request:\n")]
        pull_request = source[
            source.index("  pull_request:\n"):source.index("\npermissions:")
        ]
        contracts = source[
            source.index("- name: Run portable Observer and Gateway contracts"):
            source.index("- name: Run native Windows Job contracts")
        ]

        modules = []
        for line in contracts.splitlines():
            token = line.strip().removesuffix("\\").strip()
            if token.startswith("tests."):
                modules.append(token)
        self.assertGreaterEqual(len(modules), 30)

        for event, block in (("push", push), ("pull_request", pull_request)):
            patterns = []
            for line in block.splitlines():
                token = line.strip()
                if token.startswith('- "') and token.endswith('"'):
                    patterns.append(token[3:-1])
            for module in modules:
                path = module.replace(".", "/") + ".py"
                with self.subTest(event=event, module=module):
                    self.assertTrue(
                        any(fnmatchcase(path, pattern) for pattern in patterns),
                        f"{event} does not track {path}",
                    )

    def test_swift_automation_presentation_never_exposes_private_transport(self):
        production = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                ROOT
                / "swift_app/Sources/OpenUsageActivity/AutomationLogic.swift",
                ROOT
                / "swift_app/Sources/OpenUsageActivity/AutomationViews.swift",
            )
        )
        for forbidden in (
            "socketURL.path",
            "Read-only commands",
            "commandRow",
            "curl --unix-socket",
            "helper executable",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, production)

        focused_test = (
            ROOT
            / "swift_app/Tests/OpenUsageActivityTests/AutomationLogicTests.swift"
        ).read_text(encoding="utf-8")
        self.assertIn("Loaded state carries only safe aggregate Automation facts", focused_test)
        self.assertIn("func loadedStateCarriesOnlySafeFacts()", focused_test)
        for canary in ("socket", "localhost", "curl", "unix-socket", "helper"):
            with self.subTest(canary=canary):
                self.assertIn(f'"{canary}"', focused_test)

    def test_workflow_tracks_native_lifecycle_contract_without_claiming_execution(self):
        source = WORKFLOW.read_text(encoding="utf-8")
        push = source[source.index("  push:\n"):source.index("  pull_request:\n")]
        pull_request = source[
            source.index("  pull_request:\n"):source.index("\npermissions:")
        ]
        contracts = source[
            source.index("- name: Run portable Observer and Gateway contracts"):
            source.index("- name: Run native Windows Job contracts")
        ]
        for module in (
            "tests.test_lifecycle_state",
            "tests.test_managed_collector",
            "tests.test_native_lifecycle_evidence",
            "tests.test_native_lifecycle_runner",
        ):
            self.assertTrue(
                module in contracts,
                f"portable contracts do not run {module}",
            )
        for event, block in (("push", push), ("pull_request", pull_request)):
            self.assertIn('- "desktop/**"', block)
            self.assertIn('- "openusage_bar/**"', block)
            for path in (
                "docs/native-lifecycle-evidence.md",
                "docs/schemas/native-lifecycle-evidence-v1.schema.json",
                "scripts/native_lifecycle_evidence.py",
                "tests/test_lifecycle_state.py",
                "tests/test_managed_collector.py",
                "tests/test_native_lifecycle_evidence.py",
                "tests/test_native_lifecycle_runner.py",
            ):
                with self.subTest(event=event, path=path):
                    self.assertIn(f'- "{path}"', block)

        package = source[:source.index("\n  gateway-performance:")]
        self.assertNotIn("native_lifecycle_evidence.py generate", package)
        self.assertNotIn("native_lifecycle_evidence.py verify", package)
        handoff = source[
            source.index("- name: Assemble and verify local release handoff"):
            source.index("- name: Upload artifact and evidence")
        ]
        self.assertNotIn("native-lifecycle", handoff)
        upload = source[
            source.index("- name: Upload artifact and evidence"):
            source.index("\n  gateway-performance:")
        ]
        self.assertIn("path: ${{ steps.release_handoff.outputs.path }}", upload)
        self.assertNotIn("native-lifecycle", upload)


if __name__ == "__main__":
    unittest.main()
