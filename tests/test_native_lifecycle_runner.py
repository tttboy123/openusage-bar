from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields
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


class WindowsFailureFixture:
    def __init__(
        self,
        root: Path,
        *,
        foreign_token: bool = False,
        cleanup_failure: bool = False,
        observation_failure: bool = False,
        ready_first: bool = False,
    ) -> None:
        from scripts.native_lifecycle_evidence import (
            NativeLedgerState,
            NativeListenerState,
            NativePackagePaths,
            NativePathState,
            NativeProcessResult,
            NativeProfilePaths,
            NativeServiceState,
        )

        self.root = root
        self.artifact = root / "UsageHub-0.8.6-win-x64.exe"
        self.artifact.write_bytes(b"audited final NSIS container")
        self.artifact_sha256 = hashlib.sha256(self.artifact.read_bytes()).hexdigest()
        self.run_directory = root / "native-run"
        self.execution_copy = self.run_directory / self.artifact.name
        self.sentinel = self.run_directory / "outside-product-sentinel.bin"
        self.install_root = root / "installed" / "UsageHub"
        self.installed_app = self.install_root / "UsageHub.exe"
        self.installed_collector = (
            self.install_root
            / "resources"
            / "collector"
            / "openusage-collector.exe"
        )
        self.uninstaller = self.install_root / "Uninstall UsageHub.exe"
        runtime_root = root / "local-app-data" / "openusage-bar"
        self.profile = NativeProfilePaths(
            root / "profile" / ".local" / "state" / "openusage-bar",
            root / "profile" / ".config" / "openusage-bar",
            runtime_root,
            runtime_root.parent / "openusage-bar-task.xml",
        )
        self.package = NativePackagePaths(
            self.install_root,
            self.installed_app,
            self.uninstaller,
            self.installed_collector,
        )
        token = (
            root / "PRIVATE_FOREIGN" / "openusage-bar" / "api.token"
            if foreign_token
            else runtime_root / "api.token"
        )
        command = (
            str(self.installed_collector),
            "daemon",
            "--interval",
            "300",
            "--api-transport",
            "tcp",
            "--api-port",
            "17821",
            "--api-token-path",
            str(token),
        )
        self.events: list[tuple[object, ...]] = []
        self.cleanup_failure = cleanup_failure
        self.observation_failure = observation_failure
        self.observation_failed = False
        self.ledger_calls = 0
        ready_service = NativeServiceState(True, True, command)
        pending_service = NativeServiceState(False, False, None)
        self.service_states = iter(
            (
                (pending_service, ready_service, pending_service, ready_service,
                 pending_service)
                if ready_first
                else (
                    pending_service,
                    pending_service,
                    ready_service,
                    pending_service,
                    pending_service,
                    ready_service,
                    pending_service,
                )
            )
        )
        ready_listener = NativeListenerState(True, True)
        pending_listener = NativeListenerState(False, False)
        self.local_listeners = iter(
            (
                (pending_listener, ready_listener, pending_listener,
                 ready_listener, pending_listener)
                if ready_first
                else (
                    pending_listener,
                    pending_listener,
                    ready_listener,
                    pending_listener,
                    pending_listener,
                    ready_listener,
                    pending_listener,
                )
            )
        )
        ledger = NativeLedgerState(
            True, "ledger:file", 8192, "d" * 64, True, 7,
        )
        self.ledgers = iter(
            (
                NativeLedgerState(False, None, 0, None, False, 0),
                ledger,
                ledger,
                ledger,
                NativeLedgerState(False, None, 0, None, False, 0),
            )
        )
        self.clock = iter((0.0, 0.1, 1.0, 1.1))
        missing = NativePathState(False, "missing", 0, None, 0, None)
        self.path_states = {
            "execution_copy": NativePathState(
                True,
                "file",
                self.artifact.stat().st_size,
                self.artifact_sha256,
                0o700,
                "run:installer",
            ),
            "installed_app": NativePathState(
                True, "file", 8192, "e" * 64, 0o700, "install:desktop",
            ),
            "installed_uninstaller": NativePathState(
                True, "file", 4096, "f" * 64, 0o700, "install:uninstaller",
            ),
            "installed_collector": NativePathState(
                True, "file", 4096, "c" * 64, 0o700, "install:collector",
            ),
            "sentinel": NativePathState(
                True,
                "file",
                self.artifact.stat().st_size,
                self.artifact_sha256,
                0o600,
                "run:sentinel",
            ),
            **{
                purpose: missing
                for purpose in (
                    "fresh_state_root",
                    "fresh_config_root",
                    "fresh_runtime_root",
                    "fresh_task_definition",
                    "fresh_install_root",
                    "fresh_installed_app",
                    "fresh_installed_uninstaller",
                    "fresh_installed_collector",
                    "fresh_execution_copy",
                    "fresh_sentinel",
                    "gateway_token",
                    "gateway_cache",
                    "gateway_telemetry",
                    "preserve_gateway_token",
                    "preserve_gateway_cache",
                    "preserve_gateway_telemetry",
                    "preserve_installed_app",
                    "preserve_install_root",
                    "delete_installed_app",
                    "delete_install_root",
                    "state_root",
                    "config_root",
                    "runtime_root",
                    "task_definition",
                )
            },
        }
        self.NativeProcessResult = NativeProcessResult

    def dependencies(self):
        from scripts.native_lifecycle_evidence import NativeLifecycleDependencies

        return NativeLifecycleDependencies(
            make_run_directory=self.make_run_directory,
            inspect_path=self.inspect_path,
            copy_file=self.copy_file,
            set_file_mode=self.set_file_mode,
            remove_path=self.remove_path,
            start_process=self.start_process,
            run_process=self.run_process,
            stop_process=self.stop_process,
            read_registry_value=self.read_registry_value,
            profile_paths=self.profile_paths,
            package_paths=self.package_paths,
            inspect_service=self.inspect_service,
            inspect_listener=self.inspect_listener,
            inspect_ledger=self.inspect_ledger,
            network_events=lambda: (),
            credential_events=lambda: (),
            monotonic=self.monotonic,
            wait=lambda _seconds: None,
        )

    def make_run_directory(self, _platform: str, _arch: str) -> Path:
        self.run_directory.mkdir()
        return self.run_directory

    def inspect_path(self, purpose: str, _path: Path):
        return self.path_states[purpose]

    @staticmethod
    def copy_file(source: Path, destination: Path) -> None:
        destination.write_bytes(source.read_bytes())

    @staticmethod
    def set_file_mode(path: Path, mode: int) -> None:
        path.chmod(mode)

    def remove_path(self, path: Path) -> None:
        self.events.append(("remove_path", path))
        if self.cleanup_failure:
            raise RuntimeError("PRIVATE_CLEANUP_PATH")

    def start_process(self, _argv: tuple[str, ...]) -> object:
        handle = "active-desktop-handle"
        self.events.append(("start_process", handle))
        return handle

    def run_process(self, _argv: tuple[str, ...], _timeout: float):
        return self.NativeProcessResult(
            0, b'{"synthetic":false,"checks":"passed"}', b"PRIVATE_STDERR", False
        )

    def stop_process(self, handle: object) -> None:
        self.events.append(("stop_process", handle))

    def read_registry_value(self, _key: str, name: str) -> str:
        return {
            "InstallLocation": str(self.install_root),
            "UninstallString": str(self.uninstaller),
        }[name]

    def profile_paths(self, _platform: str):
        return self.profile

    def package_paths(self, _platform: str, _profile):
        return self.package

    def inspect_service(self, _platform: str):
        return next(self.service_states)

    def inspect_listener(self, _platform: str, namespace: str):
        from scripts.native_lifecycle_evidence import NativeListenerState

        if namespace == "gateway":
            return NativeListenerState(False, False)
        return next(self.local_listeners)

    def inspect_ledger(self, _platform: str):
        self.ledger_calls += 1
        if (
            self.observation_failure
            and self.ledger_calls > 1
            and not self.observation_failed
        ):
            self.observation_failed = True
            raise RuntimeError("PRIVATE_OBSERVATION_PATH")
        return next(self.ledgers)

    def monotonic(self) -> float:
        return next(self.clock)


class NativeLifecycleRunnerTests(unittest.TestCase):
    def test_default_executor_owns_and_closes_zero_arg_host_dependencies_context(
        self,
    ) -> None:
        from inspect import signature
        from unittest.mock import patch

        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativeLifecycleDependencies,
            NativeLifecycleExecutor,
            generate_lifecycle_evidence,
            native_lifecycle_dependencies_for_host,
        )

        self.assertEqual(tuple(signature(native_lifecycle_dependencies_for_host).parameters), ())
        for forbidden in ("executor", "dependencies", "artifact_sha256"):
            self.assertNotIn(forbidden, signature(generate_lifecycle_evidence).parameters)

        events: list[object] = []

        def fail_make_run_directory(_platform: str, _arch: str) -> Path:
            events.append("make_run_directory")
            raise RuntimeError("PRIVATE_LOW_LEVEL_FAILURE")

        noop = lambda *args, **kwargs: None
        dependencies = NativeLifecycleDependencies(
            make_run_directory=fail_make_run_directory,
            inspect_path=noop,
            copy_file=noop,
            set_file_mode=noop,
            remove_path=noop,
            start_process=noop,
            run_process=noop,
            stop_process=noop,
            read_registry_value=noop,
            profile_paths=noop,
            package_paths=noop,
            inspect_service=noop,
            inspect_listener=noop,
            inspect_ledger=noop,
            network_events=noop,
            credential_events=noop,
            monotonic=noop,
            wait=noop,
        )

        class DependencyContext:
            def __enter__(self) -> NativeLifecycleDependencies:
                events.append("enter")
                return dependencies

            def __exit__(self, exc_type, _exc, _traceback) -> bool:
                events.append(("exit", exc_type is not None))
                return False

        def dependency_context() -> DependencyContext:
            events.append("factory")
            return DependencyContext()

        artifact = (
            Path(tempfile.gettempdir()).resolve()
            / "UsageHub-0.8.6-linux-x86_64.AppImage"
        )
        with patch(
            "scripts.native_lifecycle_evidence.native_lifecycle_dependencies_for_host",
            side_effect=dependency_context,
        ):
            executor = NativeLifecycleExecutor(
                host_platform="linux",
                host_machine="x86_64",
            )
            for platform, arch in (("win", "x64"), ("linux", "arm64")):
                with self.subTest(platform=platform, arch=arch), self.assertRaisesRegex(
                    LifecycleEvidenceError,
                    "host_invalid",
                ):
                    executor.execute(
                        platform=platform,
                        arch=arch,
                        artifact=artifact,
                        artifact_sha256="a" * 64,
                    )
            self.assertEqual(events, [])

            with self.assertRaisesRegex(LifecycleEvidenceError, "driver_failed"):
                executor.execute(
                    platform="linux",
                    arch="x64",
                    artifact=artifact,
                    artifact_sha256="a" * 64,
                )

        self.assertEqual(
            events,
            ["factory", "enter", "make_run_directory", ("exit", True)],
        )

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_linux_host_dependency_context_owns_and_cleans_one_private_run_directory(
        self,
    ) -> None:
        import stat
        from unittest.mock import patch

        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativeLifecycleDependencies,
            native_lifecycle_dependencies_for_host,
        )

        host_patches = (
            patch("scripts.native_lifecycle_evidence.sys.platform", "linux"),
            patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ),
        )
        with host_patches[0], host_patches[1]:
            with native_lifecycle_dependencies_for_host() as dependencies:
                self.assertIsInstance(dependencies, NativeLifecycleDependencies)
                for platform, arch in (
                    ("win", "x64"),
                    ("linux", "arm64"),
                    (True, "x64"),
                    ("linux", True),
                ):
                    with self.subTest(platform=platform, arch=arch):
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_failed"
                        ) as raised:
                            dependencies.make_run_directory(platform, arch)
                        self.assertEqual(str(raised.exception), "driver_failed")

                run_directory = dependencies.make_run_directory("linux", "x64")
                metadata = run_directory.lstat()
                self.assertTrue(run_directory.is_absolute())
                self.assertTrue(stat.S_ISDIR(metadata.st_mode))
                self.assertFalse(run_directory.is_symlink())
                self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o700)
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_failed"
                ) as repeated:
                    dependencies.make_run_directory("linux", "x64")
                self.assertEqual(str(repeated.exception), "driver_failed")
                self.assertNotIn(str(run_directory), str(repeated.exception))
            self.assertFalse(run_directory.exists())

            escaped_directory: Path | None = None
            with self.assertRaisesRegex(RuntimeError, "unit test exit"):
                with native_lifecycle_dependencies_for_host() as dependencies:
                    escaped_directory = dependencies.make_run_directory(
                        "linux", "x64"
                    )
                    raise RuntimeError("unit test exit")
            assert escaped_directory is not None
            self.assertFalse(escaped_directory.exists())

        for platform, machine in (
            ("darwin", "x86_64"),
            ("win32", "AMD64"),
            ("linux", "aarch64"),
        ):
            with self.subTest(host=(platform, machine)), patch(
                "scripts.native_lifecycle_evidence.sys.platform", platform
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value=machine,
            ):
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_unavailable"
                ) as unavailable:
                    with native_lifecycle_dependencies_for_host():
                        self.fail("unsupported host entered lifecycle context")
                self.assertEqual(str(unavailable.exception), "driver_unavailable")

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_linux_host_copy_file_creates_and_observes_one_private_execution_copy(
        self,
    ) -> None:
        import stat
        from unittest.mock import patch

        from scripts.native_lifecycle_evidence import (
            NativePathState,
            native_lifecycle_dependencies_for_host,
        )

        source_bytes = b"audited AppImage payload"
        source_sha256 = (
            "5d228523ef8526d2b117df416820dcc217474675aff07b8ed553a766b8377088"
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "UsageHub-0.8.6-linux-x86_64.AppImage"
            source.write_bytes(source_bytes)
            source.chmod(0o644)
            source_before = source.lstat()
            source_signature = (
                source_before.st_dev,
                source_before.st_ino,
                source_before.st_size,
                source_before.st_mode,
                source_before.st_mtime_ns,
                source_before.st_ctime_ns,
                source_before.st_nlink,
            )
            self.assertTrue(stat.S_ISREG(source_before.st_mode))
            self.assertFalse(source.is_symlink())
            self.assertEqual(hashlib.sha256(source_bytes).hexdigest(), source_sha256)

            run_directory: Path | None = None
            execution_copy: Path | None = None
            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ):
                with native_lifecycle_dependencies_for_host() as dependencies:
                    run_directory = dependencies.make_run_directory(
                        "linux", "x64"
                    )
                    execution_copy = run_directory / source.name
                    self.assertEqual(execution_copy.parent, run_directory)
                    self.assertEqual(
                        dependencies.inspect_path(
                            "fresh_execution_copy", execution_copy
                        ),
                        NativePathState(False, "missing", 0, None, 0, None),
                    )

                    self.assertIsNone(
                        dependencies.copy_file(source, execution_copy)
                    )
                    destination = execution_copy.lstat()
                    self.assertTrue(stat.S_ISREG(destination.st_mode))
                    self.assertFalse(execution_copy.is_symlink())
                    self.assertEqual(stat.S_IMODE(destination.st_mode), 0o600)
                    self.assertEqual(destination.st_nlink, 1)
                    self.assertNotEqual(
                        (destination.st_dev, destination.st_ino),
                        (source_before.st_dev, source_before.st_ino),
                    )
                    self.assertEqual(
                        dependencies.inspect_path(
                            "execution_copy", execution_copy
                        ),
                        NativePathState(
                            True,
                            "file",
                            len(source_bytes),
                            source_sha256,
                            0o600,
                            f"{destination.st_dev}:{destination.st_ino}",
                        ),
                    )

                assert run_directory is not None
                assert execution_copy is not None
                self.assertFalse(execution_copy.exists())
                self.assertFalse(run_directory.exists())

            source_after = source.lstat()
            self.assertEqual(
                (
                    source_after.st_dev,
                    source_after.st_ino,
                    source_after.st_size,
                    source_after.st_mode,
                    source_after.st_mtime_ns,
                    source_after.st_ctime_ns,
                    source_after.st_nlink,
                ),
                source_signature,
            )
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(
                hashlib.sha256(source.read_bytes()).hexdigest(), source_sha256
            )

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_linux_host_set_file_mode_promotes_only_the_owned_execution_copy_to_0700(
        self,
    ) -> None:
        import stat
        from unittest.mock import patch

        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativePathState,
            native_lifecycle_dependencies_for_host,
        )

        source_bytes = b"audited chmod payload"
        source_sha256 = (
            "f4f1c1610a5d3b905e72c19e6f6ae2bb3b9fbf323d3a9619680726cd4ac7ca51"
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "UsageHub-0.8.6-linux-x86_64.AppImage"
            source.write_bytes(source_bytes)
            source.chmod(0o644)
            source_before = source.lstat()
            source_signature = (
                source_before.st_dev,
                source_before.st_ino,
                source_before.st_size,
                source_before.st_mode,
                source_before.st_mtime_ns,
                source_before.st_ctime_ns,
                source_before.st_nlink,
            )
            run_directory: Path | None = None
            execution_copy: Path | None = None

            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ):
                with native_lifecycle_dependencies_for_host() as dependencies:
                    run_directory = dependencies.make_run_directory(
                        "linux", "x64"
                    )
                    execution_copy = run_directory / source.name
                    dependencies.copy_file(source, execution_copy)
                    initial = execution_copy.lstat()
                    file_id = f"{initial.st_dev}:{initial.st_ino}"
                    self.assertEqual(stat.S_IMODE(initial.st_mode), 0o600)
                    self.assertEqual(
                        dependencies.inspect_path(
                            "execution_copy", execution_copy
                        ),
                        NativePathState(
                            True,
                            "file",
                            len(source_bytes),
                            source_sha256,
                            0o600,
                            file_id,
                        ),
                    )

                    self.assertIsNone(
                        dependencies.set_file_mode(execution_copy, 0o700)
                    )
                    promoted = execution_copy.lstat()
                    self.assertEqual(
                        (promoted.st_dev, promoted.st_ino),
                        (initial.st_dev, initial.st_ino),
                    )
                    self.assertEqual(promoted.st_size, len(source_bytes))
                    self.assertEqual(stat.S_IMODE(promoted.st_mode), 0o700)
                    self.assertEqual(
                        dependencies.inspect_path(
                            "execution_copy", execution_copy
                        ),
                        NativePathState(
                            True,
                            "file",
                            len(source_bytes),
                            source_sha256,
                            0o700,
                            file_id,
                        ),
                    )

                    invalid_cases = (
                        (execution_copy, True),
                        (execution_copy, 0o600),
                        (execution_copy, 0o755),
                        (Path(execution_copy.name), 0o700),
                        (source, 0o700),
                    )
                    for invalid_path, invalid_mode in invalid_cases:
                        with self.subTest(
                            path=str(invalid_path), mode=invalid_mode
                        ):
                            before_invalid = execution_copy.lstat()
                            with self.assertRaisesRegex(
                                LifecycleEvidenceError, "driver_failed"
                            ) as rejected:
                                dependencies.set_file_mode(
                                    invalid_path, invalid_mode
                                )
                            self.assertEqual(
                                str(rejected.exception), "driver_failed"
                            )
                            self.assertNotIn(
                                str(run_directory), str(rejected.exception)
                            )
                            after_invalid = execution_copy.lstat()
                            self.assertEqual(
                                (
                                    after_invalid.st_dev,
                                    after_invalid.st_ino,
                                    after_invalid.st_size,
                                    after_invalid.st_mode,
                                    after_invalid.st_mtime_ns,
                                    after_invalid.st_ctime_ns,
                                    after_invalid.st_nlink,
                                ),
                                (
                                    before_invalid.st_dev,
                                    before_invalid.st_ino,
                                    before_invalid.st_size,
                                    before_invalid.st_mode,
                                    before_invalid.st_mtime_ns,
                                    before_invalid.st_ctime_ns,
                                    before_invalid.st_nlink,
                                ),
                            )

                assert run_directory is not None
                assert execution_copy is not None
                self.assertFalse(execution_copy.exists())
                self.assertFalse(run_directory.exists())

            source_after = source.lstat()
            self.assertEqual(
                (
                    source_after.st_dev,
                    source_after.st_ino,
                    source_after.st_size,
                    source_after.st_mode,
                    source_after.st_mtime_ns,
                    source_after.st_ctime_ns,
                    source_after.st_nlink,
                ),
                source_signature,
            )
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(
                hashlib.sha256(source.read_bytes()).hexdigest(), source_sha256
            )

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_linux_host_copy_file_creates_one_sentinel_after_completed_execution_copy(
        self,
    ) -> None:
        import stat
        from unittest.mock import patch

        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativePathState,
            native_lifecycle_dependencies_for_host,
        )

        source_bytes = b"audited sentinel payload"
        source_sha256 = (
            "8cc73046ae16fe6ba6be49643eb58af168c0f13a59ef48ccb8ad6ef665cc43ee"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "UsageHub-0.8.6-linux-x86_64.AppImage"
            source.write_bytes(source_bytes)
            source.chmod(0o644)
            source_before = source.lstat()
            source_signature = (
                source_before.st_dev,
                source_before.st_ino,
                source_before.st_size,
                source_before.st_mode,
                source_before.st_mtime_ns,
                source_before.st_ctime_ns,
                source_before.st_nlink,
            )

            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ):
                with native_lifecycle_dependencies_for_host() as dependencies:
                    premature_root = dependencies.make_run_directory(
                        "linux", "x64"
                    )
                    premature_sentinel = (
                        premature_root / "outside-product-sentinel.bin"
                    )
                    self.assertEqual(
                        dependencies.inspect_path(
                            "fresh_sentinel", premature_sentinel
                        ),
                        NativePathState(False, "missing", 0, None, 0, None),
                    )
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_failed"
                    ) as premature:
                        dependencies.copy_file(source, premature_sentinel)
                    self.assertEqual(str(premature.exception), "driver_failed")
                    self.assertNotIn(
                        str(premature_root), str(premature.exception)
                    )
                    self.assertFalse(premature_sentinel.exists())
                self.assertFalse(premature_root.exists())

                run_directory: Path | None = None
                execution_copy: Path | None = None
                sentinel: Path | None = None
                with native_lifecycle_dependencies_for_host() as dependencies:
                    run_directory = dependencies.make_run_directory(
                        "linux", "x64"
                    )
                    execution_copy = run_directory / source.name
                    sentinel = run_directory / "outside-product-sentinel.bin"
                    dependencies.copy_file(source, execution_copy)
                    dependencies.set_file_mode(execution_copy, 0o700)
                    execution_metadata = execution_copy.lstat()
                    execution_state = NativePathState(
                        True,
                        "file",
                        len(source_bytes),
                        source_sha256,
                        0o700,
                        (
                            f"{execution_metadata.st_dev}:"
                            f"{execution_metadata.st_ino}"
                        ),
                    )
                    self.assertEqual(
                        dependencies.inspect_path(
                            "execution_copy", execution_copy
                        ),
                        execution_state,
                    )
                    self.assertEqual(
                        dependencies.inspect_path("fresh_sentinel", sentinel),
                        NativePathState(False, "missing", 0, None, 0, None),
                    )

                    wrong_name = run_directory / "wrong-sentinel.bin"
                    relative = Path("outside-product-sentinel.bin")
                    foreign = root / "outside-product-sentinel.bin"
                    for invalid_destination in (
                        wrong_name,
                        relative,
                        foreign,
                    ):
                        with self.subTest(destination=str(invalid_destination)):
                            self.assertFalse(invalid_destination.exists())
                            with self.assertRaisesRegex(
                                LifecycleEvidenceError, "driver_failed"
                            ) as rejected:
                                dependencies.copy_file(
                                    source, invalid_destination
                                )
                            self.assertEqual(
                                str(rejected.exception), "driver_failed"
                            )
                            self.assertNotIn(
                                str(root), str(rejected.exception)
                            )
                            self.assertFalse(invalid_destination.exists())

                    self.assertIsNone(
                        dependencies.copy_file(source, sentinel)
                    )
                    sentinel_metadata = sentinel.lstat()
                    self.assertTrue(stat.S_ISREG(sentinel_metadata.st_mode))
                    self.assertFalse(sentinel.is_symlink())
                    self.assertEqual(
                        stat.S_IMODE(sentinel_metadata.st_mode), 0o600
                    )
                    self.assertEqual(sentinel_metadata.st_nlink, 1)
                    self.assertNotEqual(
                        (sentinel_metadata.st_dev, sentinel_metadata.st_ino),
                        (source_before.st_dev, source_before.st_ino),
                    )
                    self.assertNotEqual(
                        (sentinel_metadata.st_dev, sentinel_metadata.st_ino),
                        (
                            execution_metadata.st_dev,
                            execution_metadata.st_ino,
                        ),
                    )
                    sentinel_state = NativePathState(
                        True,
                        "file",
                        len(source_bytes),
                        source_sha256,
                        0o600,
                        (
                            f"{sentinel_metadata.st_dev}:"
                            f"{sentinel_metadata.st_ino}"
                        ),
                    )
                    self.assertEqual(
                        dependencies.inspect_path("sentinel", sentinel),
                        sentinel_state,
                    )

                    before_duplicate = sentinel.lstat()
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_failed"
                    ) as duplicate:
                        dependencies.copy_file(source, sentinel)
                    self.assertEqual(str(duplicate.exception), "driver_failed")
                    after_duplicate = sentinel.lstat()
                    self.assertEqual(
                        (
                            after_duplicate.st_dev,
                            after_duplicate.st_ino,
                            after_duplicate.st_size,
                            after_duplicate.st_mode,
                            after_duplicate.st_mtime_ns,
                            after_duplicate.st_ctime_ns,
                            after_duplicate.st_nlink,
                        ),
                        (
                            before_duplicate.st_dev,
                            before_duplicate.st_ino,
                            before_duplicate.st_size,
                            before_duplicate.st_mode,
                            before_duplicate.st_mtime_ns,
                            before_duplicate.st_ctime_ns,
                            before_duplicate.st_nlink,
                        ),
                    )
                    self.assertEqual(
                        dependencies.inspect_path(
                            "execution_copy", execution_copy
                        ),
                        execution_state,
                    )

                assert run_directory is not None
                assert execution_copy is not None
                assert sentinel is not None
                self.assertFalse(execution_copy.exists())
                self.assertFalse(sentinel.exists())
                self.assertFalse(run_directory.exists())

            source_after = source.lstat()
            self.assertEqual(
                (
                    source_after.st_dev,
                    source_after.st_ino,
                    source_after.st_size,
                    source_after.st_mode,
                    source_after.st_mtime_ns,
                    source_after.st_ctime_ns,
                    source_after.st_nlink,
                ),
                source_signature,
            )
            self.assertEqual(source.read_bytes(), source_bytes)

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_linux_host_remove_and_recopy_execution_preserves_the_tracked_sentinel(
        self,
    ) -> None:
        import stat
        from unittest.mock import patch

        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativePathState,
            native_lifecycle_dependencies_for_host,
        )

        source_bytes = b"audited recopy payload"
        source_sha256 = (
            "f9beaa1eae256454fb28d29257796ca45d03d422e3f8bd3c1c7870d8ef0cfee5"
        )
        missing = NativePathState(False, "missing", 0, None, 0, None)

        def signature(path: Path) -> tuple[int, ...]:
            metadata = path.lstat()
            return (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mode,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                metadata.st_nlink,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "UsageHub-0.8.6-linux-x86_64.AppImage"
            source.write_bytes(source_bytes)
            source.chmod(0o644)
            source_signature = signature(source)
            alternate_same_root = root / "alternate-same"
            alternate_same_root.mkdir()
            alternate_same = alternate_same_root / source.name
            alternate_same.write_bytes(source_bytes)
            alternate_same.chmod(0o644)
            alternate_mutated_root = root / "alternate-mutated"
            alternate_mutated_root.mkdir()
            alternate_mutated = alternate_mutated_root / source.name
            alternate_mutated.write_bytes(b"mutated recopy payload")
            alternate_mutated.chmod(0o644)

            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ):
                with native_lifecycle_dependencies_for_host() as dependencies:
                    premature_root = dependencies.make_run_directory(
                        "linux", "x64"
                    )
                    premature_execution = premature_root / source.name
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_failed"
                    ) as premature:
                        dependencies.remove_path(premature_execution)
                    self.assertEqual(str(premature.exception), "driver_failed")
                    self.assertNotIn(
                        str(premature_root), str(premature.exception)
                    )
                    self.assertFalse(premature_execution.exists())
                self.assertFalse(premature_root.exists())

                run_directory: Path | None = None
                execution_copy: Path | None = None
                sentinel: Path | None = None
                with native_lifecycle_dependencies_for_host() as dependencies:
                    run_directory = dependencies.make_run_directory(
                        "linux", "x64"
                    )
                    execution_copy = run_directory / source.name
                    sentinel = run_directory / "outside-product-sentinel.bin"
                    dependencies.copy_file(source, execution_copy)
                    dependencies.set_file_mode(execution_copy, 0o700)
                    first_execution_metadata = execution_copy.lstat()
                    first_execution_id = (
                        f"{first_execution_metadata.st_dev}:"
                        f"{first_execution_metadata.st_ino}"
                    )
                    first_execution_state = NativePathState(
                        True,
                        "file",
                        len(source_bytes),
                        source_sha256,
                        0o700,
                        first_execution_id,
                    )
                    self.assertEqual(
                        dependencies.inspect_path(
                            "execution_copy", execution_copy
                        ),
                        first_execution_state,
                    )
                    dependencies.copy_file(source, sentinel)
                    sentinel_metadata = sentinel.lstat()
                    sentinel_state = NativePathState(
                        True,
                        "file",
                        len(source_bytes),
                        source_sha256,
                        0o600,
                        (
                            f"{sentinel_metadata.st_dev}:"
                            f"{sentinel_metadata.st_ino}"
                        ),
                    )
                    self.assertEqual(
                        dependencies.inspect_path("sentinel", sentinel),
                        sentinel_state,
                    )

                    wrong = run_directory / "wrong-execution.AppImage"
                    relative = Path(source.name)
                    invalid_remove_paths = (
                        wrong,
                        relative,
                        source,
                        sentinel,
                        run_directory,
                    )
                    for invalid_path in invalid_remove_paths:
                        with self.subTest(remove=str(invalid_path)):
                            execution_before = signature(execution_copy)
                            sentinel_before = signature(sentinel)
                            source_before = signature(source)
                            with self.assertRaisesRegex(
                                LifecycleEvidenceError, "driver_failed"
                            ) as rejected:
                                dependencies.remove_path(invalid_path)
                            self.assertEqual(
                                str(rejected.exception), "driver_failed"
                            )
                            self.assertNotIn(
                                str(root), str(rejected.exception)
                            )
                            self.assertEqual(
                                signature(execution_copy), execution_before
                            )
                            self.assertEqual(signature(sentinel), sentinel_before)
                            self.assertEqual(signature(source), source_before)
                            self.assertFalse(wrong.exists())
                            self.assertFalse(relative.exists())

                    self.assertIsNone(
                        dependencies.remove_path(execution_copy)
                    )
                    self.assertEqual(
                        dependencies.inspect_path(
                            "preserve_execution_copy", execution_copy
                        ),
                        missing,
                    )
                    self.assertEqual(
                        dependencies.inspect_path("sentinel", sentinel),
                        sentinel_state,
                    )
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_failed"
                    ) as duplicate_remove:
                        dependencies.remove_path(execution_copy)
                    self.assertEqual(
                        str(duplicate_remove.exception), "driver_failed"
                    )
                    self.assertFalse(execution_copy.exists())

                    for alternate in (alternate_same, alternate_mutated):
                        with self.subTest(source=str(alternate)):
                            alternate_before = signature(alternate)
                            sentinel_before = signature(sentinel)
                            with self.assertRaisesRegex(
                                LifecycleEvidenceError, "driver_failed"
                            ) as rejected_source:
                                dependencies.copy_file(
                                    alternate, execution_copy
                                )
                            self.assertEqual(
                                str(rejected_source.exception), "driver_failed"
                            )
                            self.assertNotIn(
                                str(root), str(rejected_source.exception)
                            )
                            self.assertFalse(execution_copy.exists())
                            self.assertEqual(
                                signature(alternate), alternate_before
                            )
                            self.assertEqual(signature(sentinel), sentinel_before)

                    self.assertIsNone(
                        dependencies.copy_file(source, execution_copy)
                    )
                    second_unpromoted = execution_copy.lstat()
                    self.assertEqual(
                        stat.S_IMODE(second_unpromoted.st_mode), 0o600
                    )
                    # A filesystem may legitimately reuse the first file's
                    # inode after its authoritative unlink. O_EXCL, the
                    # generation gate, and the intervening missing probe prove
                    # this is a newly created file instance.
                    self.assertNotEqual(
                        (
                            second_unpromoted.st_dev,
                            second_unpromoted.st_ino,
                        ),
                        (sentinel_metadata.st_dev, sentinel_metadata.st_ino),
                    )
                    self.assertNotEqual(
                        (
                            second_unpromoted.st_dev,
                            second_unpromoted.st_ino,
                        ),
                        (source_signature[0], source_signature[1]),
                    )
                    second_file_id = (
                        f"{second_unpromoted.st_dev}:"
                        f"{second_unpromoted.st_ino}"
                    )
                    self.assertEqual(
                        dependencies.inspect_path(
                            "execution_copy", execution_copy
                        ),
                        NativePathState(
                            True,
                            "file",
                            len(source_bytes),
                            source_sha256,
                            0o600,
                            second_file_id,
                        ),
                    )
                    dependencies.set_file_mode(execution_copy, 0o700)
                    self.assertEqual(
                        dependencies.inspect_path(
                            "execution_copy", execution_copy
                        ),
                        NativePathState(
                            True,
                            "file",
                            len(source_bytes),
                            source_sha256,
                            0o700,
                            second_file_id,
                        ),
                    )
                    self.assertEqual(
                        dependencies.inspect_path("sentinel", sentinel),
                        sentinel_state,
                    )

                    self.assertIsNone(
                        dependencies.remove_path(execution_copy)
                    )
                    self.assertEqual(
                        dependencies.inspect_path(
                            "delete_execution_copy", execution_copy
                        ),
                        missing,
                    )
                    self.assertEqual(
                        dependencies.inspect_path("sentinel", sentinel),
                        sentinel_state,
                    )
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_failed"
                    ) as third_copy:
                        dependencies.copy_file(source, execution_copy)
                    self.assertEqual(str(third_copy.exception), "driver_failed")
                    self.assertFalse(execution_copy.exists())

                assert run_directory is not None
                assert execution_copy is not None
                assert sentinel is not None
                self.assertFalse(execution_copy.exists())
                self.assertFalse(sentinel.exists())
                self.assertFalse(run_directory.exists())

            self.assertEqual(signature(source), source_signature)
            self.assertEqual(source.read_bytes(), source_bytes)

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_linux_host_copy_rejects_a_source_inside_the_owned_run_directory(
        self,
    ) -> None:
        from unittest.mock import patch

        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            native_lifecycle_dependencies_for_host,
        )

        context = None
        run_directory: Path | None = None
        nested: Path | None = None
        source: Path | None = None
        destination: Path | None = None
        copy_error: LifecycleEvidenceError | None = None
        destination_created = False
        source_after = b""
        try:
            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ):
                context = native_lifecycle_dependencies_for_host()
                dependencies = context.__enter__()
                run_directory = dependencies.make_run_directory("linux", "x64")
                nested = run_directory / "nested-source"
                nested.mkdir(mode=0o700)
                source = nested / "UsageHub-0.8.6-linux-x86_64.AppImage"
                source.write_bytes(b"private nested source")
                source.chmod(0o600)
                destination = run_directory / source.name
                try:
                    dependencies.copy_file(source, destination)
                except LifecycleEvidenceError as error:
                    copy_error = error
                destination_created = destination.exists()
                source_after = source.read_bytes()
                source.unlink()
                nested.rmdir()
                context.__exit__(
                    type(copy_error) if copy_error is not None else None,
                    copy_error,
                    copy_error.__traceback__ if copy_error is not None else None,
                )
                context = None

            self.assertIsNotNone(copy_error)
            assert copy_error is not None
            self.assertEqual(str(copy_error), "driver_failed")
            assert run_directory is not None
            self.assertNotIn(str(run_directory), str(copy_error))
            self.assertFalse(destination_created)
            self.assertEqual(source_after, b"private nested source")
            self.assertFalse(run_directory.exists())
        finally:
            if source is not None and source.exists():
                source.unlink()
            if nested is not None and nested.exists():
                nested.rmdir()
            if destination is not None and destination.exists():
                destination.unlink()
            if context is not None:
                try:
                    context.__exit__(None, None, None)
                except LifecycleEvidenceError:
                    pass
            if run_directory is not None and run_directory.exists():
                run_directory.rmdir()

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_linux_host_copy_failure_cleans_its_partial_file_and_run_directory(
        self,
    ) -> None:
        from unittest.mock import patch

        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            native_lifecycle_dependencies_for_host,
        )

        real_write = os.write
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "UsageHub-0.8.6-linux-x86_64.AppImage"
            source_bytes = b"audited source remains unchanged"
            source.write_bytes(source_bytes)
            source.chmod(0o644)
            source_before = source.lstat()
            run_directory: Path | None = None
            destination: Path | None = None
            context = None
            copy_error: LifecycleEvidenceError | None = None
            context_error: LifecycleEvidenceError | None = None
            wrote_partial = False

            def fail_after_partial_write(
                descriptor: int, data: object
            ) -> int:
                nonlocal wrote_partial
                payload = bytes(data)
                real_write(descriptor, payload[:4])
                wrote_partial = True
                raise OSError("PRIVATE_PARTIAL_WRITE")

            try:
                with patch(
                    "scripts.native_lifecycle_evidence.sys.platform", "linux"
                ), patch(
                    "scripts.native_lifecycle_evidence.host_platform_module.machine",
                    return_value="x86_64",
                ):
                    context = native_lifecycle_dependencies_for_host()
                    dependencies = context.__enter__()
                    run_directory = dependencies.make_run_directory(
                        "linux", "x64"
                    )
                    destination = run_directory / source.name
                    with patch(
                        "scripts.native_lifecycle_evidence.os.write",
                        side_effect=fail_after_partial_write,
                    ):
                        try:
                            dependencies.copy_file(source, destination)
                        except LifecycleEvidenceError as error:
                            copy_error = error
                    try:
                        context.__exit__(
                            type(copy_error) if copy_error is not None else None,
                            copy_error,
                            (
                                copy_error.__traceback__
                                if copy_error is not None
                                else None
                            ),
                        )
                    except LifecycleEvidenceError as error:
                        context_error = error
                    context = None

                self.assertTrue(wrote_partial)
                self.assertIsNotNone(copy_error)
                assert copy_error is not None
                self.assertEqual(str(copy_error), "driver_failed")
                self.assertNotIn("PRIVATE_", str(copy_error))
                if context_error is not None:
                    self.assertEqual(str(context_error), "driver_failed")
                    self.assertNotIn("PRIVATE_", str(context_error))
                assert destination is not None
                assert run_directory is not None
                self.assertFalse(destination.exists())
                self.assertFalse(run_directory.exists())
                source_after = source.lstat()
                self.assertEqual(
                    (source_after.st_dev, source_after.st_ino),
                    (source_before.st_dev, source_before.st_ino),
                )
                self.assertEqual(source.read_bytes(), source_bytes)
            finally:
                if destination is not None and destination.exists():
                    destination.unlink()
                if context is not None:
                    try:
                        context.__exit__(None, None, None)
                    except LifecycleEvidenceError:
                        pass
                if run_directory is not None and run_directory.exists():
                    run_directory.rmdir()

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_linux_host_inspect_rejects_a_public_entry_replaced_during_hashing(
        self,
    ) -> None:
        from unittest.mock import patch

        import scripts.native_lifecycle_evidence as lifecycle_evidence
        from scripts.native_lifecycle_evidence import LifecycleEvidenceError

        real_fstat = os.fstat
        real_rmdir = os.rmdir
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "UsageHub-0.8.6-linux-x86_64.AppImage"
            source_bytes = b"audited execution copy"
            source.write_bytes(source_bytes)
            source.chmod(0o644)
            foreign_seed = root / "foreign-seed"
            foreign_bytes = b"PRIVATE_FOREIGN_ENTRY"
            foreign_seed.write_bytes(foreign_bytes)
            foreign_identity = foreign_seed.lstat()
            context = None
            run_directory: Path | None = None
            destination: Path | None = None
            owned_original: Path | None = None
            inspect_error: LifecycleEvidenceError | None = None
            cleanup_error: LifecycleEvidenceError | None = None
            returned_state: object | None = None
            swapped = False
            descriptor_checks = 0

            def swap_public_after_hashed_descriptor_check(
                descriptor: int,
            ) -> os.stat_result:
                nonlocal descriptor_checks, swapped
                result = real_fstat(descriptor)
                descriptor_checks += 1
                if descriptor_checks == 2:
                    assert destination is not None
                    assert owned_original is not None
                    destination.rename(owned_original)
                    foreign_seed.rename(destination)
                    swapped = True
                return result

            try:
                with patch(
                    "scripts.native_lifecycle_evidence.sys.platform", "linux"
                ), patch(
                    "scripts.native_lifecycle_evidence.host_platform_module.machine",
                    return_value="x86_64",
                ):
                    context = lifecycle_evidence.native_lifecycle_dependencies_for_host()
                    dependencies = context.__enter__()
                    run_directory = dependencies.make_run_directory(
                        "linux", "x64"
                    )
                    destination = run_directory / source.name
                    owned_original = run_directory / "owned-original"
                    dependencies.copy_file(source, destination)
                    with patch(
                        "scripts.native_lifecycle_evidence.os.fstat",
                        side_effect=swap_public_after_hashed_descriptor_check,
                    ):
                        try:
                            returned_state = dependencies.inspect_path(
                                "execution_copy", destination
                            )
                        except LifecycleEvidenceError as error:
                            inspect_error = error
                    try:
                        context.__exit__(
                            type(inspect_error) if inspect_error is not None else None,
                            inspect_error,
                            (
                                inspect_error.__traceback__
                                if inspect_error is not None
                                else None
                            ),
                        )
                    except LifecycleEvidenceError as error:
                        cleanup_error = error
                    context = None

                self.assertTrue(swapped)
                self.assertIsNone(returned_state)
                self.assertIsNotNone(inspect_error)
                assert inspect_error is not None
                self.assertEqual(str(inspect_error), "driver_failed")
                self.assertNotIn(str(root), str(inspect_error))
                if cleanup_error is not None:
                    self.assertEqual(str(cleanup_error), "driver_failed")
                    self.assertNotIn(str(root), str(cleanup_error))
                assert destination is not None
                assert owned_original is not None
                replacement = destination.lstat()
                self.assertEqual(
                    (replacement.st_dev, replacement.st_ino),
                    (foreign_identity.st_dev, foreign_identity.st_ino),
                )
                self.assertEqual(destination.read_bytes(), foreign_bytes)
                self.assertEqual(owned_original.read_bytes(), source_bytes)
            finally:
                for candidate in (destination, owned_original, foreign_seed):
                    if candidate is not None and candidate.exists():
                        candidate.unlink()
                if context is not None:
                    try:
                        context.__exit__(None, None, None)
                    except LifecycleEvidenceError:
                        pass
                if run_directory is not None and run_directory.exists():
                    real_rmdir(run_directory)

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_linux_host_inspect_rejects_same_inode_mutation_before_final_path_stat(
        self,
    ) -> None:
        from unittest.mock import patch

        import scripts.native_lifecycle_evidence as lifecycle_evidence
        from scripts.native_lifecycle_evidence import LifecycleEvidenceError

        real_stat = os.stat
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "UsageHub-0.8.6-linux-x86_64.AppImage"
            source_bytes = b"trusted inspect payload"
            mutated_bytes = b"hostile inspect payload"
            self.assertEqual(len(mutated_bytes), len(source_bytes))
            source.write_bytes(source_bytes)
            source.chmod(0o644)
            returned_state: object | None = None
            inspect_error: LifecycleEvidenceError | None = None
            destination: Path | None = None
            mutation_fd: int | None = None
            stat_calls = 0
            mutated = False

            def mutate_before_final_public_stat(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> os.stat_result:
                nonlocal mutated, stat_calls
                stat_calls += 1
                if stat_calls == 2:
                    assert mutation_fd is not None
                    before = os.fstat(mutation_fd)
                    os.lseek(mutation_fd, 0, os.SEEK_SET)
                    self.assertEqual(os.write(mutation_fd, mutated_bytes), len(mutated_bytes))
                    os.fsync(mutation_fd)
                    os.utime(
                        mutation_fd,
                        ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
                    )
                    after = os.fstat(mutation_fd)
                    self.assertNotEqual(
                        (after.st_mtime_ns, after.st_ctime_ns),
                        (before.st_mtime_ns, before.st_ctime_ns),
                    )
                    mutated = True
                return real_stat(path, *args, **kwargs)

            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ):
                with lifecycle_evidence.native_lifecycle_dependencies_for_host() as dependencies:
                    run_directory = dependencies.make_run_directory(
                        "linux", "x64"
                    )
                    destination = run_directory / source.name
                    dependencies.copy_file(source, destination)
                    mutation_fd = os.open(
                        destination,
                        os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    )
                    try:
                        with patch(
                            "scripts.native_lifecycle_evidence.os.stat",
                            side_effect=mutate_before_final_public_stat,
                        ):
                            try:
                                returned_state = dependencies.inspect_path(
                                    "execution_copy", destination
                                )
                            except LifecycleEvidenceError as error:
                                inspect_error = error
                    finally:
                        os.close(mutation_fd)
                        mutation_fd = None

            self.assertTrue(mutated)
            self.assertIsNone(returned_state)
            self.assertIsNotNone(inspect_error)
            assert inspect_error is not None
            self.assertEqual(str(inspect_error), "driver_failed")
            self.assertNotIn(str(root), str(inspect_error))
            self.assertEqual(source.read_bytes(), source_bytes)
            assert destination is not None
            self.assertFalse(destination.exists())

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_linux_host_run_directory_rejects_an_entry_swap_before_identity_binding(
        self,
    ) -> None:
        import os
        import stat
        from unittest.mock import patch

        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            native_lifecycle_dependencies_for_host,
        )

        real_mkdir = os.mkdir
        real_open = os.open
        real_rename = os.rename
        real_rmdir = os.rmdir
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            temp_root = root / "authoritative-temp"
            temp_root.mkdir(mode=0o700)
            foreign_seed = temp_root / "foreign-seed"
            foreign_seed.mkdir(mode=0o700)
            foreign_metadata = foreign_seed.lstat()
            foreign_identity = (
                foreign_metadata.st_dev,
                foreign_metadata.st_ino,
            )
            public_entry: Path | None = None
            original_entry: Path | None = None
            swapped = False

            def track_run_directory_creation(
                path: object,
                mode: int = 0o777,
                *args: object,
                **kwargs: object,
            ) -> None:
                nonlocal public_entry
                real_mkdir(path, mode, *args, **kwargs)
                candidate = Path(path)
                if not candidate.is_absolute():
                    candidate = temp_root / candidate
                if (
                    candidate.parent == temp_root
                    and candidate.name.startswith("usagehub-native-lifecycle-")
                ):
                    public_entry = candidate

            def swap_before_first_binding_open(
                path: object,
                flags: int,
                *args: object,
                **kwargs: object,
            ) -> int:
                nonlocal original_entry, swapped
                if (
                    not swapped
                    and public_entry is not None
                    and public_entry.exists()
                ):
                    original_entry = public_entry.with_name(
                        f"{public_entry.name}-owned-original"
                    )
                    (public_entry / "owned-marker").write_bytes(b"owned")
                    real_rename(public_entry, original_entry)
                    real_rename(foreign_seed, public_entry)
                    swapped = True
                return real_open(path, flags, *args, **kwargs)

            context = None
            make_error: LifecycleEvidenceError | None = None
            try:
                with patch(
                    "scripts.native_lifecycle_evidence.sys.platform", "linux"
                ), patch(
                    "scripts.native_lifecycle_evidence.host_platform_module.machine",
                    return_value="x86_64",
                ), patch(
                    "scripts.native_lifecycle_evidence.tempfile.gettempdir",
                    return_value=str(temp_root),
                ), patch(
                    "scripts.native_lifecycle_evidence.os.mkdir",
                    side_effect=track_run_directory_creation,
                ), patch(
                    "scripts.native_lifecycle_evidence.os.open",
                    side_effect=swap_before_first_binding_open,
                ):
                    context = native_lifecycle_dependencies_for_host()
                    dependencies = context.__enter__()
                    try:
                        dependencies.make_run_directory("linux", "x64")
                    except LifecycleEvidenceError as error:
                        make_error = error
                    finally:
                        context.__exit__(
                            type(make_error) if make_error is not None else None,
                            make_error,
                            make_error.__traceback__ if make_error is not None else None,
                        )
                        context = None

                self.assertIsNotNone(make_error)
                assert make_error is not None
                self.assertEqual(str(make_error), "driver_failed")
                self.assertNotIn(str(temp_root), str(make_error))
                self.assertTrue(swapped)
                assert public_entry is not None
                assert original_entry is not None
                replacement = public_entry.lstat()
                self.assertTrue(stat.S_ISDIR(replacement.st_mode))
                self.assertEqual(
                    (replacement.st_dev, replacement.st_ino), foreign_identity
                )
                self.assertEqual(
                    (original_entry / "owned-marker").read_bytes(), b"owned"
                )
            finally:
                if context is not None:
                    context.__exit__(None, None, None)
                for candidate in (public_entry, original_entry, foreign_seed):
                    if candidate is None or not candidate.exists():
                        continue
                    marker = candidate / "owned-marker"
                    if marker.exists():
                        marker.unlink()
                    real_rmdir(candidate)

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_linux_host_dependency_cleanup_does_not_delete_a_swapped_foreign_directory(
        self,
    ) -> None:
        import os
        from unittest.mock import patch

        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            native_lifecycle_dependencies_for_host,
        )

        real_rename = os.rename
        real_rmdir = os.rmdir
        run_directory: Path | None = None
        original_directory: Path | None = None
        foreign_directory: Path | None = None
        foreign_identity: tuple[int, int] | None = None
        swapped = False

        def swap_then_quarantine(
            source: object,
            destination: object,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal swapped
            if not swapped:
                assert run_directory is not None
                assert original_directory is not None
                assert foreign_directory is not None
                real_rename(run_directory, original_directory)
                (original_directory / "owned-marker").write_bytes(b"owned")
                real_rename(foreign_directory, run_directory)
                swapped = True
            real_rename(source, destination, *args, **kwargs)

        try:
            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ), patch(
                "scripts.native_lifecycle_evidence.os.rename",
                side_effect=swap_then_quarantine,
            ):
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_failed"
                ) as raised:
                    with native_lifecycle_dependencies_for_host() as dependencies:
                        run_directory = dependencies.make_run_directory(
                            "linux", "x64"
                        )
                        original_directory = run_directory.with_name(
                            f"{run_directory.name}-owned-original"
                        )
                        foreign_directory = run_directory.with_name(
                            f"{run_directory.name}-foreign"
                        )
                        foreign_directory.mkdir(mode=0o700)
                        metadata = foreign_directory.lstat()
                        foreign_identity = (metadata.st_dev, metadata.st_ino)

            self.assertEqual(str(raised.exception), "driver_failed")
            self.assertTrue(swapped)
            assert run_directory is not None
            assert original_directory is not None
            assert foreign_identity is not None
            replacement = run_directory.lstat()
            self.assertEqual(
                (replacement.st_dev, replacement.st_ino), foreign_identity
            )
            self.assertEqual(
                (original_directory / "owned-marker").read_bytes(), b"owned"
            )
        finally:
            for directory in (run_directory, original_directory, foreign_directory):
                if directory is None or not directory.exists():
                    continue
                marker = directory / "owned-marker"
                if marker.exists():
                    marker.unlink()
                real_rmdir(directory)

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_linux_host_execution_copy_quarantine_restore_does_not_clobber_new_public_entry(
        self,
    ) -> None:
        from unittest.mock import patch

        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            native_lifecycle_dependencies_for_host,
        )

        real_rename = os.rename
        real_stat = os.stat
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "UsageHub-0.8.6-linux-x86_64.AppImage"
            owned_bytes = b"owned execution copy"
            source.write_bytes(owned_bytes)
            source.chmod(0o644)
            foreign_seed = root / "foreign-seed"
            foreign_bytes = b"foreign quarantined entry"
            foreign_seed.write_bytes(foreign_bytes)
            foreign_metadata = foreign_seed.lstat()
            foreign_identity = (
                foreign_metadata.st_dev,
                foreign_metadata.st_ino,
            )
            run_directory: Path | None = None
            public_entry: Path | None = None
            owned_original: Path | None = None
            quarantine_name: str | None = None
            new_public_identity: tuple[int, int] | None = None
            cleanup_error: LifecycleEvidenceError | None = None
            swapped = False
            new_public_created = False

            def swap_before_public_to_quarantine(
                source_name: object,
                destination_name: object,
                *args: object,
                **kwargs: object,
            ) -> None:
                nonlocal quarantine_name, swapped
                if not swapped:
                    assert public_entry is not None
                    assert owned_original is not None
                    real_rename(public_entry, owned_original)
                    real_rename(foreign_seed, public_entry)
                    quarantine_name = str(destination_name)
                    swapped = True
                real_rename(source_name, destination_name, *args, **kwargs)

            def create_public_before_mismatch_restore(
                path: object,
                *args: object,
                **kwargs: object,
            ) -> os.stat_result:
                nonlocal new_public_created, new_public_identity
                if (
                    not new_public_created
                    and quarantine_name is not None
                    and str(path) == quarantine_name
                ):
                    assert public_entry is not None
                    public_entry.write_bytes(b"new concurrent public entry")
                    metadata = public_entry.lstat()
                    new_public_identity = (metadata.st_dev, metadata.st_ino)
                    new_public_created = True
                return real_stat(path, *args, **kwargs)

            context = None
            try:
                with patch(
                    "scripts.native_lifecycle_evidence.sys.platform", "linux"
                ), patch(
                    "scripts.native_lifecycle_evidence.host_platform_module.machine",
                    return_value="x86_64",
                ):
                    context = native_lifecycle_dependencies_for_host()
                    dependencies = context.__enter__()
                    run_directory = dependencies.make_run_directory(
                        "linux", "x64"
                    )
                    public_entry = run_directory / source.name
                    owned_original = run_directory / "owned-original"
                    dependencies.copy_file(source, public_entry)
                    with patch(
                        "scripts.native_lifecycle_evidence.os.rename",
                        side_effect=swap_before_public_to_quarantine,
                    ), patch(
                        "scripts.native_lifecycle_evidence.os.stat",
                        side_effect=create_public_before_mismatch_restore,
                    ):
                        try:
                            context.__exit__(None, None, None)
                        except LifecycleEvidenceError as error:
                            cleanup_error = error
                    context = None

                self.assertTrue(swapped)
                self.assertTrue(new_public_created)
                self.assertIsNotNone(cleanup_error)
                assert cleanup_error is not None
                self.assertEqual(str(cleanup_error), "driver_failed")
                self.assertNotIn(str(root), str(cleanup_error))
                assert public_entry is not None
                assert new_public_identity is not None
                public_metadata = public_entry.lstat()
                self.assertEqual(
                    (public_metadata.st_dev, public_metadata.st_ino),
                    new_public_identity,
                )
                self.assertEqual(
                    public_entry.read_bytes(), b"new concurrent public entry"
                )
                assert owned_original is not None
                self.assertEqual(owned_original.read_bytes(), owned_bytes)
                assert run_directory is not None
                assert quarantine_name is not None
                quarantine = run_directory / quarantine_name
                quarantine_metadata = quarantine.lstat()
                self.assertEqual(
                    (quarantine_metadata.st_dev, quarantine_metadata.st_ino),
                    foreign_identity,
                )
                self.assertEqual(quarantine.read_bytes(), foreign_bytes)
            finally:
                if context is not None:
                    try:
                        context.__exit__(None, None, None)
                    except LifecycleEvidenceError:
                        pass
                if run_directory is not None and run_directory.exists():
                    for child in run_directory.iterdir():
                        child.unlink()
                    run_directory.rmdir()
                if foreign_seed.exists():
                    foreign_seed.unlink()

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_linux_host_dependency_cleanup_failures_are_path_free(self) -> None:
        import os
        from unittest.mock import patch

        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            native_lifecycle_dependencies_for_host,
        )

        real_rmdir = os.rmdir
        for body_fails in (False, True):
            with self.subTest(body_fails=body_fails):
                run_directory: Path | None = None
                try:
                    with patch(
                        "scripts.native_lifecycle_evidence.sys.platform", "linux"
                    ), patch(
                        "scripts.native_lifecycle_evidence.host_platform_module.machine",
                        return_value="x86_64",
                    ), patch(
                        "scripts.native_lifecycle_evidence.os.rmdir",
                        side_effect=OSError("PRIVATE_CLEANUP_PATH"),
                    ):
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_failed"
                        ) as raised:
                            with native_lifecycle_dependencies_for_host() as dependencies:
                                run_directory = dependencies.make_run_directory(
                                    "linux", "x64"
                                )
                                if body_fails:
                                    raise RuntimeError("PRIVATE_BODY_VALUE")

                    self.assertEqual(str(raised.exception), "driver_failed")
                    self.assertNotIn("PRIVATE_", str(raised.exception))
                finally:
                    if run_directory is not None and run_directory.exists():
                        real_rmdir(run_directory)

    def test_generate_cannot_accept_a_caller_executor_or_write_real_evidence(
        self,
    ) -> None:
        from scripts.native_lifecycle_evidence import generate_lifecycle_evidence

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "UsageHub-0.8.6-win-x64.exe"
            artifact.write_bytes(b"final NSIS bytes")
            output = root / "native-lifecycle.json"

            with self.assertRaises(TypeError):
                generate_lifecycle_evidence(
                    platform="win",
                    arch="x64",
                    artifact=artifact,
                    source_commit=SOURCE_COMMIT,
                    output=output,
                    executor=RecordingExecutor(),
                )

            self.assertFalse(output.exists())

    def test_linux_native_executor_runs_product_managed_lifecycle_from_audited_copy(
        self,
    ) -> None:
        from scripts.native_lifecycle_evidence import (
            NativeLedgerState,
            NativeLifecycleDependencies,
            NativeLifecycleExecutor,
            NativeListenerState,
            NativePackagePaths,
            NativePathState,
            NativeProcessResult,
            NativeProfilePaths,
            NativeServiceState,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "UsageHub-0.8.6-linux-x86_64.AppImage"
            artifact.write_bytes(b"audited final AppImage container")
            artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
            run_directory = root / "native-run"
            execution_copy = run_directory / artifact.name
            sentinel = run_directory / "outside-product-sentinel.bin"
            state_root = root / "profile" / ".local" / "state" / "openusage-bar"
            config_root = root / "profile" / ".config" / "openusage-bar"
            runtime_root = root / "xdg-data" / "usagehub" / "runtime"
            unit = config_root.parent / "systemd" / "user" / "openusage-bar.service"
            profile = NativeProfilePaths(
                state_root, config_root, runtime_root, unit,
            )
            stable_collector = runtime_root / "openusage-collector"
            package = NativePackagePaths(
                runtime_root, None, None, stable_collector,
            )
            api_socket = state_root / "openusage.sock"
            service_command = (
                str(stable_collector),
                "daemon",
                "--interval",
                "300",
                "--api-transport",
                "unix",
                "--api-socket",
                str(api_socket),
            )
            ledger = NativeLedgerState(
                True, "ledger:file", 8192, "d" * 64, True, 7,
            )
            missing = NativePathState(False, "missing", 0, None, 0, None)
            execution_state = NativePathState(
                True,
                "file",
                artifact.stat().st_size,
                artifact_sha256,
                0o700,
                "run:appimage",
            )
            collector_state = NativePathState(
                True, "file", 4096, "c" * 64, 0o700, "runtime:collector",
            )
            path_states = {
                **{
                    purpose: missing
                    for purpose in (
                        "fresh_state_root",
                        "fresh_config_root",
                        "fresh_runtime_root",
                        "fresh_task_definition",
                        "fresh_install_root",
                        "fresh_stable_collector",
                        "fresh_execution_copy",
                        "fresh_sentinel",
                    )
                },
                "execution_copy": execution_state,
                "stable_collector": collector_state,
                "gateway_token": missing,
                "gateway_cache": missing,
                "gateway_telemetry": missing,
                "preserve_gateway_token": missing,
                "preserve_gateway_cache": missing,
                "preserve_gateway_telemetry": missing,
                "preserve_stable_collector": missing,
                "preserve_unit": missing,
                "preserve_runtime_root": missing,
                "preserve_execution_copy": missing,
                "delete_stable_collector": missing,
                "runtime_root": missing,
                "task_definition": missing,
                "state_root": missing,
                "config_root": missing,
                "delete_execution_copy": missing,
                "sentinel": NativePathState(
                    True,
                    "file",
                    artifact.stat().st_size,
                    artifact_sha256,
                    0o600,
                    "run:sentinel",
                ),
            }
            events: list[tuple[object, ...]] = []

            def make_run_directory(platform: str, arch: str) -> Path:
                events.append(("make_run_directory", platform, arch))
                run_directory.mkdir()
                return run_directory

            def copy_file(source: Path, destination: Path) -> None:
                events.append(("copy_file", source, destination))
                destination.write_bytes(source.read_bytes())

            def set_file_mode(path: Path, mode: int) -> None:
                events.append(("set_file_mode", path, mode))
                path.chmod(mode)

            def inspect_path(purpose: str, path: Path) -> NativePathState:
                events.append(("inspect_path", purpose, path))
                return path_states[purpose]

            def remove_path(path: Path) -> None:
                events.append(("remove_path", path))
                if path == execution_copy and path.exists():
                    path.unlink()

            handle_number = 0

            def start_process(argv: tuple[str, ...]) -> object:
                nonlocal handle_number
                handle_number += 1
                handle = f"appimage-{handle_number}"
                events.append(("start_process", argv, handle))
                return handle

            def stop_process(handle: object) -> None:
                events.append(("stop_process", handle))

            def run_process(
                argv: tuple[str, ...], timeout_seconds: float
            ) -> NativeProcessResult:
                events.append(("run_process", argv, timeout_seconds))
                return NativeProcessResult(
                    0,
                    b'{"checks":"passed","private":"PRIVATE_APPIMAGE"}',
                    b"PRIVATE_STDERR",
                    False,
                )

            def unexpected_registry(_key: str, _name: str) -> str:
                raise AssertionError("Linux lifecycle must not read the registry")

            def profile_paths(platform: str) -> NativeProfilePaths:
                events.append(("profile_paths", platform))
                return profile

            def package_paths(
                platform: str, observed_profile: NativeProfilePaths
            ) -> NativePackagePaths:
                events.append(("package_paths", platform, observed_profile))
                return package

            service_states = iter(
                (
                    NativeServiceState(False, False, None),
                    NativeServiceState(True, True, service_command),
                    NativeServiceState(False, False, None),
                    NativeServiceState(True, True, service_command),
                    NativeServiceState(False, False, None),
                )
            )

            def inspect_service(platform: str) -> NativeServiceState:
                events.append(("inspect_service", platform))
                return next(service_states)

            local_listeners = iter(
                (
                    NativeListenerState(False, False),
                    NativeListenerState(True, True),
                    NativeListenerState(False, False),
                    NativeListenerState(True, True),
                    NativeListenerState(False, False),
                )
            )

            def inspect_listener(
                platform: str, namespace: str
            ) -> NativeListenerState:
                events.append(("inspect_listener", platform, namespace))
                if namespace == "gateway":
                    return NativeListenerState(False, False)
                return next(local_listeners)

            ledgers = iter(
                (
                    NativeLedgerState(False, None, 0, None, False, 0),
                    ledger,
                    ledger,
                    ledger,
                    NativeLedgerState(False, None, 0, None, False, 0),
                )
            )

            def inspect_ledger(platform: str) -> NativeLedgerState:
                events.append(("inspect_ledger", platform))
                return next(ledgers)

            clock = iter((0.0, 0.1, 1.0, 1.1))

            def monotonic() -> float:
                value = next(clock)
                events.append(("monotonic", value))
                return value

            def wait(seconds: float) -> None:
                events.append(("wait", seconds))

            def network_events() -> tuple[object, ...]:
                events.append(("network_events",))
                return ()

            def credential_events() -> tuple[object, ...]:
                events.append(("credential_events",))
                return ()

            dependencies = NativeLifecycleDependencies(
                make_run_directory=make_run_directory,
                inspect_path=inspect_path,
                copy_file=copy_file,
                set_file_mode=set_file_mode,
                remove_path=remove_path,
                start_process=start_process,
                run_process=run_process,
                stop_process=stop_process,
                read_registry_value=unexpected_registry,
                profile_paths=profile_paths,
                package_paths=package_paths,
                inspect_service=inspect_service,
                inspect_listener=inspect_listener,
                inspect_ledger=inspect_ledger,
                network_events=network_events,
                credential_events=credential_events,
                monotonic=monotonic,
                wait=wait,
            )
            executor = NativeLifecycleExecutor(
                dependencies=dependencies,
                host_platform="linux",
                host_machine="x86_64",
            )

            observations = executor.execute(
                platform="linux",
                arch="x64",
                artifact=artifact,
                artifact_sha256=artifact_sha256,
            )

            self.assertEqual(observations, _observations())
            self.assertNotIn("PRIVATE_APPIMAGE", repr(observations))
            self.assertNotIn("PRIVATE_STDERR", repr(observations))
            self.assertEqual(
                [event[0] for event in events],
                [
                    "make_run_directory",
                    "profile_paths",
                    "package_paths",
                    "inspect_service",
                    "inspect_listener",
                    "inspect_listener",
                    "inspect_ledger",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "copy_file",
                    "set_file_mode",
                    "inspect_path",
                    "copy_file",
                    "start_process",
                    "monotonic",
                    "inspect_service",
                    "inspect_listener",
                    "inspect_listener",
                    "inspect_ledger",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "stop_process",
                    "run_process",
                    "inspect_service",
                    "inspect_listener",
                    "inspect_listener",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_ledger",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "remove_path",
                    "inspect_path",
                    "copy_file",
                    "set_file_mode",
                    "inspect_path",
                    "start_process",
                    "monotonic",
                    "inspect_service",
                    "inspect_listener",
                    "inspect_listener",
                    "inspect_ledger",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "stop_process",
                    "run_process",
                    "inspect_service",
                    "inspect_listener",
                    "inspect_listener",
                    "inspect_ledger",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "remove_path",
                    "inspect_path",
                    "inspect_path",
                    "network_events",
                    "credential_events",
                    "remove_path",
                ],
            )
            self.assertEqual(
                [event[1] for event in events if event[0] == "inspect_path"],
                [
                    "fresh_state_root",
                    "fresh_config_root",
                    "fresh_runtime_root",
                    "fresh_task_definition",
                    "fresh_install_root",
                    "fresh_stable_collector",
                    "fresh_execution_copy",
                    "fresh_sentinel",
                    "execution_copy",
                    "stable_collector",
                    "gateway_token",
                    "gateway_cache",
                    "gateway_telemetry",
                    "preserve_gateway_token",
                    "preserve_gateway_cache",
                    "preserve_gateway_telemetry",
                    "preserve_stable_collector",
                    "preserve_unit",
                    "preserve_runtime_root",
                    "preserve_execution_copy",
                    "execution_copy",
                    "stable_collector",
                    "gateway_token",
                    "gateway_cache",
                    "gateway_telemetry",
                    "delete_stable_collector",
                    "runtime_root",
                    "task_definition",
                    "state_root",
                    "config_root",
                    "delete_execution_copy",
                    "sentinel",
                ],
            )
            self.assertEqual(
                [
                    (event[1], event[2])
                    for event in events
                    if event[0] == "inspect_path"
                    and str(event[1]).startswith("preserve_gateway_")
                ],
                [
                    ("preserve_gateway_token", state_root / "gateway.token"),
                    (
                        "preserve_gateway_cache",
                        state_root / "gateway-cache.sqlite3",
                    ),
                    (
                        "preserve_gateway_telemetry",
                        state_root / "gateway-telemetry.sqlite3",
                    ),
                ],
            )
            process_calls = [event for event in events if event[0] == "run_process"]
            self.assertEqual(
                process_calls,
                [
                    (
                        "run_process",
                        (str(execution_copy), "--usagehub-uninstall"),
                        180.0,
                    ),
                    (
                        "run_process",
                        (
                            str(execution_copy),
                            "--usagehub-uninstall",
                            "--delete-data",
                        ),
                        180.0,
                    ),
                ],
            )
            self.assertEqual(
                [event for event in events if event[0] == "remove_path"],
                [
                    ("remove_path", execution_copy),
                    ("remove_path", execution_copy),
                    ("remove_path", run_directory),
                ],
            )

    def test_foreign_service_token_fails_closed_stops_desktop_and_writes_no_report(
        self,
    ) -> None:
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativeLifecycleExecutor,
        )

        with tempfile.TemporaryDirectory() as directory:
            fixture = WindowsFailureFixture(Path(directory), foreign_token=True)
            output = fixture.root / "native-lifecycle.json"
            native = NativeLifecycleExecutor(
                dependencies=fixture.dependencies(),
                host_platform="win32",
                host_machine="AMD64",
            )

            with self.assertRaisesRegex(
                LifecycleEvidenceError, "driver_failed"
            ) as raised:
                native.execute(
                    platform="win",
                    arch="x64",
                    artifact=fixture.artifact,
                    artifact_sha256=fixture.artifact_sha256,
                )

            self.assertFalse(output.exists())
            self.assertIn(
                ("stop_process", "active-desktop-handle"), fixture.events
            )
            self.assertNotIn("PRIVATE_FOREIGN", str(raised.exception))

    def test_cleanup_failure_fails_closed_and_writes_no_report(self) -> None:
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativeLifecycleExecutor,
        )

        with tempfile.TemporaryDirectory() as directory:
            fixture = WindowsFailureFixture(Path(directory), cleanup_failure=True)
            output = fixture.root / "native-lifecycle.json"
            native = NativeLifecycleExecutor(
                dependencies=fixture.dependencies(),
                host_platform="win32",
                host_machine="AMD64",
            )

            with self.assertRaisesRegex(
                LifecycleEvidenceError, "driver_failed"
            ) as raised:
                native.execute(
                    platform="win",
                    arch="x64",
                    artifact=fixture.artifact,
                    artifact_sha256=fixture.artifact_sha256,
                )

            self.assertFalse(output.exists())
            self.assertEqual(
                fixture.events[-1], ("remove_path", fixture.run_directory)
            )
            self.assertNotIn("PRIVATE_CLEANUP_PATH", str(raised.exception))

    def test_observation_failure_stops_active_desktop_and_writes_no_report(
        self,
    ) -> None:
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativeLifecycleExecutor,
        )

        with tempfile.TemporaryDirectory() as directory:
            fixture = WindowsFailureFixture(
                Path(directory), observation_failure=True
            )
            output = fixture.root / "native-lifecycle.json"
            native = NativeLifecycleExecutor(
                dependencies=fixture.dependencies(),
                host_platform="win32",
                host_machine="AMD64",
            )

            with self.assertRaisesRegex(
                LifecycleEvidenceError, "driver_failed"
            ) as raised:
                native.execute(
                    platform="win",
                    arch="x64",
                    artifact=fixture.artifact,
                    artifact_sha256=fixture.artifact_sha256,
                )

            self.assertFalse(output.exists())
            self.assertIn(
                ("stop_process", "active-desktop-handle"), fixture.events
            )
            self.assertNotIn("PRIVATE_OBSERVATION_PATH", str(raised.exception))

    def test_ready_first_observer_probe_is_valid_after_fresh_preflight(
        self,
    ) -> None:
        from scripts.native_lifecycle_evidence import NativeLifecycleExecutor

        with tempfile.TemporaryDirectory() as directory:
            fixture = WindowsFailureFixture(
                Path(directory), ready_first=True
            )
            native = NativeLifecycleExecutor(
                dependencies=fixture.dependencies(),
                host_platform="win32",
                host_machine="AMD64",
            )

            observations = native.execute(
                platform="win",
                arch="x64",
                artifact=fixture.artifact,
                artifact_sha256=fixture.artifact_sha256,
            )

            self.assertEqual(observations, _observations())

    def test_windows_native_executor_uses_only_external_low_level_facts(self) -> None:
        from scripts.native_lifecycle_evidence import (
            NativeLedgerState,
            NativeLifecycleDependencies,
            NativeLifecycleExecutor,
            NativeListenerState,
            NativePackagePaths,
            NativePathState,
            NativeProcessResult,
            NativeProfilePaths,
            NativeServiceState,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "UsageHub-0.8.6-win-x64.exe"
            artifact.write_bytes(b"audited final NSIS container")
            artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
            run_directory = root / "native-run"
            execution_copy = run_directory / artifact.name
            sentinel = run_directory / "outside-product-sentinel.bin"
            install_root = root / "installed" / "UsageHub"
            installed_app = install_root / "UsageHub.exe"
            installed_collector = (
                install_root
                / "resources"
                / "collector"
                / "openusage-collector.exe"
            )
            uninstaller = install_root / "Uninstall UsageHub.exe"
            api_token = root / "local-app-data" / "openusage-bar" / "api.token"
            profile_paths = NativeProfilePaths(
                root / "profile" / ".local" / "state" / "openusage-bar",
                root / "profile" / ".config" / "openusage-bar",
                api_token.parent,
                api_token.parent.parent / "openusage-bar-task.xml",
            )
            package_paths = NativePackagePaths(
                install_root,
                installed_app,
                uninstaller,
                installed_collector,
            )
            collector_sha256 = "c" * 64
            ledger_sha256 = "d" * 64
            forged_stdout = (
                b'{"checks":{"install":"passed"},'
                b'"providerCredentialReads":999,"private":"PRIVATE_CANARY"}'
            )
            events: list[tuple[object, ...]] = []

            def make_run_directory(platform: str, arch: str) -> Path:
                events.append(("make_run_directory", platform, arch))
                run_directory.mkdir()
                return run_directory

            def copy_file(source: Path, destination: Path) -> None:
                events.append(("copy_file", source, destination))
                destination.write_bytes(source.read_bytes())

            def set_file_mode(path: Path, mode: int) -> None:
                events.append(("set_file_mode", path, mode))
                path.chmod(mode)

            path_states = {
                **{
                    purpose: NativePathState(
                        False, "missing", 0, None, 0, None,
                    )
                    for purpose in (
                        "fresh_state_root",
                        "fresh_config_root",
                        "fresh_runtime_root",
                        "fresh_task_definition",
                        "fresh_install_root",
                        "fresh_installed_app",
                        "fresh_installed_uninstaller",
                        "fresh_installed_collector",
                        "fresh_execution_copy",
                        "fresh_sentinel",
                    )
                },
                "execution_copy": NativePathState(
                    True, "file", artifact.stat().st_size,
                    artifact_sha256, 0o700, "run:installer",
                ),
                "installed_collector": NativePathState(
                    True, "file", 4096, collector_sha256, 0o700,
                    "install:collector",
                ),
                "installed_app": NativePathState(
                    True, "file", 8192, "e" * 64, 0o700, "install:desktop",
                ),
                "installed_uninstaller": NativePathState(
                    True, "file", 4096, "f" * 64, 0o700,
                    "install:uninstaller",
                ),
                "preserve_installed_app": NativePathState(
                    False, "missing", 0, None, 0, None,
                ),
                "preserve_install_root": NativePathState(
                    False, "missing", 0, None, 0, None,
                ),
                "delete_installed_app": NativePathState(
                    False, "missing", 0, None, 0, None,
                ),
                "delete_install_root": NativePathState(
                    False, "missing", 0, None, 0, None,
                ),
                "gateway_token": NativePathState(
                    False, "missing", 0, None, 0, None,
                ),
                "gateway_cache": NativePathState(
                    False, "missing", 0, None, 0, None,
                ),
                "gateway_telemetry": NativePathState(
                    False, "missing", 0, None, 0, None,
                ),
                "preserve_gateway_token": NativePathState(
                    False, "missing", 0, None, 0, None,
                ),
                "preserve_gateway_cache": NativePathState(
                    False, "missing", 0, None, 0, None,
                ),
                "preserve_gateway_telemetry": NativePathState(
                    False, "missing", 0, None, 0, None,
                ),
                "state_root": NativePathState(
                    False, "missing", 0, None, 0, None,
                ),
                "config_root": NativePathState(
                    False, "missing", 0, None, 0, None,
                ),
                "runtime_root": NativePathState(
                    False, "missing", 0, None, 0, None,
                ),
                "task_definition": NativePathState(
                    False, "missing", 0, None, 0, None,
                ),
                "sentinel": NativePathState(
                    True, "file", artifact.stat().st_size,
                    artifact_sha256, 0o600, "run:sentinel",
                ),
            }

            def inspect_path(purpose: str, path: Path) -> NativePathState:
                events.append(("inspect_path", purpose, path))
                return path_states[purpose]

            process_number = 0

            def run_process(
                argv: tuple[str, ...], timeout_seconds: float
            ) -> NativeProcessResult:
                nonlocal process_number
                process_number += 1
                events.append(("run_process", argv, timeout_seconds))
                return NativeProcessResult(0, forged_stdout, b"PRIVATE_STDERR", False)

            handle_number = 0

            def start_process(argv: tuple[str, ...]) -> object:
                nonlocal handle_number
                handle_number += 1
                handle = f"desktop-{handle_number}"
                events.append(("start_process", argv, handle))
                return handle

            def stop_process(handle: object) -> None:
                events.append(("stop_process", handle))

            def read_registry_value(key: str, name: str) -> str:
                events.append(("read_registry_value", key, name))
                values = {
                    "InstallLocation": str(install_root),
                    "UninstallString": str(uninstaller),
                }
                return values[name]

            def inspect_profile_paths(platform: str) -> NativeProfilePaths:
                events.append(("profile_paths", platform))
                return profile_paths

            def inspect_package_paths(
                platform: str, observed_profile: NativeProfilePaths
            ) -> NativePackagePaths:
                events.append(("package_paths", platform, observed_profile))
                return package_paths

            service_states = iter(
                (
                    NativeServiceState(False, False, None),
                    NativeServiceState(
                        True,
                        True,
                        (
                            str(installed_collector),
                            "daemon",
                            "--interval",
                            "300",
                            "--api-transport",
                            "tcp",
                            "--api-port",
                            "17821",
                            "--api-token-path",
                            str(api_token),
                        ),
                    ),
                    NativeServiceState(False, False, None),
                    NativeServiceState(
                        True,
                        True,
                        (
                            str(installed_collector),
                            "daemon",
                            "--interval",
                            "300",
                            "--api-transport",
                            "tcp",
                            "--api-port",
                            "17821",
                            "--api-token-path",
                            str(api_token),
                        ),
                    ),
                    NativeServiceState(False, False, None),
                )
            )

            def inspect_service(platform: str) -> NativeServiceState:
                events.append(("inspect_service", platform))
                return next(service_states)

            local_listener_states = iter(
                (
                    NativeListenerState(False, False),
                    NativeListenerState(True, True),
                    NativeListenerState(False, False),
                    NativeListenerState(True, True),
                    NativeListenerState(False, False),
                )
            )

            def inspect_listener(
                platform: str, namespace: str
            ) -> NativeListenerState:
                events.append(("inspect_listener", platform, namespace))
                if namespace == "gateway":
                    return NativeListenerState(False, False)
                return next(local_listener_states)

            preserved_ledger = NativeLedgerState(
                True, "ledger:file", 8192, ledger_sha256, True, 7,
            )
            ledger_states = iter(
                (
                    NativeLedgerState(False, None, 0, None, False, 0),
                    preserved_ledger,
                    preserved_ledger,
                    preserved_ledger,
                    NativeLedgerState(False, None, 0, None, False, 0),
                )
            )

            def inspect_ledger(platform: str) -> NativeLedgerState:
                events.append(("inspect_ledger", platform))
                return next(ledger_states)

            clock = iter((0.0, 0.1, 1.0, 1.1))

            def monotonic() -> float:
                value = next(clock)
                events.append(("monotonic", value))
                return value

            def wait(seconds: float) -> None:
                events.append(("wait", seconds))

            def network_events() -> tuple[object, ...]:
                events.append(("network_events",))
                return ()

            def credential_events() -> tuple[object, ...]:
                events.append(("credential_events",))
                return ()

            def remove_path(path: Path) -> None:
                events.append(("remove_path", path))

            dependencies = NativeLifecycleDependencies(
                make_run_directory=make_run_directory,
                inspect_path=inspect_path,
                copy_file=copy_file,
                set_file_mode=set_file_mode,
                remove_path=remove_path,
                start_process=start_process,
                run_process=run_process,
                stop_process=stop_process,
                read_registry_value=read_registry_value,
                profile_paths=inspect_profile_paths,
                package_paths=inspect_package_paths,
                inspect_service=inspect_service,
                inspect_listener=inspect_listener,
                inspect_ledger=inspect_ledger,
                network_events=network_events,
                credential_events=credential_events,
                monotonic=monotonic,
                wait=wait,
            )
            executor = NativeLifecycleExecutor(
                dependencies=dependencies,
                host_platform="win32",
                host_machine="AMD64",
            )

            observations = executor.execute(
                platform="win",
                arch="x64",
                artifact=artifact,
                artifact_sha256=artifact_sha256,
            )

            self.assertEqual(observations, _observations())
            self.assertNotIn("PRIVATE_CANARY", repr(observations))
            self.assertNotIn("PRIVATE_STDERR", repr(observations))
            self.assertEqual(process_number, 4)
            self.assertEqual(handle_number, 2)
            self.assertEqual(
                [event[0] for event in events],
                [
                    "make_run_directory",
                    "profile_paths",
                    "package_paths",
                    "inspect_service",
                    "inspect_listener",
                    "inspect_listener",
                    "inspect_ledger",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "copy_file",
                    "set_file_mode",
                    "inspect_path",
                    "copy_file",
                    "run_process",
                    "read_registry_value",
                    "read_registry_value",
                    "start_process",
                    "monotonic",
                    "inspect_service",
                    "inspect_listener",
                    "inspect_listener",
                    "inspect_ledger",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "stop_process",
                    "run_process",
                    "inspect_service",
                    "inspect_listener",
                    "inspect_listener",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_ledger",
                    "inspect_path",
                    "inspect_path",
                    "run_process",
                    "read_registry_value",
                    "read_registry_value",
                    "start_process",
                    "monotonic",
                    "inspect_service",
                    "inspect_listener",
                    "inspect_listener",
                    "inspect_ledger",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "stop_process",
                    "run_process",
                    "inspect_service",
                    "inspect_listener",
                    "inspect_listener",
                    "inspect_ledger",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "inspect_path",
                    "network_events",
                    "credential_events",
                    "remove_path",
                ],
            )
            copy_events = [event for event in events if event[0] == "copy_file"]
            mode_events = [event for event in events if event[0] == "set_file_mode"]
            self.assertEqual(copy_events[0], ("copy_file", artifact, execution_copy))
            self.assertEqual(
                mode_events[0], ("set_file_mode", execution_copy, 0o700)
            )
            self.assertEqual(
                copy_events[1],
                ("copy_file", artifact, sentinel),
            )
            path_calls = [event for event in events if event[0] == "inspect_path"]
            self.assertEqual(
                [event[1] for event in path_calls],
                [
                    "fresh_state_root",
                    "fresh_config_root",
                    "fresh_runtime_root",
                    "fresh_task_definition",
                    "fresh_install_root",
                    "fresh_installed_app",
                    "fresh_installed_uninstaller",
                    "fresh_installed_collector",
                    "fresh_execution_copy",
                    "fresh_sentinel",
                    "execution_copy",
                    "installed_app",
                    "installed_uninstaller",
                    "installed_collector",
                    "gateway_token",
                    "gateway_cache",
                    "gateway_telemetry",
                    "preserve_gateway_token",
                    "preserve_gateway_cache",
                    "preserve_gateway_telemetry",
                    "preserve_installed_app",
                    "preserve_install_root",
                    "installed_app",
                    "installed_uninstaller",
                    "installed_collector",
                    "gateway_token",
                    "gateway_cache",
                    "gateway_telemetry",
                    "delete_installed_app",
                    "delete_install_root",
                    "state_root",
                    "config_root",
                    "runtime_root",
                    "task_definition",
                    "sentinel",
                ],
            )
            expected_product_paths = {
                "installed_app": installed_app,
                "installed_uninstaller": uninstaller,
                "installed_collector": installed_collector,
                "preserve_gateway_token": profile_paths.runtime_root / "gateway.token",
                "preserve_gateway_cache": profile_paths.state_root / "gateway-cache.sqlite3",
                "preserve_gateway_telemetry": (
                    profile_paths.state_root / "gateway-telemetry.sqlite3"
                ),
                "preserve_installed_app": installed_app,
                "preserve_install_root": install_root,
                "delete_installed_app": installed_app,
                "delete_install_root": install_root,
                "sentinel": sentinel,
            }
            for _, purpose, path in path_calls:
                if purpose in expected_product_paths:
                    self.assertEqual(path, expected_product_paths[purpose])
            process_calls = [event for event in events if event[0] == "run_process"]
            self.assertEqual(process_calls[0][1], (str(execution_copy), "/S"))
            self.assertEqual(process_calls[1][1], (str(uninstaller), "/S"))
            self.assertEqual(process_calls[2][1], (str(execution_copy), "/S"))
            self.assertEqual(
                process_calls[3][1],
                (str(uninstaller), "/S", "--delete-app-data"),
            )
            self.assertTrue(all(call[2] == 180.0 for call in process_calls))
            self.assertEqual(events[-1], ("remove_path", run_directory))

    def test_native_observation_results_are_closed_frozen_low_level_facts(self) -> None:
        from scripts.native_lifecycle_evidence import (
            NativeLedgerState,
            NativeListenerState,
            NativePackagePaths,
            NativePathState,
            NativeProcessResult,
            NativeProfilePaths,
            NativeServiceState,
        )

        host_root = Path(tempfile.gettempdir()).resolve() / "native-contract-root"
        profile_root = host_root / "profile"
        install_root = host_root / "Program Files" / "UsageHub"
        contracts = (
            (
                NativePathState,
                ("exists", "kind", "size_bytes", "sha256", "mode", "file_id"),
                (True, "file", 128, "a" * 64, 0o700, "volume:inode"),
            ),
            (
                NativeProcessResult,
                ("returncode", "stdout", "stderr", "timed_out"),
                (0, b"untrusted artifact output", b"", False),
            ),
            (
                NativeServiceState,
                ("registered", "active", "command"),
                (True, True, (str(install_root / "openusage-collector.exe"), "daemon")),
            ),
            (
                NativeProfilePaths,
                ("state_root", "config_root", "runtime_root", "task_definition"),
                (
                    profile_root / ".local" / "state" / "openusage-bar",
                    profile_root / ".config" / "openusage-bar",
                    profile_root / "AppData" / "Local" / "openusage-bar",
                    profile_root / "AppData" / "Local" / "openusage-bar-task.xml",
                ),
            ),
            (
                NativePackagePaths,
                ("install_root", "app", "uninstaller", "collector"),
                (
                    install_root,
                    install_root / "UsageHub.exe",
                    install_root / "Uninstall UsageHub.exe",
                    install_root / "resources" / "collector" / "openusage-collector.exe",
                ),
            ),
            (
                NativeLedgerState,
                (
                    "exists",
                    "file_id",
                    "size_bytes",
                    "sha256",
                    "integrity_ok",
                    "revision",
                ),
                (True, "volume:ledger", 4096, "b" * 64, True, 1),
            ),
            (
                NativeListenerState,
                ("active", "authenticated_ready"),
                (True, True),
            ),
        )
        for result_type, expected_fields, values in contracts:
            with self.subTest(result_type=result_type.__name__):
                self.assertEqual(
                    tuple(field.name for field in fields(result_type)),
                    expected_fields,
                )
                self.assertTrue(result_type.__dataclass_params__.frozen)
                instance = result_type(*values)
                with self.assertRaises(FrozenInstanceError):
                    setattr(instance, expected_fields[0], None)

    def test_native_profile_and_package_paths_reject_hostile_lexical_forms(
        self,
    ) -> None:
        from scripts.native_lifecycle_evidence import (
            NativePackagePaths,
            NativeProfilePaths,
        )

        host_root = Path(tempfile.gettempdir()).resolve() / "native-contract-root"
        profile_root = host_root / "profile"
        safe_profile = (
            profile_root / ".local" / "state" / "openusage-bar",
            profile_root / ".config" / "openusage-bar",
            profile_root / "AppData" / "Local" / "openusage-bar",
            profile_root / "AppData" / "Local" / "openusage-bar-task.xml",
        )
        hostile_paths = (
            profile_root / "safe" / ".." / "foreign",
            profile_root / "control\x01path",
            profile_root / "delete\x7fpath",
            profile_root / ("x" * 4097),
            Path(host_root.anchor),
        )
        for hostile in hostile_paths:
            with self.subTest(contract="profile", hostile=repr(hostile)):
                with self.assertRaisesRegex(
                    ValueError, "native profile paths invalid"
                ):
                    NativeProfilePaths(hostile, *safe_profile[1:])

        install_root = host_root / "Program Files" / "UsageHub"
        safe_app = install_root / "UsageHub.exe"
        safe_uninstaller = install_root / "Uninstall UsageHub.exe"
        safe_collector = (
            install_root
            / "resources"
            / "collector"
            / "openusage-collector.exe"
        )
        hostile_packages = (
            (Path(host_root.anchor), safe_app, safe_uninstaller, safe_collector),
            (
                install_root,
                safe_app,
                install_root / "safe" / ".." / ".." / "foreign.exe",
                safe_collector,
            ),
            (
                install_root,
                safe_app,
                safe_uninstaller,
                install_root / "resources" / "collector\x01.exe",
            ),
            (
                install_root,
                safe_app,
                safe_uninstaller,
                install_root / ("x" * 4097),
            ),
        )
        for values in hostile_packages:
            with self.subTest(contract="package", values=tuple(map(repr, values))):
                with self.assertRaisesRegex(
                    ValueError, "native package paths invalid"
                ):
                    NativePackagePaths(*values)

    def test_native_dependencies_are_closed_low_level_and_exclusive_with_driver(self) -> None:
        from scripts.native_lifecycle_evidence import (
            NativeLifecycleDependencies,
            NativeLifecycleExecutor,
        )

        expected_fields = (
            "make_run_directory",
            "inspect_path",
            "copy_file",
            "set_file_mode",
            "remove_path",
            "start_process",
            "run_process",
            "stop_process",
            "read_registry_value",
            "profile_paths",
            "package_paths",
            "inspect_service",
            "inspect_listener",
            "inspect_ledger",
            "network_events",
            "credential_events",
            "monotonic",
            "wait",
        )
        self.assertEqual(
            tuple(field.name for field in fields(NativeLifecycleDependencies)),
            expected_fields,
        )
        self.assertTrue(NativeLifecycleDependencies.__dataclass_params__.frozen)
        self.assertFalse(
            any(
                forbidden in name.casefold()
                for name in expected_fields
                for forbidden in ("passed", "checks", "observations", "report")
            )
        )

        noop = lambda *args, **kwargs: None
        dependencies = NativeLifecycleDependencies(
            **{name: noop for name in expected_fields}
        )
        with self.assertRaises(FrozenInstanceError):
            dependencies.run_process = noop
        with self.assertRaises(ValueError):
            NativeLifecycleExecutor(driver=object(), dependencies=dependencies)

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

    @unittest.skipIf(os.name == "nt", "requires native Linux path semantics")
    def test_linux_host_package_paths_are_a_pure_mapping_of_a_safe_profile(
        self,
    ) -> None:
        import stat
        from unittest.mock import patch

        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativePackagePaths,
            NativeProfilePaths,
            native_lifecycle_dependencies_for_host,
        )

        def tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
            snapshot: list[tuple[object, ...]] = []
            for path in sorted(root.rglob("*")):
                metadata = path.lstat()
                relative = path.relative_to(root).as_posix()
                if stat.S_ISLNK(metadata.st_mode):
                    payload: object = os.readlink(path)
                elif stat.S_ISREG(metadata.st_mode):
                    payload = path.read_bytes()
                else:
                    payload = None
                snapshot.append(
                    (
                        relative,
                        stat.S_IFMT(metadata.st_mode),
                        stat.S_IMODE(metadata.st_mode),
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_size,
                        metadata.st_nlink,
                        payload,
                    )
                )
            return tuple(snapshot)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            safe_root = root / "safe-profile"
            safe_profile = NativeProfilePaths(
                state_root=safe_root / "state" / "openusage-bar",
                config_root=safe_root / "config" / "openusage-bar",
                runtime_root=safe_root / "data" / "usagehub" / "runtime",
                task_definition=(
                    safe_root
                    / "config"
                    / "systemd"
                    / "user"
                    / "openusage-bar.service"
                ),
            )
            expected = NativePackagePaths(
                install_root=safe_profile.runtime_root,
                app=None,
                uninstaller=None,
                collector=safe_profile.runtime_root / "openusage-collector",
            )

            foreign_runtime = root / "foreign-runtime"
            foreign_runtime.mkdir()
            foreign_marker = foreign_runtime / "PRIVATE_FOREIGN_MARKER"
            foreign_marker.write_bytes(b"foreign remains unchanged")
            alias_root = root / "aliased-runtime"
            alias_root.symlink_to(foreign_runtime, target_is_directory=True)
            alias_profile = NativeProfilePaths(
                state_root=root / "alias-state",
                config_root=root / "alias-config",
                runtime_root=alias_root,
                task_definition=root / "alias-systemd" / "openusage-bar.service",
            )

            hostile_profile = object.__new__(NativeProfilePaths)
            object.__setattr__(
                hostile_profile, "state_root", root / "hostile-state"
            )
            object.__setattr__(
                hostile_profile, "config_root", root / "hostile-config"
            )
            object.__setattr__(
                hostile_profile,
                "runtime_root",
                root / "hostile-runtime" / ".." / "escape",
            )
            object.__setattr__(
                hostile_profile,
                "task_definition",
                root / "hostile-systemd" / "openusage-bar.service",
            )

            before = tree_snapshot(root)
            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ), native_lifecycle_dependencies_for_host() as dependencies:
                self.assertEqual(
                    dependencies.package_paths("linux", safe_profile), expected
                )
                self.assertEqual(tree_snapshot(root), before)

                for platform, profile in (
                    ("win", safe_profile),
                    (True, safe_profile),
                    ("linux", object()),
                    ("linux", hostile_profile),
                    ("linux", alias_profile),
                ):
                    with self.subTest(platform=platform, profile=type(profile)):
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_failed"
                        ) as rejected:
                            dependencies.package_paths(platform, profile)
                        self.assertEqual(str(rejected.exception), "driver_failed")
                        self.assertNotIn(str(root), str(rejected.exception))
                        self.assertEqual(tree_snapshot(root), before)

            self.assertEqual(tree_snapshot(root), before)

    @unittest.skipIf(os.name == "nt", "requires native Linux path semantics")
    def test_linux_host_profile_paths_are_a_pure_authoritative_projection(
        self,
    ) -> None:
        import stat
        from unittest.mock import patch

        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativePackagePaths,
            NativeProfilePaths,
            native_lifecycle_dependencies_for_host,
        )

        def tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
            paths = (root, *sorted(root.rglob("*")))
            snapshot: list[tuple[object, ...]] = []
            for path in paths:
                metadata = path.lstat()
                relative = "." if path == root else path.relative_to(root).as_posix()
                if stat.S_ISLNK(metadata.st_mode):
                    payload: object = os.readlink(path)
                elif stat.S_ISREG(metadata.st_mode):
                    payload = path.read_bytes()
                else:
                    payload = None
                snapshot.append(
                    (
                        relative,
                        stat.S_IFMT(metadata.st_mode),
                        stat.S_IMODE(metadata.st_mode),
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_size,
                        metadata.st_mtime_ns,
                        metadata.st_ctime_ns,
                        metadata.st_nlink,
                        payload,
                    )
                )
            return tuple(snapshot)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            home.mkdir()
            xdg_data = root / "authoritative-xdg-data"
            xdg_data.mkdir()
            foreign = root / "foreign-xdg-data"
            foreign.mkdir()
            foreign_marker = foreign / "PRIVATE_FOREIGN_MARKER"
            foreign_marker.write_bytes(b"foreign remains unchanged")
            xdg_alias = root / "aliased-xdg-data"
            xdg_alias.symlink_to(foreign, target_is_directory=True)
            authority = LifecycleStatePaths(platform="linux", home=home)
            expected_default = NativeProfilePaths(
                state_root=home / ".local" / "state" / "openusage-bar",
                config_root=home / ".config" / "openusage-bar",
                runtime_root=home / ".local" / "share" / "usagehub" / "runtime",
                task_definition=(
                    home
                    / ".config"
                    / "systemd"
                    / "user"
                    / "openusage-bar.service"
                ),
            )
            expected_xdg = NativeProfilePaths(
                state_root=expected_default.state_root,
                config_root=expected_default.config_root,
                runtime_root=xdg_data / "usagehub" / "runtime",
                task_definition=expected_default.task_definition,
            )
            before = tree_snapshot(root)

            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ), native_lifecycle_dependencies_for_host() as dependencies:
                with patch.object(
                    LifecycleStatePaths,
                    "for_current_user",
                    return_value=authority,
                ) as current_user, patch.dict(os.environ, {}, clear=True):
                    profile = dependencies.profile_paths("linux")
                current_user.assert_called_once_with(platform="linux")
                self.assertEqual(profile, expected_default)
                self.assertEqual(
                    dependencies.package_paths("linux", profile),
                    NativePackagePaths(
                        install_root=profile.runtime_root,
                        app=None,
                        uninstaller=None,
                        collector=profile.runtime_root / "openusage-collector",
                    ),
                )
                self.assertEqual(tree_snapshot(root), before)

                with patch.object(
                    LifecycleStatePaths,
                    "for_current_user",
                    return_value=authority,
                ) as current_user, patch.dict(
                    os.environ,
                    {"XDG_DATA_HOME": str(xdg_data)},
                    clear=True,
                ):
                    profile = dependencies.profile_paths("linux")
                current_user.assert_called_once_with(platform="linux")
                self.assertEqual(profile, expected_xdg)
                self.assertEqual(
                    dependencies.package_paths("linux", profile).collector,
                    xdg_data / "usagehub" / "runtime" / "openusage-collector",
                )
                self.assertEqual(tree_snapshot(root), before)

                for platform in ("win", True):
                    with self.subTest(platform=platform), patch.object(
                        LifecycleStatePaths, "for_current_user"
                    ) as current_user:
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_failed"
                        ) as rejected:
                            dependencies.profile_paths(platform)
                        self.assertEqual(str(rejected.exception), "driver_failed")
                        self.assertNotIn(str(root), str(rejected.exception))
                        current_user.assert_not_called()

                for authority_result in (
                    object(),
                    LifecycleStatePaths(platform="win32", home=home),
                ):
                    with self.subTest(authority=type(authority_result)), patch.object(
                        LifecycleStatePaths,
                        "for_current_user",
                        return_value=authority_result,
                    ), patch.dict(os.environ, {}, clear=True):
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_failed"
                        ) as rejected:
                            dependencies.profile_paths("linux")
                        self.assertEqual(str(rejected.exception), "driver_failed")
                        self.assertNotIn(str(root), str(rejected.exception))

                with patch.object(
                    LifecycleStatePaths,
                    "for_current_user",
                    side_effect=RuntimeError("PRIVATE_AUTHORITY_FAILURE"),
                ), patch.dict(os.environ, {}, clear=True):
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_failed"
                    ) as rejected:
                        dependencies.profile_paths("linux")
                    self.assertEqual(str(rejected.exception), "driver_failed")
                    self.assertNotIn("PRIVATE_", str(rejected.exception))

                for configured in (
                    "relative-xdg-data",
                    str(root / "control\nxdg-data"),
                    str(Path(root.anchor)),
                    str(xdg_alias),
                ):
                    with self.subTest(xdg=configured), patch.object(
                        LifecycleStatePaths,
                        "for_current_user",
                        return_value=authority,
                    ), patch.dict(
                        os.environ,
                        {"XDG_DATA_HOME": configured},
                        clear=True,
                    ):
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_failed"
                        ) as rejected:
                            dependencies.profile_paths("linux")
                        self.assertEqual(str(rejected.exception), "driver_failed")
                        self.assertNotIn(str(root), str(rejected.exception))
                        self.assertEqual(tree_snapshot(root), before)

            self.assertEqual(tree_snapshot(root), before)

    @unittest.skipIf(os.name == "nt", "requires POSIX dirfd and file modes")
    def test_linux_host_unimplemented_dependencies_are_driver_unavailable(
        self,
    ) -> None:
        from unittest.mock import patch

        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            native_lifecycle_dependencies_for_host,
        )

        with patch(
            "scripts.native_lifecycle_evidence.sys.platform", "linux"
        ), patch(
            "scripts.native_lifecycle_evidence.host_platform_module.machine",
            return_value="x86_64",
        ), native_lifecycle_dependencies_for_host() as dependencies:
            with self.assertRaisesRegex(
                LifecycleEvidenceError, "driver_unavailable"
            ) as unavailable:
                dependencies.inspect_service("linux")
            self.assertEqual(str(unavailable.exception), "driver_unavailable")

    def test_default_generate_fails_closed_without_a_real_platform_backend(self) -> None:
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            generate_lifecycle_evidence,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "UsageHub-0.8.6-linux-x86_64.AppImage"
            artifact.write_bytes(b"final AppImage bytes")
            output = root / "private-machine-name.json"
            with self.assertRaisesRegex(
                LifecycleEvidenceError, "host_invalid|driver_unavailable"
            ) as raised:
                generate_lifecycle_evidence(
                    platform="linux",
                    arch="x64",
                    artifact=artifact,
                    source_commit=SOURCE_COMMIT,
                    output=output,
                )

            self.assertFalse(output.exists())
            self.assertNotIn(str(root), str(raised.exception))

    def test_validator_rejects_a_failed_lifecycle_check(self) -> None:
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            validate_lifecycle_record,
        )

        failed = {
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
        failed["checks"] = {**_checks(), "firstRun": "failed"}

        with self.assertRaisesRegex(LifecycleEvidenceError, "record_invalid"):
            validate_lifecycle_record(failed)

    def test_verify_rehashes_container_binds_target_and_requires_canonical_report(self) -> None:
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            verify_lifecycle_evidence,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "UsageHub-0.8.6-linux-x86_64.AppImage"
            artifact.write_bytes(b"final AppImage bytes")
            output = root / "native-lifecycle.json"
            expected = {
                "schemaVersion": "native-lifecycle-evidence/v1",
                "object": "native.lifecycle",
                "synthetic": False,
                "releaseEligible": False,
                "observedAt": "2026-08-11T01:02:03.000000Z",
                "sourceCommit": SOURCE_COMMIT,
                "target": {
                    "platform": "linux",
                    "arch": "x64",
                    "serviceManager": "systemd_user",
                },
                "artifact": {
                    "name": artifact.name,
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    "sizeBytes": artifact.stat().st_size,
                },
                **_observations(),
            }
            output.write_bytes(
                (
                    json.dumps(
                        expected,
                        allow_nan=False,
                        ensure_ascii=True,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("ascii")
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

            output.write_bytes(
                json.dumps(expected, sort_keys=True).encode("ascii")
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
