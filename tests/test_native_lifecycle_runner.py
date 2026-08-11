from __future__ import annotations

import hashlib
import json
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
