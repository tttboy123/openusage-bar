from __future__ import annotations

import hashlib
import importlib.util
import json
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from openusage_bar.bounded_process import BoundedProcessError


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/distribution_trust_posture.py"
SCHEMA = ROOT / "docs/schemas/distribution-trust-posture-v1.schema.json"
class ToolRunner:
    """A system-boundary fake; the contract never depends on host tools."""

    def __init__(
        self,
        *,
        display: tuple[int, str, str] = (1, "", "not signed"),
        verify: tuple[int, str, str] = (1, "", "invalid"),
        stapler: tuple[int, str, str] = (1, "", "not stapled"),
        authenticode: tuple[int, str, str] = (0, "NotSigned\n", ""),
        mount_info: tuple[int, str, str] = (1, "", "unavailable"),
        error: Exception | None = None,
    ) -> None:
        self.display = display
        self.verify = verify
        self.stapler = stapler
        self.authenticode = authenticode
        self.mount_info = mount_info
        self.error = error
        self.calls: list[tuple[str, ...]] = []
        self.options: list[dict[str, object]] = []

    def __call__(self, command: list[str] | tuple[str, ...], **kwargs: object):
        argv = tuple(str(part) for part in command)
        self.calls.append(argv)
        self.options.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        lowered = tuple(part.casefold() for part in argv)
        joined = " ".join(lowered)
        if "codesign" in joined and "--display" in lowered:
            outcome = self.display
        elif "codesign" in joined and "--verify" in lowered:
            outcome = self.verify
        elif "stapler" in joined and "validate" in lowered:
            outcome = self.stapler
        elif "hdiutil" in joined and "info" in lowered:
            outcome = self.mount_info
        elif "powershell" in joined or "pwsh" in joined:
            outcome = self.authenticode
        else:
            raise AssertionError("unexpected trust-tool command")
        return subprocess.CompletedProcess(argv, outcome[0], outcome[1], outcome[2])


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "distribution_trust_posture_contract_target", SCRIPT
    )
    if spec is None or spec.loader is None:
        raise AssertionError("distribution trust posture script is missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture(root: Path, platform: str) -> tuple[Path, Path]:
    package_root = root / ("UsageHub.app" if platform == "mac" else "unpacked")
    package_root.mkdir()
    (package_root / "safe-member").write_bytes(b"packaged application")
    extension = {"mac": "dmg", "win": "exe", "linux": "AppImage"}[platform]
    artifact = root / f"UsageHub-1.2.3-{platform}-x64.{extension}"
    if platform == "mac":
        artifact.write_bytes(b"dmg" + b"\0" * 509 + b"koly" + b"\0" * 508)
    elif platform == "win":
        payload = bytearray(512)
        payload[:2] = b"MZ"
        payload[60:64] = (128).to_bytes(4, "little")
        payload[128:132] = b"PE\0\0"
        artifact.write_bytes(payload)
    else:
        payload = bytearray(64)
        payload[:7] = b"\x7fELF\x02\x01\x01"
        payload[18:20] = (62).to_bytes(2, "little")
        artifact.write_bytes(payload)
    return package_root, artifact


def _expected(
    platform: str,
    artifact: Path,
    *,
    signing: str,
    notarization: str,
) -> dict[str, object]:
    payload = artifact.read_bytes()
    return {
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


class DistributionTrustPostureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_module()

    def inspect(
        self,
        platform: str,
        package_root: Path,
        artifact: Path,
        runner: ToolRunner,
    ) -> dict[str, object]:
        return self.module.inspect_distribution_trust(
            platform=platform,
            package_root=package_root,
            artifact=artifact,
            runner=runner,
        )

    def test_macos_combines_display_verify_and_stapler_without_exposing_metadata(self):
        cases = (
            (
                "unsigned",
                (1, "", "code object is not signed at all PRIVATE_CANARY"),
                (1, "", "PRIVATE_CANARY"),
                (1, "", "does not have a ticket stapled PRIVATE_CANARY"),
                "unsigned",
                "not_stapled",
            ),
            (
                "ad_hoc_strict_valid",
                (0, "", "Signature=adhoc\nTeamIdentifier=PRIVATE_CANARY"),
                (0, "", ""),
                (0, "", "PRIVATE_CANARY"),
                "ad_hoc_strict_valid",
                "stapled",
            ),
            (
                "ad_hoc_strict_invalid",
                (0, "", "Signature=adhoc"),
                (1, "", "PRIVATE_CANARY"),
                (1, "", "not stapled"),
                "ad_hoc_strict_invalid",
                "not_stapled",
            ),
            (
                "developer_id_style_strict_valid",
                (0, "", "Authority=Developer ID Application: PRIVATE_CANARY"),
                (0, "", ""),
                (0, "", ""),
                "developer_id_style_strict_valid",
                "stapled",
            ),
            (
                "developer_id_style_strict_invalid",
                (0, "", "Authority=Developer ID Application: PRIVATE_CANARY"),
                (1, "", "PRIVATE_CANARY"),
                (1, "", "not stapled"),
                "developer_id_style_strict_invalid",
                "not_stapled",
            ),
            (
                "other_style_strict_valid",
                (0, "", "Authority=Apple Development: PRIVATE_CANARY"),
                (0, "", ""),
                (0, "", ""),
                "other_style_strict_valid",
                "stapled",
            ),
            (
                "other_style_strict_invalid",
                (0, "", "Authority=Apple Development: PRIVATE_CANARY"),
                (1, "", "PRIVATE_CANARY"),
                (1, "", "not stapled"),
                "other_style_strict_invalid",
                "not_stapled",
            ),
            (
                "unknown",
                (0, "", "malformed PRIVATE_CANARY"),
                (0, "", ""),
                (0, "", ""),
                "unknown",
                "stapled",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            package_root, artifact = _fixture(Path(directory), "mac")
            for name, display, verify, stapler, signing, notarization in cases:
                with self.subTest(name=name):
                    report = self.inspect(
                        "mac",
                        package_root,
                        artifact,
                        ToolRunner(
                            display=display,
                            verify=verify,
                            stapler=stapler,
                        ),
                    )
                    self.assertEqual(
                        report,
                        _expected(
                            "mac",
                            artifact,
                            signing=signing,
                            notarization=notarization,
                        ),
                    )
                    encoded = json.dumps(report, sort_keys=True)
                    self.assertNotIn("PRIVATE_CANARY", encoded)
                    self.assertNotIn(str(package_root), encoded)
                    self.assertNotIn(str(artifact.parent), encoded)

    def test_command_errors_timeouts_and_oversized_output_fail_to_unknown_safely(self):
        failures = (
            RuntimeError("COMMAND_EXCEPTION_PRIVATE_CANARY"),
            TimeoutError("TIMEOUT_PRIVATE_CANARY"),
            BoundedProcessError("output_overflow"),
        )
        with tempfile.TemporaryDirectory() as directory:
            package_root, artifact = _fixture(Path(directory), "mac")
            for failure in failures:
                with self.subTest(failure=type(failure).__name__):
                    report = self.inspect(
                        "mac",
                        package_root,
                        artifact,
                        ToolRunner(error=failure),
                    )
                    self.assertEqual(report["platformCodeSigning"], "unknown")
                    self.assertEqual(report["platformNotarization"], "unknown")
                    self.assertFalse(report["releaseEligible"])
                    encoded = json.dumps(report, sort_keys=True)
                    self.assertNotIn("PRIVATE_CANARY", encoded)
                    self.assertNotIn("COMMAND_EXCEPTION", encoded)
                    self.assertNotIn("TIMEOUT", encoded)

            oversized = "OVERSIZED_PRIVATE_CANARY" * 100_000
            report = self.inspect(
                "mac",
                package_root,
                artifact,
                ToolRunner(
                    display=(0, "", oversized),
                    verify=(0, "", ""),
                    stapler=(0, oversized, ""),
                ),
            )
            self.assertEqual(report["platformCodeSigning"], "unknown")
            self.assertEqual(report["platformNotarization"], "unknown")
            self.assertNotIn("PRIVATE_CANARY", json.dumps(report, sort_keys=True))

            output = Path(directory) / "unknown.json"
            with self.assertRaisesRegex(
                ValueError, "^distribution trust inspection failed$"
            ):
                self.module.write_distribution_trust_report(
                    platform="mac",
                    package_root=package_root,
                    artifact=artifact,
                    output=output,
                    runner=ToolRunner(error=TimeoutError("PRIVATE_CANARY")),
                )
            self.assertFalse(output.exists())

    def test_platform_and_container_format_are_derived_from_the_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for platform in ("mac", "win", "linux"):
                with self.subTest(platform=platform):
                    fixture_root = root / platform
                    fixture_root.mkdir()
                    package_root, artifact = _fixture(fixture_root, platform)
                    artifact.write_bytes(b"not-a-container")
                    runner = ToolRunner()
                    with self.assertRaisesRegex(
                        ValueError, "^distribution trust inspection failed$"
                    ):
                        self.inspect(platform, package_root, artifact, runner)
                    self.assertEqual(runner.calls, [])

            linux_root = root / "wrong-platform"
            linux_root.mkdir()
            package_root, linux_artifact = _fixture(linux_root, "linux")
            runner = ToolRunner()
            with self.assertRaisesRegex(
                ValueError, "^distribution trust inspection failed$"
            ):
                self.inspect("win", package_root, linux_artifact, runner)
            self.assertEqual(runner.calls, [])

    def test_artifact_identity_is_real_closed_and_changes_with_file_content(self):
        with tempfile.TemporaryDirectory() as directory:
            package_root, artifact = _fixture(Path(directory), "linux")
            runner = ToolRunner(error=AssertionError("linux must not invoke tools"))

            report = self.inspect("linux", package_root, artifact, runner)

            self.assertEqual(
                report,
                _expected(
                    "linux",
                    artifact,
                    signing="not_applicable",
                    notarization="not_applicable",
                ),
            )
            self.assertEqual(runner.calls, [])
            self.assertEqual(set(report), {
                "artifact",
                "object",
                "platformCodeSigning",
                "platformNotarization",
                "policy",
                "provenanceAttestation",
                "releaseEligible",
                "schemaVersion",
                "targetPlatform",
            })

            changed_payload = artifact.read_bytes() + b"changed"
            artifact.write_bytes(changed_payload)
            changed = self.inspect("linux", package_root, artifact, runner)
            self.assertEqual(changed["artifact"], {
                "name": artifact.name,
                "sha256": hashlib.sha256(changed_payload).hexdigest(),
                "sizeBytes": len(changed_payload),
            })
            self.assertNotEqual(changed["artifact"], report["artifact"])

    def test_artifact_and_package_root_symlinks_are_rejected_before_tool_use(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_root, artifact = _fixture(root, "linux")
            artifact_link = root / "linked.AppImage"
            artifact_link.symlink_to(artifact.name)
            package_link = root / "linked-package"
            package_link.symlink_to(package_root.name, target_is_directory=True)

            for candidate_root, candidate_artifact in (
                (package_root, artifact_link),
                (package_link, artifact),
            ):
                runner = ToolRunner()
                with self.subTest(
                    package_root=candidate_root.name,
                    artifact=candidate_artifact.name,
                ), self.assertRaisesRegex(
                    ValueError, "^distribution trust inspection failed$"
                ) as raised:
                    self.inspect(
                        "linux", candidate_root, candidate_artifact, runner
                    )
                self.assertEqual(runner.calls, [])
                self.assertNotIn(str(root), str(raised.exception))

    def test_windows_authenticode_status_is_normalized_to_a_closed_enum(self):
        cases = (
            ("Valid\n", 0, "valid"),
            ("NotSigned\n", 0, "unsigned"),
            ("HashMismatch\n", 0, "invalid"),
            ("NotTrusted\n", 0, "invalid"),
            ("UnknownError\n", 0, "invalid"),
            ("Valid\nPRIVATE_CANARY", 0, "unknown"),
            ("PRIVATE_CANARY", 0, "unknown"),
            ("Valid\n", 1, "unknown"),
        )
        with tempfile.TemporaryDirectory() as directory:
            package_root, artifact = _fixture(Path(directory), "win")
            for output, returncode, expected_signing in cases:
                with self.subTest(output=output, returncode=returncode):
                    report = self.inspect(
                        "win",
                        package_root,
                        artifact,
                        ToolRunner(
                            authenticode=(returncode, output, "PRIVATE_CANARY")
                        ),
                    )
                    self.assertEqual(
                        report,
                        _expected(
                            "win",
                            artifact,
                            signing=expected_signing,
                            notarization="not_applicable",
                        ),
                    )
                    self.assertNotIn(
                        "PRIVATE_CANARY", json.dumps(report, sort_keys=True)
                    )

    def test_platform_tool_argv_and_process_bounds_are_fixed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_root, artifact = _fixture(root, "mac")
            mac_runner = ToolRunner(
                display=(0, "", "Signature=adhoc"),
                verify=(0, "", ""),
                stapler=(0, "", ""),
            )

            self.inspect("mac", package_root, artifact, mac_runner)

            self.assertEqual(
                mac_runner.calls,
                [
                    (
                        "/usr/bin/codesign",
                        "--display",
                        "--verbose=4",
                        str(package_root),
                    ),
                    (
                        "/usr/bin/codesign",
                        "--verify",
                        "--deep",
                        "--strict",
                        str(package_root),
                    ),
                    (
                        "/usr/bin/xcrun",
                        "stapler",
                        "validate",
                        str(artifact),
                    ),
                ],
            )
            expected_options = {
                "encoding": "utf-8",
                "errors": "replace",
                "shell": False,
                "stderr_limit": 64 * 1024,
                "stdout_limit": 64 * 1024,
                "text": True,
                "timeout": 10.0,
            }
            self.assertEqual(
                mac_runner.options,
                [expected_options, expected_options, expected_options],
            )

            windows_fixture = root / "windows"
            windows_fixture.mkdir()
            win_root, win_artifact = _fixture(windows_fixture, "win")
            win_runner = ToolRunner(authenticode=(0, "Valid\n", ""))
            self.inspect("win", win_root, win_artifact, win_runner)
            self.assertEqual(len(win_runner.calls), 1)
            win_command = win_runner.calls[0]
            self.assertEqual(
                win_command[0],
                r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            )
            self.assertIn(
                "Microsoft.PowerShell.Security\\Get-AuthenticodeSignature",
                " ".join(win_command),
            )
            self.assertEqual(win_command[-1], str(win_artifact))
            self.assertNotIn("Invoke-Expression", " ".join(win_command))
            self.assertEqual(win_runner.options, [expected_options])

    def test_persisted_macos_report_requires_the_same_read_only_mounted_dmg(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_root, artifact = _fixture(root, "mac")
            mount_info = plistlib.dumps(
                {
                    "images": [
                        {
                            "image-path": str(artifact.resolve()),
                            "writeable": False,
                            "system-entities": [
                                {"mount-point": str(package_root.parent.resolve())}
                            ],
                        }
                    ]
                }
            ).decode("utf-8")
            output = root / "trust.json"
            runner = ToolRunner(
                display=(0, "", "Signature=adhoc"),
                verify=(1, "", "invalid"),
                stapler=(1, "", "not stapled"),
                mount_info=(0, mount_info, ""),
            )

            report = self.module.write_distribution_trust_report(
                platform="mac",
                package_root=package_root,
                artifact=artifact,
                output=output,
                runner=runner,
            )

            self.assertEqual(
                report["platformCodeSigning"],
                "ad_hoc_strict_invalid",
            )
            self.assertTrue(output.is_file())
            self.assertEqual(
                runner.calls[0],
                ("/usr/bin/hdiutil", "info", "-plist"),
            )
            self.assertNotIn(str(root), output.read_text(encoding="utf-8"))

            output.unlink()
            writable_info = plistlib.dumps(
                {
                    "images": [
                        {
                            "image-path": str(artifact.resolve()),
                            "writeable": True,
                            "system-entities": [
                                {"mount-point": str(package_root.parent.resolve())}
                            ],
                        }
                    ]
                }
            ).decode("utf-8")
            with self.assertRaisesRegex(
                ValueError, "^distribution trust inspection failed$"
            ):
                self.module.write_distribution_trust_report(
                    platform="mac",
                    package_root=package_root,
                    artifact=artifact,
                    output=output,
                    runner=ToolRunner(mount_info=(0, writable_info, "")),
                )
            self.assertFalse(output.exists())

    def test_linux_cli_writes_canonical_closed_json_and_rejects_forged_trust(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_root, artifact = _fixture(root, "linux")
            output = root / "trust.json"
            command = [
                sys.executable,
                str(SCRIPT),
                "inspect",
                "--platform",
                "linux",
                "--package-root",
                str(package_root),
                "--artifact",
                str(artifact),
                "--output",
                str(output),
            ]

            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual((completed.returncode, completed.stdout, completed.stderr), (0, "", ""))
            expected = _expected(
                "linux",
                artifact,
                signing="not_applicable",
                notarization="not_applicable",
            )
            canonical = json.dumps(
                expected,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ) + "\n"
            self.assertEqual(output.read_text(encoding="utf-8"), canonical)

            forged_output = root / "forged.json"
            forged = subprocess.run(
                command[:-1]
                + [
                    str(forged_output),
                    "--platform-code-signing",
                    "valid",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertNotEqual(forged.returncode, 0)
            self.assertFalse(forged_output.exists())

    def test_documented_schema_is_closed_and_never_grants_release_eligibility(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

        self.assertEqual(
            schema["$schema"],
            "https://json-schema.org/draft/2020-12/schema",
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["required"]),
            {
                "artifact",
                "object",
                "platformCodeSigning",
                "platformNotarization",
                "policy",
                "provenanceAttestation",
                "releaseEligible",
                "schemaVersion",
                "targetPlatform",
            },
        )
        self.assertEqual(
            schema["properties"]["releaseEligible"],
            {"const": False, "type": "boolean"},
        )
        self.assertFalse(schema["properties"]["artifact"]["additionalProperties"])

    def test_verify_cli_recomputes_artifact_binding_and_rejects_report_forgery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_root, artifact = _fixture(root, "linux")
            report = root / "trust.json"
            inspect_command = [
                sys.executable,
                str(SCRIPT),
                "inspect",
                "--platform",
                "linux",
                "--package-root",
                str(package_root),
                "--artifact",
                str(artifact),
                "--output",
                str(report),
            ]
            inspected = subprocess.run(
                inspect_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            verify_command = [
                sys.executable,
                str(SCRIPT),
                "verify",
                "--report",
                str(report),
                "--platform",
                "linux",
                "--artifact",
                str(artifact),
            ]

            verified = subprocess.run(
                verify_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual((verified.returncode, verified.stdout, verified.stderr), (0, "", ""))

            payload = json.loads(report.read_text(encoding="utf-8"))
            payload["releaseEligible"] = True
            report.write_text(
                json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            forged = subprocess.run(
                verify_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(forged.returncode, 1)
            self.assertEqual(
                forged.stderr,
                "distribution_trust_posture_invalid\n",
            )
            self.assertNotIn(str(root), forged.stderr)

            report.write_text(
                '{"artifact":{},"artifact":{}}\n',
                encoding="utf-8",
            )
            duplicate = subprocess.run(
                verify_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(duplicate.returncode, 1)
            self.assertEqual(
                duplicate.stderr,
                "distribution_trust_posture_invalid\n",
            )

    def test_cli_rejects_symlinked_reports_artifacts_and_outputs_without_leaks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_root, artifact = _fixture(root, "linux")
            report = root / "trust.json"
            inspect_command = [
                sys.executable,
                str(SCRIPT),
                "inspect",
                "--platform",
                "linux",
                "--package-root",
                str(package_root),
                "--artifact",
                str(artifact),
                "--output",
                str(report),
            ]
            self.assertEqual(subprocess.run(inspect_command, check=False).returncode, 0)
            report_link = root / "trust-link.json"
            report_link.symlink_to(report.name)
            artifact_link = root / "artifact-link.AppImage"
            artifact_link.symlink_to(artifact.name)

            for candidate_report, candidate_artifact in (
                (report_link, artifact),
                (report, artifact_link),
            ):
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "verify",
                        "--report",
                        str(candidate_report),
                        "--platform",
                        "linux",
                        "--artifact",
                        str(candidate_artifact),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(
                    completed.stderr,
                    "distribution_trust_posture_invalid\n",
                )
                self.assertNotIn(str(root), completed.stderr)

            protected = root / "protected.json"
            protected.write_text("protected", encoding="utf-8")
            output_link = root / "output-link.json"
            output_link.symlink_to(protected.name)
            rejected_output = subprocess.run(
                inspect_command[:-1] + [str(output_link)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(rejected_output.returncode, 1)
            self.assertEqual(
                rejected_output.stderr,
                "distribution_trust_posture_invalid\n",
            )
            self.assertEqual(protected.read_text(encoding="utf-8"), "protected")

    def test_verify_rejects_path_and_raw_output_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_root, artifact = _fixture(root, "linux")
            payload = self.inspect("linux", package_root, artifact, ToolRunner())
            report = root / "trust.json"
            verify_command = [
                sys.executable,
                str(SCRIPT),
                "verify",
                "--report",
                str(report),
                "--platform",
                "linux",
                "--artifact",
                str(artifact),
            ]
            for field in ("path", "rawOutput"):
                with self.subTest(field=field):
                    forged = dict(payload)
                    forged[field] = f"PRIVATE_CANARY_{root}"
                    report.write_text(
                        json.dumps(
                            forged,
                            ensure_ascii=True,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    completed = subprocess.run(
                        verify_command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(completed.returncode, 1)
                    self.assertEqual(
                        completed.stderr,
                        "distribution_trust_posture_invalid\n",
                    )
                    self.assertNotIn("PRIVATE_CANARY", completed.stderr)
                    self.assertNotIn(str(root), completed.stderr)


if __name__ == "__main__":
    unittest.main()
