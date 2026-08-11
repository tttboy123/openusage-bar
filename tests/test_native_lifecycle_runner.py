from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


SOURCE_COMMIT = "a" * 40


def _checks() -> dict[str, str]:
    return {
        "install": "passed",
        "firstRun": "passed",
        "observeDefault": "passed",
        "serviceRegistered": "passed",
        "uninstallPreserve": "passed",
        "serviceRemoved": "passed",
        "reinstall": "passed",
        "uninstallDelete": "passed",
        "finalStateRemoved": "passed",
    }


def _observations() -> dict[str, object]:
    return {
        "checks": _checks(),
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


class RecordingExecutor:
    execution_class = "unit_test_injected"

    def __init__(self, observations: dict[str, object] | None = None) -> None:
        self.observations = observations or _observations()
        self.calls: list[tuple[str, str, Path, str]] = []

    def execute(
        self,
        *,
        platform: str,
        arch: str,
        artifact: Path,
        artifact_sha256: str,
    ) -> dict[str, object]:
        self.calls.append((platform, arch, artifact, artifact_sha256))
        return self.observations


class MutatingExecutor(RecordingExecutor):
    def execute(self, **kwargs: object) -> dict[str, object]:
        artifact = kwargs["artifact"]
        assert isinstance(artifact, Path)
        artifact.write_bytes(b"mutated final container")
        return super().execute(**kwargs)


class NativeLifecycleRunnerTests(unittest.TestCase):
    def test_windows_file_signature_ignores_only_unstable_ctime(self) -> None:
        from scripts.native_lifecycle_evidence import _stable_metadata_signature

        baseline = SimpleNamespace(
            st_dev=4,
            st_ino=8,
            st_size=16,
            st_mtime_ns=32,
            st_ctime_ns=64,
        )
        ctime_drift = SimpleNamespace(**{**vars(baseline), "st_ctime_ns": 65})
        mtime_drift = SimpleNamespace(**{**vars(baseline), "st_mtime_ns": 33})

        self.assertEqual(
            _stable_metadata_signature(baseline, platform_name="nt"),
            _stable_metadata_signature(ctime_drift, platform_name="nt"),
        )
        self.assertNotEqual(
            _stable_metadata_signature(baseline, platform_name="nt"),
            _stable_metadata_signature(mtime_drift, platform_name="nt"),
        )
        self.assertNotEqual(
            _stable_metadata_signature(baseline, platform_name="posix"),
            _stable_metadata_signature(ctime_drift, platform_name="posix"),
        )

    def test_validator_rejects_boolean_counters_and_unknown_fields(self) -> None:
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            validate_lifecycle_record,
        )

        base = {
            "schemaVersion": "native-lifecycle-evidence/v1",
            "object": "native.lifecycle",
            "synthetic": False,
            "releaseEligible": False,
            "observedAt": "2026-08-11T01:02:03.000000Z",
            "sourceCommit": SOURCE_COMMIT,
            "target": {
                "platform": "win",
                "arch": "x64",
                "serviceManager": "task_scheduler",
            },
            "artifact": {
                "name": "UsageHub-0.8.6-win-x64.exe",
                "sha256": "b" * 64,
                "sizeBytes": 128,
            },
            **_observations(),
        }
        cases = (
            {**base, "privacy": {
                "providerCredentialReads": False,
                "providerNetworkCalls": 0,
            }},
            {**base, "privatePath": "/private/runner"},
        )

        for record in cases:
            with self.subTest(keys=tuple(record)):
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "record_invalid"
                ):
                    validate_lifecycle_record(record)

    def test_generate_binds_real_final_container_and_writes_canonical_record(self) -> None:
        from scripts.native_lifecycle_evidence import generate_lifecycle_evidence

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "UsageHub-0.8.6-win-x64.exe"
            artifact.write_bytes(b"final NSIS bytes")
            output = root / "native-lifecycle.json"
            executor = RecordingExecutor()

            record = generate_lifecycle_evidence(
                platform="win",
                arch="x64",
                artifact=artifact,
                source_commit=SOURCE_COMMIT,
                output=output,
                executor=executor,
                clock=lambda: datetime(
                    2026, 8, 11, 1, 2, 3, tzinfo=timezone.utc
                ),
            )

            digest = hashlib.sha256(b"final NSIS bytes").hexdigest()
            self.assertEqual(
                record,
                {
                    "schemaVersion": "native-lifecycle-evidence/v1",
                    "object": "native.lifecycle",
                    "synthetic": False,
                    "releaseEligible": False,
                    "observedAt": "2026-08-11T01:02:03.000000Z",
                    "sourceCommit": SOURCE_COMMIT,
                    "target": {
                        "platform": "win",
                        "arch": "x64",
                        "serviceManager": "task_scheduler",
                    },
                    "artifact": {
                        "name": artifact.name,
                        "sha256": digest,
                        "sizeBytes": len(b"final NSIS bytes"),
                    },
                    **_observations(),
                },
            )
            self.assertEqual(
                output.read_text(encoding="ascii"),
                json.dumps(
                    record,
                    allow_nan=False,
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )
            self.assertEqual(
                executor.calls,
                [("win", "x64", artifact, digest)],
            )

    def test_generate_rejects_non_real_executor_without_writing_report(self) -> None:
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            generate_lifecycle_evidence,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "UsageHub-0.8.6-linux-x86_64.AppImage"
            artifact.write_bytes(b"final AppImage bytes")
            output = root / "private-machine-name.json"
            executor = RecordingExecutor()
            executor.execution_class = "untrusted_self_report"

            with self.assertRaisesRegex(
                LifecycleEvidenceError, "execution_not_real"
            ) as raised:
                generate_lifecycle_evidence(
                    platform="linux",
                    arch="x64",
                    artifact=artifact,
                    source_commit=SOURCE_COMMIT,
                    output=output,
                    executor=executor,
                )

            self.assertFalse(output.exists())
            self.assertNotIn(str(root), str(raised.exception))

    def test_generate_rejects_failed_or_mutating_execution_without_report(self) -> None:
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            generate_lifecycle_evidence,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "UsageHub-0.8.6-win-x64.exe"
            artifact.write_bytes(b"final NSIS bytes")
            failed = _observations()
            failed["checks"] = {**_checks(), "firstRun": "failed"}
            for name, executor, reason in (
                ("failed", RecordingExecutor(failed), "record_invalid"),
                ("mutated", MutatingExecutor(), "artifact_changed"),
            ):
                with self.subTest(name=name):
                    artifact.write_bytes(b"final NSIS bytes")
                    output = root / f"{name}.json"
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, reason
                    ):
                        generate_lifecycle_evidence(
                            platform="win",
                            arch="x64",
                            artifact=artifact,
                            source_commit=SOURCE_COMMIT,
                            output=output,
                            executor=executor,
                        )
                    self.assertFalse(output.exists())

    def test_verify_rehashes_container_binds_target_and_requires_canonical_report(self) -> None:
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            generate_lifecycle_evidence,
            verify_lifecycle_evidence,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "UsageHub-0.8.6-linux-x86_64.AppImage"
            artifact.write_bytes(b"final AppImage bytes")
            output = root / "native-lifecycle.json"
            expected = generate_lifecycle_evidence(
                platform="linux",
                arch="x64",
                artifact=artifact,
                source_commit=SOURCE_COMMIT,
                output=output,
                executor=RecordingExecutor(),
            )

            self.assertEqual(
                verify_lifecycle_evidence(
                    report=output,
                    artifact=artifact,
                    expected_source_commit=SOURCE_COMMIT,
                    expected_platform="linux",
                    expected_arch="x64",
                ),
                expected,
            )

            output.write_text(
                json.dumps(expected, sort_keys=True), encoding="ascii"
            )
            with self.assertRaisesRegex(
                LifecycleEvidenceError, "report_not_canonical"
            ):
                verify_lifecycle_evidence(
                    report=output,
                    artifact=artifact,
                    expected_source_commit=SOURCE_COMMIT,
                    expected_platform="linux",
                    expected_arch="x64",
                )

    def test_native_executor_delegates_to_an_independent_platform_driver(self) -> None:
        from scripts.native_lifecycle_evidence import NativeLifecycleExecutor

        class ExternalDriver:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, Path, str]] = []

            def execute(
                self,
                *,
                platform: str,
                arch: str,
                artifact: Path,
                artifact_sha256: str,
            ) -> dict[str, object]:
                self.calls.append(
                    (platform, arch, artifact, artifact_sha256)
                )
                return _observations()

        driver = ExternalDriver()
        executor = NativeLifecycleExecutor(
            driver=driver,
            host_platform="linux",
            host_machine="x86_64",
        )
        outcome = executor.execute(
            platform="linux",
            arch="x64",
            artifact=Path("/tmp/UsageHub-0.8.6-linux-x86_64.AppImage"),
            artifact_sha256="b" * 64,
        )

        self.assertEqual(outcome, _observations())
        self.assertEqual(
            driver.calls,
            [
                (
                    "linux",
                    "x64",
                    Path(
                        "/tmp/UsageHub-0.8.6-linux-x86_64.AppImage"
                    ),
                    "b" * 64,
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
