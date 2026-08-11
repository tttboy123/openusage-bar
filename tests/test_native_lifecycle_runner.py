from __future__ import annotations

import hashlib
import json
import os
import platform
import socket as socket_module
import stat
import struct
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SOURCE_COMMIT = "a" * 40

_NETLINK_AF = 16
_NETLINK_RAW = 3
_NETLINK_SOCK_DIAG = 4
_NETLINK_DONE = 3
_NETLINK_DIAG_BY_FAMILY = 20
_NETLINK_MULTI = 0x2
_GATEWAY_PORT = 17823
_UNRELATED_PORT = 17822
_GATEWAY_FAMILIES = [2, 10, 10, 2]


def _netlink_header(
    message_type: int,
    *,
    sequence: int,
    flags: int = _NETLINK_MULTI,
    port_id: int = 0,
    declared_length: int = 16,
) -> bytes:
    return struct.pack(
        "=IHHII",
        declared_length,
        message_type,
        flags,
        sequence,
        port_id,
    )


def _netlink_done(
    sequence: int,
    *,
    flags: int = _NETLINK_MULTI,
    status: int | None = None,
    port_id: int = 0,
) -> bytes:
    if status is None:
        return _netlink_header(
            _NETLINK_DONE,
            sequence=sequence,
            flags=flags,
            port_id=port_id,
        )
    return (
        _netlink_header(
            _NETLINK_DONE,
            sequence=sequence,
            flags=flags,
            port_id=port_id,
            declared_length=20,
        )
        + struct.pack("=i", status)
    )


def _netlink_diagnostic(
    family: int,
    *,
    sequence: int,
    port: int = _UNRELATED_PORT,
    state: int = 10,
    flags: int = _NETLINK_MULTI,
    port_id: int = 0,
) -> bytes:
    body = bytearray(72)
    body[0] = family
    body[1] = state
    struct.pack_into("!H", body, 4, port)
    return (
        _netlink_header(
            _NETLINK_DIAG_BY_FAMILY,
            sequence=sequence,
            flags=flags,
            port_id=port_id,
            declared_length=16 + len(body),
        )
        + bytes(body)
    )


def _stable_netns_facts(*, final_inode: int = 41) -> tuple[object, object]:
    mode = stat.S_IFREG | 0o400
    return (
        SimpleNamespace(st_mode=mode, st_dev=31, st_ino=41),
        SimpleNamespace(st_mode=mode, st_dev=31, st_ino=final_inode),
    )


def _file_snapshot(root: Path) -> tuple[tuple[str, int, bytes], ...]:
    snapshot: list[tuple[str, int, bytes]] = []
    for path in sorted(root.rglob("*")):
        metadata = os.lstat(path)
        if stat.S_ISREG(metadata.st_mode):
            snapshot.append(
                (
                    path.relative_to(root).as_posix(),
                    metadata.st_mode,
                    path.read_bytes(),
                )
            )
    return tuple(snapshot)


def _enter_linux_host_dependencies(
    stack: ExitStack,
    authority: object,
    *,
    xdg_data_home: Path | None = None,
) -> tuple[object, Path, object, object]:
    from openusage_bar.lifecycle_state import LifecycleStatePaths
    from scripts.native_lifecycle_evidence import (
        native_lifecycle_dependencies_for_host,
    )

    stack.enter_context(
        patch("scripts.native_lifecycle_evidence.sys.platform", "linux")
    )
    stack.enter_context(
        patch(
            "scripts.native_lifecycle_evidence.host_platform_module.machine",
            return_value="x86_64",
        )
    )
    stack.enter_context(
        patch.object(
            LifecycleStatePaths,
            "for_current_user",
            return_value=authority,
        )
    )
    environment = (
        {} if xdg_data_home is None else {"XDG_DATA_HOME": str(xdg_data_home)}
    )
    stack.enter_context(patch.dict(os.environ, environment, clear=True))
    dependencies = stack.enter_context(native_lifecycle_dependencies_for_host())
    run_directory = dependencies.make_run_directory("linux", "x64")
    profile = dependencies.profile_paths("linux")
    package = dependencies.package_paths("linux", profile)
    return dependencies, run_directory, profile, package


def _native_path_identity(path: Path) -> tuple[int, int, int, int, int]:
    metadata = path.lstat()
    return (
        metadata.st_mode,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_nlink,
    )


def _prohibit_lifecycle_broad_reads_and_mutation(stack: ExitStack) -> None:
    for target in (
        "scripts.native_lifecycle_evidence.os.listdir",
        "scripts.native_lifecycle_evidence.os.scandir",
        "scripts.native_lifecycle_evidence.json.load",
        "scripts.native_lifecycle_evidence.json.loads",
        "scripts.native_lifecycle_evidence.os.mkdir",
        "scripts.native_lifecycle_evidence.os.rename",
        "scripts.native_lifecycle_evidence.os.unlink",
        "scripts.native_lifecycle_evidence.os.remove",
        "scripts.native_lifecycle_evidence.os.rmdir",
        "scripts.native_lifecycle_evidence.os.write",
    ):
        stack.enter_context(
            patch(target, side_effect=AssertionError("broad mutation forbidden"))
        )
    stack.enter_context(
        patch.object(
            Path,
            "iterdir",
            side_effect=AssertionError("directory enumeration forbidden"),
        )
    )


def _assert_install_alias_rejected_without_probe(
    test_case: unittest.TestCase,
    dependencies: object,
    install_root: Path,
) -> None:
    from scripts.native_lifecycle_evidence import LifecycleEvidenceError

    with patch.object(
        Path,
        "home",
        side_effect=AssertionError("failed proof must not arm alias"),
    ) as home_probe, patch(
        "scripts.native_lifecycle_evidence.os.open",
        side_effect=AssertionError("failed proof must not arm alias"),
    ) as open_probe, patch(
        "scripts.native_lifecycle_evidence.os.stat",
        side_effect=AssertionError("failed proof must not arm alias"),
    ) as stat_probe:
        with test_case.assertRaisesRegex(
            LifecycleEvidenceError, "driver_failed"
        ) as rejected:
            dependencies.inspect_path("fresh_install_root", install_root)
    test_case.assertEqual(str(rejected.exception), "driver_failed")
    home_probe.assert_not_called()
    open_probe.assert_not_called()
    stat_probe.assert_not_called()


def _inspect_fresh_runtime_root(
    test_case: unittest.TestCase,
    dependencies: object,
    home: Path,
    runtime_root: Path,
) -> object:
    from scripts.native_lifecycle_evidence import NativePathState

    with patch.object(Path, "home", return_value=home) as home_probe:
        state = dependencies.inspect_path("fresh_runtime_root", runtime_root)
    home_probe.assert_called_once_with()
    test_case.assertEqual(
        state,
        NativePathState(False, "missing", 0, None, 0, None),
    )
    return state


def _consume_install_alias_without_probe(
    test_case: unittest.TestCase,
    dependencies: object,
    install_root: Path,
    expected_fact: object,
) -> None:
    with patch.object(
        Path,
        "home",
        side_effect=AssertionError("alias must not reread authority"),
    ) as home_probe, patch(
        "scripts.native_lifecycle_evidence.os.open",
        side_effect=AssertionError("alias must not reprobe"),
    ) as open_probe, patch(
        "scripts.native_lifecycle_evidence.os.stat",
        side_effect=AssertionError("alias must not reprobe"),
    ) as stat_probe:
        install_state = dependencies.inspect_path("fresh_install_root", install_root)
    test_case.assertIs(install_state, expected_fact)
    home_probe.assert_not_called()
    open_probe.assert_not_called()
    stat_probe.assert_not_called()


class _SockDiagSocket:
    def __init__(
        self,
        test: unittest.TestCase,
        responses,
        *,
        port_id: object = 0,
        groups: object = 0,
        message_flags: int = 0,
        ancillary: object = None,
        source: object = (0, 0),
        payload_transform=None,
        send_delta: int = 0,
        timeout_observer=None,
        receive_observer=None,
        require_operation_timeout: bool = False,
    ) -> None:
        self.test = test
        self.responses = responses
        self.port_id = port_id
        self.groups = groups
        self.message_flags = message_flags
        self.ancillary = [] if ancillary is None else ancillary
        self.source = source
        self.payload_transform = payload_transform
        self.send_delta = send_delta
        self.timeout_observer = timeout_observer
        self.receive_observer = receive_observer
        self.require_operation_timeout = require_operation_timeout
        self.pending: list[object] = []
        self.requested_families: list[int] = []
        self.timeouts: list[float] = []
        self.armed_timeout: float | None = None
        self.successful_sends = 0
        self.send_without_timeout = False
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True

    def bind(self, address: tuple[int, int]) -> None:
        self.test.assertEqual(address, (0, 0))

    def settimeout(self, seconds: float) -> None:
        if self.timeout_observer is not None:
            self.timeout_observer(seconds)
        self.timeouts.append(seconds)
        self.armed_timeout = seconds

    def getsockname(self) -> tuple[object, object]:
        return self.port_id, self.groups

    def sendto(self, request: bytes, address: tuple[int, int]) -> int:
        self.test.assertEqual(address, (0, 0))
        self.test.assertIs(type(request), bytes)
        length, kind, flags, sequence, _port_id = struct.unpack_from(
            "=IHHII", request, 0
        )
        self.test.assertEqual(length, len(request))
        self.test.assertEqual(kind, _NETLINK_DIAG_BY_FAMILY)
        self.test.assertEqual(flags, 0x301)
        self.test.assertEqual(sequence, len(self.requested_families) + 1)
        self.test.assertEqual(_port_id, self.port_id)
        family, protocol, extension, padding = struct.unpack_from(
            "=BBBB", request, 16
        )
        self.test.assertEqual(protocol, 6)
        self.test.assertEqual((extension, padding), (0, 0))
        self.test.assertEqual(struct.unpack_from("=I", request, 20)[0], 1 << 10)
        if self.require_operation_timeout:
            if self.armed_timeout is None:
                self.send_without_timeout = True
            else:
                self.armed_timeout = None
        self.successful_sends += 1
        self.requested_families.append(family)
        self.pending.extend(self.responses(sequence, family))
        return len(request) + self.send_delta

    def recvmsg(self, _size: int):
        self.test.assertTrue(self.pending)
        if self.require_operation_timeout:
            self.test.assertIsNotNone(self.armed_timeout)
            self.armed_timeout = None
        if self.receive_observer is not None:
            self.receive_observer()
        response = self.pending.pop(0)
        if isinstance(response, BaseException):
            raise response
        self.test.assertIs(type(response), bytes)
        payload = response
        if self.payload_transform is not None:
            payload = self.payload_transform(payload)
        return payload, self.ancillary, self.message_flags, self.source


class _GatewayHarness:
    def __init__(
        self,
        test: unittest.TestCase,
        responses,
        *,
        netns_facts: tuple[object, ...] | None = None,
        socket_options: dict[str, object] | None = None,
        monotonic=None,
    ) -> None:
        self.test = test
        self.responses = responses
        self.netns_facts = netns_facts or _stable_netns_facts()
        self.socket_options = socket_options or {}
        self.monotonic = monotonic
        self.created: list[_SockDiagSocket] = []

    def __enter__(self):
        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from scripts.native_lifecycle_evidence import (
            native_lifecycle_dependencies_for_host,
        )

        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.__enter__())
        self.home = self.root / "authoritative-home"
        self.home.mkdir()
        authority = LifecycleStatePaths(platform="linux", home=self.home)

        def socket_factory(
            family: int, socket_type: int, protocol: int = 0
        ) -> _SockDiagSocket:
            self.test.assertEqual(
                (family, socket_type, protocol),
                (_NETLINK_AF, _NETLINK_RAW, _NETLINK_SOCK_DIAG),
            )
            diagnostic = _SockDiagSocket(
                self.test,
                self.responses,
                **self.socket_options,
            )
            self.created.append(diagnostic)
            return diagnostic

        self._stack = ExitStack()
        self._stack.enter_context(
            patch("scripts.native_lifecycle_evidence.sys.platform", "linux")
        )
        self._stack.enter_context(
            patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            )
        )
        self._stack.enter_context(
            patch.object(
                LifecycleStatePaths,
                "for_current_user",
                return_value=authority,
            )
        )
        self._stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self._stack.enter_context(
            patch.object(socket_module, "AF_NETLINK", _NETLINK_AF, create=True)
        )
        self._stack.enter_context(
            patch.object(socket_module, "SOCK_RAW", _NETLINK_RAW, create=True)
        )
        self._stack.enter_context(
            patch.object(
                socket_module,
                "NETLINK_SOCK_DIAG",
                _NETLINK_SOCK_DIAG,
                create=True,
            )
        )
        self._stack.enter_context(
            patch.object(socket_module, "socket", side_effect=socket_factory)
        )
        self.connect_probe = self._stack.enter_context(
            patch.object(
                socket_module,
                "create_connection",
                side_effect=AssertionError("TCP connect is not listener absence"),
            )
        )
        self.netns_probe = self._stack.enter_context(
            patch(
                "scripts.native_lifecycle_evidence.os.stat",
                side_effect=self.netns_facts,
            )
        )
        if self.monotonic is not None:
            self._stack.enter_context(
                patch.object(time, "monotonic", side_effect=self.monotonic)
            )
        self.dependencies = self._stack.enter_context(
            native_lifecycle_dependencies_for_host()
        )
        self.dependencies.profile_paths("linux")
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            return bool(self._stack.__exit__(exc_type, exc, traceback))
        finally:
            self._temporary.__exit__(exc_type, exc, traceback)


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
                if namespace in {"gateway", "gateway_default_endpoint"}:
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
                    "fresh_install_root",
                    "fresh_task_definition",
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
                    if event[0] == "inspect_listener"
                ],
                [
                    ("linux", "local"),
                    ("linux", "gateway_default_endpoint"),
                    ("linux", "local"),
                    ("linux", "gateway"),
                    ("linux", "local"),
                    ("linux", "gateway"),
                    ("linux", "local"),
                    ("linux", "gateway"),
                    ("linux", "local"),
                    ("linux", "gateway"),
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
    def test_linux_host_inspect_path_proves_only_fresh_authoritative_state_root_absent(
        self,
    ) -> None:
        import stat

        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativePathState,
            native_lifecycle_dependencies_for_host,
        )

        def tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
            snapshot: list[tuple[object, ...]] = []
            for path in (root, *sorted(root.rglob("*"))):
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
                        metadata.st_nlink,
                        payload,
                    )
                )
            return tuple(snapshot)

        def enter_dependencies(authority: LifecycleStatePaths, stack: ExitStack):
            stack.enter_context(
                patch("scripts.native_lifecycle_evidence.sys.platform", "linux")
            )
            stack.enter_context(
                patch(
                    "scripts.native_lifecycle_evidence.host_platform_module.machine",
                    return_value="x86_64",
                )
            )
            stack.enter_context(
                patch.object(
                    LifecycleStatePaths,
                    "for_current_user",
                    return_value=authority,
                )
            )
            stack.enter_context(patch.dict(os.environ, {}, clear=True))
            dependencies = stack.enter_context(
                native_lifecycle_dependencies_for_host()
            )
            run_directory = dependencies.make_run_directory("linux", "x64")
            return dependencies, run_directory

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            home.mkdir()
            authority = LifecycleStatePaths(platform="linux", home=home)
            expected_state_root = home / ".local" / "state" / "openusage-bar"

            with ExitStack() as stack:
                dependencies, _run_directory = enter_dependencies(authority, stack)
                with patch(
                    "scripts.native_lifecycle_evidence.os.open",
                    side_effect=AssertionError("order rejection must not probe"),
                ) as open_probe, patch(
                    "scripts.native_lifecycle_evidence.os.stat",
                    side_effect=AssertionError("order rejection must not probe"),
                ) as stat_probe:
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_failed"
                    ) as rejected:
                        dependencies.inspect_path(
                            "fresh_state_root", expected_state_root
                        )
                self.assertEqual(str(rejected.exception), "driver_failed")
                open_probe.assert_not_called()
                stat_probe.assert_not_called()

            before = tree_snapshot(root)
            with ExitStack() as stack:
                dependencies, _run_directory = enter_dependencies(authority, stack)
                profile = dependencies.profile_paths("linux")
                dependencies.package_paths("linux", profile)
                for purpose, path in (
                    ("fresh_config_root", profile.state_root),
                    ("fresh_state_root", root / "foreign-state"),
                    (True, profile.state_root),
                ):
                    with self.subTest(purpose=purpose, path=path), patch(
                        "scripts.native_lifecycle_evidence.os.open",
                        side_effect=AssertionError("invalid request must not probe"),
                    ) as open_probe, patch(
                        "scripts.native_lifecycle_evidence.os.stat",
                        side_effect=AssertionError("invalid request must not probe"),
                    ) as stat_probe:
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_failed"
                        ) as rejected:
                            dependencies.inspect_path(purpose, path)
                        self.assertEqual(str(rejected.exception), "driver_failed")
                        self.assertNotIn(str(root), str(rejected.exception))
                        open_probe.assert_not_called()
                        stat_probe.assert_not_called()

                private_comparisons: list[str] = []

                class HostileNonPath:
                    def __eq__(self, _other: object) -> bool:
                        private_comparisons.append("PRIVATE_EQ_SIDE_EFFECT")
                        raise RuntimeError("PRIVATE_EQ_ERROR")

                with self.subTest(case="hostile non-Path is rejected before equality"), patch(
                    "scripts.native_lifecycle_evidence.os.open",
                    side_effect=AssertionError("non-Path must not probe"),
                ) as open_probe, patch(
                    "scripts.native_lifecycle_evidence.os.stat",
                    side_effect=AssertionError("non-Path must not probe"),
                ) as stat_probe:
                    observed: BaseException | None = None
                    try:
                        dependencies.inspect_path(
                            "fresh_state_root", HostileNonPath()
                        )
                    except BaseException as error:
                        observed = error
                    self.assertEqual(private_comparisons, [])
                    self.assertIsInstance(observed, LifecycleEvidenceError)
                    assert observed is not None
                    self.assertEqual(str(observed), "driver_failed")
                    self.assertNotIn("PRIVATE_", str(observed))
                    open_probe.assert_not_called()
                    stat_probe.assert_not_called()

                self.assertEqual(
                    dependencies.inspect_path(
                        "fresh_state_root", profile.state_root
                    ),
                    NativePathState(False, "missing", 0, None, 0, None),
                )
            self.assertEqual(tree_snapshot(root), before)

        for existing_kind in ("directory", "file", "symlink"):
            with self.subTest(existing_kind=existing_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home = root / "authoritative-home"
                home.mkdir()
                authority = LifecycleStatePaths(platform="linux", home=home)
                state_root = home / ".local" / "state" / "openusage-bar"
                state_root.parent.mkdir(parents=True)
                if existing_kind == "directory":
                    state_root.mkdir()
                elif existing_kind == "file":
                    state_root.write_bytes(b"PRIVATE_EXISTING_STATE")
                else:
                    foreign = root / "PRIVATE_FOREIGN_STATE"
                    foreign.mkdir()
                    (foreign / "marker").write_bytes(b"foreign unchanged")
                    state_root.symlink_to(foreign, target_is_directory=True)
                before = tree_snapshot(root)

                with ExitStack() as stack:
                    dependencies, _run_directory = enter_dependencies(authority, stack)
                    profile = dependencies.profile_paths("linux")
                    dependencies.package_paths("linux", profile)
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as unavailable:
                        dependencies.inspect_path(
                            "fresh_state_root", profile.state_root
                        )
                    self.assertEqual(
                        str(unavailable.exception), "driver_unavailable"
                    )
                    self.assertNotIn(str(root), str(unavailable.exception))
                    self.assertNotIn("PRIVATE_", str(unavailable.exception))
                self.assertEqual(tree_snapshot(root), before)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            state_parent = home / ".local" / "state"
            state_parent.mkdir(parents=True)
            state_root = state_parent / "openusage-bar"
            foreign = root / "PRIVATE_FOREIGN_STATE"
            foreign.mkdir()
            foreign_marker = foreign / "marker"
            foreign_marker.write_bytes(b"foreign remains unchanged")
            config_marker = home / ".config" / "openusage-bar" / "marker"
            config_marker.parent.mkdir(parents=True)
            config_marker.write_bytes(b"config remains unchanged")
            authority = LifecycleStatePaths(platform="linux", home=home)
            local_identity = (
                (home / ".local").lstat().st_dev,
                (home / ".local").lstat().st_ino,
            )
            state_parent_identity = (
                state_parent.lstat().st_dev,
                state_parent.lstat().st_ino,
            )
            real_lstat = Path.lstat
            lstat_calls = 0
            swapped = False

            def swap_at_final_public_chain(
                path: Path,
                *args: object,
                **kwargs: object,
            ):
                nonlocal lstat_calls, swapped
                lstat_calls += 1
                if lstat_calls == 4 and path == home:
                    state_root.symlink_to(foreign, target_is_directory=True)
                    swapped = True
                return real_lstat(path, *args, **kwargs)

            with ExitStack() as stack:
                dependencies, run_directory = enter_dependencies(authority, stack)
                profile = dependencies.profile_paths("linux")
                dependencies.package_paths("linux", profile)
                with patch.object(
                    Path,
                    "lstat",
                    autospec=True,
                    side_effect=swap_at_final_public_chain,
                ), self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_unavailable"
                ) as unavailable:
                    dependencies.inspect_path(
                        "fresh_state_root", profile.state_root
                    )
                self.assertTrue(swapped)
                self.assertEqual(lstat_calls, 4)
                self.assertEqual(str(unavailable.exception), "driver_unavailable")
                self.assertNotIn(str(root), str(unavailable.exception))
                self.assertNotIn("PRIVATE_", str(unavailable.exception))
                self.assertEqual(list(run_directory.iterdir()), [])

            self.assertTrue(state_root.is_symlink())
            self.assertEqual(os.readlink(state_root), str(foreign))
            self.assertEqual(
                ((home / ".local").lstat().st_dev, (home / ".local").lstat().st_ino),
                local_identity,
            )
            self.assertEqual(
                (state_parent.lstat().st_dev, state_parent.lstat().st_ino),
                state_parent_identity,
            )
            self.assertEqual(
                foreign_marker.read_bytes(), b"foreign remains unchanged"
            )
            self.assertEqual(config_marker.read_bytes(), b"config remains unchanged")

    @unittest.skipIf(os.name == "nt", "requires native Linux path semantics")
    def test_linux_host_inspect_path_proves_only_fresh_authoritative_config_root_absent(
        self,
    ) -> None:
        import stat

        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativePathState,
            native_lifecycle_dependencies_for_host,
        )

        def enter_dependencies(
            authority: LifecycleStatePaths,
            stack: ExitStack,
            *,
            initialize_profile: bool,
        ):
            stack.enter_context(
                patch("scripts.native_lifecycle_evidence.sys.platform", "linux")
            )
            stack.enter_context(
                patch(
                    "scripts.native_lifecycle_evidence.host_platform_module.machine",
                    return_value="x86_64",
                )
            )
            stack.enter_context(
                patch.object(
                    LifecycleStatePaths,
                    "for_current_user",
                    return_value=authority,
                )
            )
            stack.enter_context(patch.dict(os.environ, {}, clear=True))
            dependencies = stack.enter_context(
                native_lifecycle_dependencies_for_host()
            )
            run_directory = dependencies.make_run_directory("linux", "x64")
            if not initialize_profile:
                return dependencies, run_directory, None
            profile = dependencies.profile_paths("linux")
            dependencies.package_paths("linux", profile)
            return dependencies, run_directory, profile

        def path_facts(*paths: Path) -> tuple[tuple[object, ...], ...]:
            facts: list[tuple[object, ...]] = []
            for path in paths:
                try:
                    metadata = path.lstat()
                except FileNotFoundError:
                    facts.append((path, "missing"))
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    payload: object = os.readlink(path)
                elif stat.S_ISREG(metadata.st_mode):
                    payload = path.read_bytes()
                else:
                    payload = None
                facts.append(
                    (
                        path,
                        stat.S_IFMT(metadata.st_mode),
                        stat.S_IMODE(metadata.st_mode),
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_size,
                        metadata.st_nlink,
                        payload,
                    )
                )
            return tuple(facts)

        def prohibit_enumeration_and_json(stack: ExitStack) -> None:
            for target in (
                "scripts.native_lifecycle_evidence.os.listdir",
                "scripts.native_lifecycle_evidence.os.scandir",
                "scripts.native_lifecycle_evidence.json.load",
                "scripts.native_lifecycle_evidence.json.loads",
            ):
                stack.enter_context(
                    patch(target, side_effect=AssertionError("broad read forbidden"))
                )
            stack.enter_context(
                patch.object(
                    Path,
                    "iterdir",
                    side_effect=AssertionError("directory enumeration forbidden"),
                )
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            home.mkdir()
            authority = LifecycleStatePaths(platform="linux", home=home)
            expected_config_root = home / ".config" / "openusage-bar"

            with ExitStack() as stack:
                dependencies, _run_directory, _profile = enter_dependencies(
                    authority,
                    stack,
                    initialize_profile=False,
                )
                with patch.object(
                    Path,
                    "home",
                    side_effect=AssertionError("invalid order must not read authority"),
                ) as home_probe, patch(
                    "scripts.native_lifecycle_evidence.os.open",
                    side_effect=AssertionError("invalid order must not probe"),
                ) as open_probe, patch(
                    "scripts.native_lifecycle_evidence.os.stat",
                    side_effect=AssertionError("invalid order must not probe"),
                ) as stat_probe:
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_failed"
                    ) as rejected:
                        dependencies.inspect_path(
                            "fresh_config_root", expected_config_root
                        )
                self.assertEqual(str(rejected.exception), "driver_failed")
                home_probe.assert_not_called()
                open_probe.assert_not_called()
                stat_probe.assert_not_called()

            config_parent = home / ".config"
            config_parent.mkdir()
            state_marker = home / ".local" / "state" / "marker"
            state_marker.parent.mkdir(parents=True)
            state_marker.write_bytes(b"state remains unchanged")
            observed_paths = (home, config_parent, expected_config_root, state_marker)
            before = path_facts(*observed_paths)

            with ExitStack() as stack:
                dependencies, run_directory, profile = enter_dependencies(
                    authority,
                    stack,
                    initialize_profile=True,
                )
                assert profile is not None
                for purpose, path in (
                    ("fresh_state_root", profile.config_root),
                    ("fresh_config_root", profile.state_root),
                    ("fresh_config_root", root / "foreign-config"),
                    ("fresh_config_root", object()),
                ):
                    with self.subTest(purpose=purpose, path_type=type(path)), patch.object(
                        Path,
                        "home",
                        side_effect=AssertionError("invalid request must not read authority"),
                    ) as home_probe, patch(
                        "scripts.native_lifecycle_evidence.os.open",
                        side_effect=AssertionError("invalid request must not probe"),
                    ) as open_probe, patch(
                        "scripts.native_lifecycle_evidence.os.stat",
                        side_effect=AssertionError("invalid request must not probe"),
                    ) as stat_probe:
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_failed"
                        ) as rejected:
                            dependencies.inspect_path(purpose, path)
                        self.assertEqual(str(rejected.exception), "driver_failed")
                        self.assertNotIn(str(root), str(rejected.exception))
                        home_probe.assert_not_called()
                        open_probe.assert_not_called()
                        stat_probe.assert_not_called()

                with ExitStack() as probe_stack:
                    home_probe = probe_stack.enter_context(
                        patch.object(Path, "home", return_value=home)
                    )
                    prohibit_enumeration_and_json(probe_stack)
                    self.assertEqual(
                        dependencies.inspect_path(
                            "fresh_config_root", profile.config_root
                        ),
                        NativePathState(False, "missing", 0, None, 0, None),
                    )
                home_probe.assert_called_once_with()
                self.assertEqual(list(run_directory.iterdir()), [])
            self.assertEqual(path_facts(*observed_paths), before)

        for authority_case in ("mismatch", "error", "hostile_non_path"):
            with self.subTest(authority_case=authority_case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home = root / "authoritative-home"
                home.mkdir()
                config_parent = home / ".config"
                config_parent.mkdir()
                authority = LifecycleStatePaths(platform="linux", home=home)
                expected_config_root = config_parent / "openusage-bar"
                before = path_facts(home, config_parent, expected_config_root)
                with ExitStack() as stack:
                    dependencies, _run_directory, profile = enter_dependencies(
                        authority,
                        stack,
                        initialize_profile=True,
                    )
                    assert profile is not None
                    hostile_equalities: list[str] = []

                    class HostileHome:
                        def __eq__(self, _other: object) -> bool:
                            hostile_equalities.append("PRIVATE_HOME_EQ")
                            raise RuntimeError("PRIVATE_HOME_EQ_ERROR")

                    if authority_case == "mismatch":
                        home_behavior = {"return_value": root / "foreign-home"}
                    elif authority_case == "error":
                        home_behavior = {
                            "side_effect": PermissionError("PRIVATE_HOME_ERROR")
                        }
                    else:
                        home_behavior = {"return_value": HostileHome()}
                    with patch.object(Path, "home", **home_behavior) as home_probe, self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as unavailable:
                        dependencies.inspect_path(
                            "fresh_config_root", profile.config_root
                        )
                    home_probe.assert_called_once_with()
                    self.assertEqual(
                        str(unavailable.exception), "driver_unavailable"
                    )
                    self.assertNotIn(str(root), str(unavailable.exception))
                    self.assertNotIn("PRIVATE_", str(unavailable.exception))
                    self.assertEqual(hostile_equalities, [])
                self.assertEqual(
                    path_facts(home, config_parent, expected_config_root), before
                )

        for existing_kind in ("directory", "file", "symlink"):
            with self.subTest(existing_kind=existing_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home = root / "authoritative-home"
                config_root = home / ".config" / "openusage-bar"
                config_root.parent.mkdir(parents=True)
                foreign_marker: Path | None = None
                if existing_kind == "directory":
                    config_root.mkdir()
                elif existing_kind == "file":
                    config_root.write_bytes(b"PRIVATE_EXISTING_CONFIG")
                else:
                    foreign = root / "PRIVATE_FOREIGN_CONFIG"
                    foreign.mkdir()
                    foreign_marker = foreign / "marker"
                    foreign_marker.write_bytes(b"foreign remains unchanged")
                    config_root.symlink_to(foreign, target_is_directory=True)
                authority = LifecycleStatePaths(platform="linux", home=home)
                before = path_facts(home, config_root.parent, config_root)
                if foreign_marker is not None:
                    before += path_facts(foreign_marker)

                with ExitStack() as stack:
                    dependencies, _run_directory, profile = enter_dependencies(
                        authority,
                        stack,
                        initialize_profile=True,
                    )
                    assert profile is not None
                    with ExitStack() as probe_stack:
                        home_probe = probe_stack.enter_context(
                            patch.object(Path, "home", return_value=home)
                        )
                        prohibit_enumeration_and_json(probe_stack)
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_unavailable"
                        ) as unavailable:
                            dependencies.inspect_path(
                                "fresh_config_root", profile.config_root
                            )
                    home_probe.assert_called_once_with()
                    self.assertEqual(
                        str(unavailable.exception), "driver_unavailable"
                    )
                    self.assertNotIn(str(root), str(unavailable.exception))
                    self.assertNotIn("PRIVATE_", str(unavailable.exception))
                after = path_facts(home, config_root.parent, config_root)
                if foreign_marker is not None:
                    after += path_facts(foreign_marker)
                self.assertEqual(after, before)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            config_parent = home / ".config"
            config_parent.mkdir(parents=True)
            config_root = config_parent / "openusage-bar"
            foreign = root / "PRIVATE_FOREIGN_CONFIG"
            foreign.mkdir()
            foreign_marker = foreign / "marker"
            foreign_marker.write_bytes(b"foreign remains unchanged")
            state_marker = home / ".local" / "state" / "marker"
            state_marker.parent.mkdir(parents=True)
            state_marker.write_bytes(b"state remains unchanged")
            authority = LifecycleStatePaths(platform="linux", home=home)
            parent_identity = (
                config_parent.lstat().st_dev,
                config_parent.lstat().st_ino,
            )
            real_lstat = Path.lstat
            lstat_calls = 0
            swapped = False

            def swap_at_final_public_chain(
                path: Path,
                *args: object,
                **kwargs: object,
            ):
                nonlocal lstat_calls, swapped
                lstat_calls += 1
                if lstat_calls == 4 and path == home:
                    config_root.symlink_to(foreign, target_is_directory=True)
                    swapped = True
                return real_lstat(path, *args, **kwargs)

            with ExitStack() as stack:
                dependencies, run_directory, profile = enter_dependencies(
                    authority,
                    stack,
                    initialize_profile=True,
                )
                assert profile is not None
                with patch.object(
                    Path,
                    "home",
                    return_value=home,
                ) as home_probe, patch.object(
                    Path,
                    "lstat",
                    autospec=True,
                    side_effect=swap_at_final_public_chain,
                ), self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_unavailable"
                ) as unavailable:
                    dependencies.inspect_path(
                        "fresh_config_root", profile.config_root
                    )
                home_probe.assert_called_once_with()
                self.assertTrue(swapped)
                self.assertEqual(lstat_calls, 4)
                self.assertEqual(str(unavailable.exception), "driver_unavailable")
                self.assertNotIn(str(root), str(unavailable.exception))
                self.assertNotIn("PRIVATE_", str(unavailable.exception))
                self.assertEqual(list(run_directory.iterdir()), [])

            self.assertTrue(config_root.is_symlink())
            self.assertEqual(os.readlink(config_root), str(foreign))
            self.assertEqual(
                (config_parent.lstat().st_dev, config_parent.lstat().st_ino),
                parent_identity,
            )
            self.assertEqual(
                foreign_marker.read_bytes(), b"foreign remains unchanged"
            )
            self.assertEqual(state_marker.read_bytes(), b"state remains unchanged")

    @unittest.skipIf(os.name == "nt", "requires native Linux path semantics")
    def test_linux_host_runtime_and_install_roots_share_one_fresh_authority_fact(
        self,
    ) -> None:
        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativePathState,
            native_lifecycle_dependencies_for_host,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            runtime_parent = home / ".local" / "share" / "usagehub"
            runtime_parent.mkdir(parents=True)
            runtime_root = runtime_parent / "runtime"
            state_marker = home / ".local" / "state" / "marker"
            state_marker.parent.mkdir(parents=True)
            state_marker.write_bytes(b"state remains unchanged")
            config_marker = home / ".config" / "openusage-bar" / "marker"
            config_marker.parent.mkdir(parents=True)
            config_marker.write_bytes(b"config remains unchanged")
            authority = LifecycleStatePaths(platform="linux", home=home)
            protected_before = (
                _native_path_identity(home),
                _native_path_identity(runtime_parent),
                state_marker.read_bytes(),
                config_marker.read_bytes(),
            )

            with self.subTest(case="one proof and one zero-probe alias"), ExitStack() as stack:
                dependencies, run_directory, profile, package = (
                    _enter_linux_host_dependencies(stack, authority)
                )
                self.assertEqual(profile.runtime_root, runtime_root)
                self.assertEqual(package.install_root, runtime_root)
                with ExitStack() as probe_stack:
                    _prohibit_lifecycle_broad_reads_and_mutation(probe_stack)
                    runtime_state = _inspect_fresh_runtime_root(
                        self, dependencies, home, profile.runtime_root
                    )
                _consume_install_alias_without_probe(
                    self,
                    dependencies,
                    package.install_root,
                    runtime_state,
                )
                self.assertEqual(list(run_directory.iterdir()), [])

            with self.subTest(case="install alias cannot precede runtime proof"), ExitStack() as stack:
                dependencies, _run_directory, _profile, package = (
                    _enter_linux_host_dependencies(stack, authority)
                )
                _assert_install_alias_rejected_without_probe(
                    self, dependencies, package.install_root
                )

            protected_after = (
                _native_path_identity(home),
                _native_path_identity(runtime_parent),
                state_marker.read_bytes(),
                config_marker.read_bytes(),
            )
            self.assertEqual(protected_after, protected_before)
            self.assertFalse(runtime_root.exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            home.mkdir()
            custom_data = root / "authoritative-xdg-data"
            custom_runtime_parent = custom_data / "usagehub"
            custom_runtime_parent.mkdir(parents=True)
            custom_runtime = custom_runtime_parent / "runtime"
            marker = custom_data / "PRIVATE_XDG_MARKER"
            marker.write_bytes(b"custom XDG remains unchanged")
            authority = LifecycleStatePaths(platform="linux", home=home)
            protected_before = (
                _native_path_identity(home),
                _native_path_identity(custom_data),
                _native_path_identity(custom_runtime_parent),
                marker.read_bytes(),
            )

            with self.subTest(case="custom XDG one proof and alias"), ExitStack() as stack:
                dependencies, _run_directory, profile, package = (
                    _enter_linux_host_dependencies(
                        stack,
                        authority,
                        xdg_data_home=custom_data,
                    )
                )
                self.assertEqual(profile.runtime_root, custom_runtime)
                self.assertEqual(package.install_root, custom_runtime)
                with ExitStack() as probe_stack:
                    _prohibit_lifecycle_broad_reads_and_mutation(probe_stack)
                    runtime_state = _inspect_fresh_runtime_root(
                        self, dependencies, home, profile.runtime_root
                    )
                _consume_install_alias_without_probe(
                    self,
                    dependencies,
                    package.install_root,
                    runtime_state,
                )

            with self.subTest(case="custom XDG drift clears alias token"), ExitStack() as stack:
                dependencies, _run_directory, profile, package = (
                    _enter_linux_host_dependencies(
                        stack,
                        authority,
                        xdg_data_home=custom_data,
                    )
                )
                drifted_data = root / "drifted-xdg-data"
                with patch.dict(
                    os.environ,
                    {"XDG_DATA_HOME": str(drifted_data)},
                    clear=True,
                ), patch.object(
                    Path, "home", return_value=home
                ) as home_probe, self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_unavailable"
                ) as unavailable:
                    dependencies.inspect_path(
                        "fresh_runtime_root", profile.runtime_root
                    )
                home_probe.assert_called_once_with()
                self.assertEqual(str(unavailable.exception), "driver_unavailable")
                self.assertNotIn(str(root), str(unavailable.exception))
                _assert_install_alias_rejected_without_probe(
                    self, dependencies, package.install_root
                )

            protected_after = (
                _native_path_identity(home),
                _native_path_identity(custom_data),
                _native_path_identity(custom_runtime_parent),
                marker.read_bytes(),
            )
            self.assertEqual(protected_after, protected_before)
            self.assertFalse(custom_runtime.exists())

    @unittest.skipIf(os.name == "nt", "requires native Linux path semantics")
    def test_linux_host_runtime_root_rejects_custom_xdg_symlink_ancestor(
        self,
    ) -> None:
        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            native_lifecycle_dependencies_for_host,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            home.mkdir()
            foreign = root / "PRIVATE_FOREIGN_XDG"
            foreign.mkdir()
            marker = foreign / "PRIVATE_XDG_MARKER"
            marker.write_bytes(b"foreign XDG remains unchanged")
            ancestor = root / "configured-xdg-parent"
            ancestor.symlink_to(foreign, target_is_directory=True)
            configured_xdg = ancestor / "missing-child"
            authority = LifecycleStatePaths(platform="linux", home=home)
            foreign_identity = foreign.lstat()
            ancestor_identity = ancestor.lstat()

            with ExitStack() as stack:
                dependencies, run_directory, profile, package = (
                    _enter_linux_host_dependencies(
                        stack,
                        authority,
                        xdg_data_home=configured_xdg,
                    )
                )
                with patch.object(Path, "home", return_value=home):
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as unavailable:
                        dependencies.inspect_path(
                            "fresh_runtime_root", profile.runtime_root
                        )
                self.assertEqual(str(unavailable.exception), "driver_unavailable")
                self.assertNotIn(str(root), str(unavailable.exception))
                self.assertNotIn("PRIVATE", str(unavailable.exception))

                _assert_install_alias_rejected_without_probe(
                    self, dependencies, package.install_root
                )
                self.assertEqual(list(run_directory.iterdir()), [])

            self.assertEqual(
                (foreign.lstat().st_dev, foreign.lstat().st_ino),
                (foreign_identity.st_dev, foreign_identity.st_ino),
            )
            self.assertEqual(
                (ancestor.lstat().st_dev, ancestor.lstat().st_ino),
                (ancestor_identity.st_dev, ancestor_identity.st_ino),
            )
            self.assertTrue(ancestor.is_symlink())
            self.assertFalse(configured_xdg.exists())
            self.assertEqual(marker.read_bytes(), b"foreign XDG remains unchanged")

    @unittest.skipIf(os.name == "nt", "requires native Linux path semantics")
    def test_linux_host_runtime_root_absence_rejects_existing_path_types(
        self,
    ) -> None:
        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
        )

        for authority_kind in ("default", "custom"):
            for existing_kind in ("directory", "file", "symlink"):
                with self.subTest(
                    authority=authority_kind, existing=existing_kind
                ), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    home = root / "authoritative-home"
                    home.mkdir()
                    custom_data = root / "authoritative-xdg-data"
                    xdg_data_home: Path | None
                    if authority_kind == "default":
                        runtime_parent = home / ".local" / "share" / "usagehub"
                        xdg_data_home = None
                    else:
                        custom_data.mkdir()
                        runtime_parent = custom_data / "usagehub"
                        xdg_data_home = custom_data
                    runtime_parent.mkdir(parents=True)
                    authority = LifecycleStatePaths(platform="linux", home=home)
                    foreign = root / "PRIVATE_FOREIGN_RUNTIME"
                    foreign.mkdir()
                    foreign_marker = foreign / "PRIVATE_FOREIGN_MARKER"
                    foreign_marker.write_bytes(b"foreign runtime remains unchanged")

                    with ExitStack() as stack:
                        dependencies, run_directory, profile, package = (
                            _enter_linux_host_dependencies(
                                stack,
                                authority,
                                xdg_data_home=xdg_data_home,
                            )
                        )
                        runtime_root = profile.runtime_root
                        if existing_kind == "directory":
                            runtime_root.mkdir()
                            protected_marker = runtime_root / "PRIVATE_RUNTIME_MARKER"
                            protected_marker.write_bytes(b"directory remains unchanged")
                        elif existing_kind == "file":
                            runtime_root.write_bytes(b"file remains unchanged")
                            protected_marker = runtime_root
                        else:
                            runtime_root.symlink_to(foreign, target_is_directory=True)
                            protected_marker = foreign_marker
                        before = runtime_root.lstat()
                        before_payload = protected_marker.read_bytes()

                        with patch.object(Path, "home", return_value=home):
                            with self.assertRaisesRegex(
                                LifecycleEvidenceError, "driver_unavailable"
                            ) as unavailable:
                                dependencies.inspect_path(
                                    "fresh_runtime_root", runtime_root
                                )
                        self.assertEqual(
                            str(unavailable.exception), "driver_unavailable"
                        )
                        self.assertNotIn(str(root), str(unavailable.exception))

                        _assert_install_alias_rejected_without_probe(
                            self, dependencies, package.install_root
                        )
                        after = runtime_root.lstat()
                        self.assertEqual(
                            (after.st_dev, after.st_ino, after.st_mode),
                            (before.st_dev, before.st_ino, before.st_mode),
                        )
                        self.assertEqual(protected_marker.read_bytes(), before_payload)
                        self.assertEqual(list(run_directory.iterdir()), [])

                    self.assertEqual(foreign_marker.read_bytes(), b"foreign runtime remains unchanged")

    @unittest.skipIf(os.name == "nt", "requires native Linux path semantics")
    def test_linux_host_runtime_root_rejects_final_missing_entry_swap(
        self,
    ) -> None:
        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            home.mkdir()
            custom_data = root / "authoritative-xdg-data"
            (custom_data / "usagehub").mkdir(parents=True)
            foreign = root / "PRIVATE_FOREIGN_RUNTIME"
            foreign.mkdir()
            marker = foreign / "PRIVATE_FOREIGN_MARKER"
            marker.write_bytes(b"final swap remains unchanged")
            authority = LifecycleStatePaths(platform="linux", home=home)
            with ExitStack() as stack:
                dependencies, run_directory, profile, package = (
                    _enter_linux_host_dependencies(
                        stack,
                        authority,
                        xdg_data_home=custom_data,
                    )
                )
                real_stat = os.stat
                missing_checks = 0

                def swap_before_final_missing(
                    path: object, *args: object, **kwargs: object
                ) -> os.stat_result:
                    nonlocal missing_checks
                    if (
                        path == "runtime"
                        and kwargs.get("dir_fd") is not None
                        and kwargs.get("follow_symlinks") is False
                    ):
                        missing_checks += 1
                        if missing_checks == 2:
                            profile.runtime_root.symlink_to(
                                foreign, target_is_directory=True
                            )
                    return real_stat(path, *args, **kwargs)

                with patch.object(Path, "home", return_value=home), patch(
                    "scripts.native_lifecycle_evidence.os.stat",
                    side_effect=swap_before_final_missing,
                ):
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as unavailable:
                        dependencies.inspect_path(
                            "fresh_runtime_root", profile.runtime_root
                        )
                self.assertEqual(missing_checks, 2)
                self.assertEqual(str(unavailable.exception), "driver_unavailable")
                self.assertNotIn(str(root), str(unavailable.exception))
                self.assertTrue(profile.runtime_root.is_symlink())
                self.assertEqual(marker.read_bytes(), b"final swap remains unchanged")

                _assert_install_alias_rejected_without_probe(
                    self, dependencies, package.install_root
                )
                self.assertEqual(list(run_directory.iterdir()), [])

            self.assertEqual(marker.read_bytes(), b"final swap remains unchanged")

    @unittest.skipIf(os.name == "nt", "requires native Linux path semantics")
    def test_linux_host_inspect_path_proves_only_fresh_authoritative_task_definition_absent(
        self,
    ) -> None:
        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativePathState,
            native_lifecycle_dependencies_for_host,
        )

        def prohibit_task_side_effects(stack: ExitStack) -> None:
            _prohibit_lifecycle_broad_reads_and_mutation(stack)
            stack.enter_context(
                patch.object(
                    Path,
                    "read_bytes",
                    side_effect=AssertionError("unit content read forbidden"),
                )
            )
            stack.enter_context(
                patch.object(
                    Path,
                    "read_text",
                    side_effect=AssertionError("unit content read forbidden"),
                )
            )
            stack.enter_context(
                patch(
                    "openusage_bar.platform_services.service_is_registered",
                    side_effect=AssertionError("service manager probe forbidden"),
                )
            )

        for xdg_config in (None, ""):
            with self.subTest(case="missing", xdg_config=xdg_config), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home = root / "authoritative-home"
                (home / ".config" / "systemd" / "user").mkdir(parents=True)
                authority = LifecycleStatePaths(platform="linux", home=home)
                with ExitStack() as stack:
                    dependencies, run_directory, profile, _package = (
                        _enter_linux_host_dependencies(stack, authority)
                    )
                    self.assertEqual(
                        profile.task_definition,
                        home
                        / ".config"
                        / "systemd"
                        / "user"
                        / "openusage-bar.service",
                    )
                    environment = (
                        {} if xdg_config is None else {"XDG_CONFIG_HOME": ""}
                    )
                    with ExitStack() as probe_stack:
                        probe_stack.enter_context(
                            patch.dict(os.environ, environment, clear=True)
                        )
                        home_probe = probe_stack.enter_context(
                            patch.object(Path, "home", return_value=home)
                        )
                        prohibit_task_side_effects(probe_stack)
                        state = dependencies.inspect_path(
                            "fresh_task_definition", profile.task_definition
                        )
                    home_probe.assert_called_once_with()
                    self.assertEqual(
                        state,
                        NativePathState(False, "missing", 0, None, 0, None),
                    )
                    self.assertEqual(list(run_directory.iterdir()), [])

        with self.subTest(case="before initialization"), patch(
            "scripts.native_lifecycle_evidence.sys.platform", "linux"
        ), patch(
            "scripts.native_lifecycle_evidence.host_platform_module.machine",
            return_value="x86_64",
        ), native_lifecycle_dependencies_for_host() as dependencies, patch(
            "scripts.native_lifecycle_evidence.os.open",
            side_effect=AssertionError("invalid order must not probe"),
        ) as open_probe, patch(
            "scripts.native_lifecycle_evidence.os.stat",
            side_effect=AssertionError("invalid order must not probe"),
        ) as stat_probe, self.assertRaisesRegex(
            LifecycleEvidenceError, "driver_failed"
        ) as rejected:
            dependencies.inspect_path(
                "fresh_task_definition",
                Path(tempfile.gettempdir()).resolve()
                / "uninitialized"
                / "openusage-bar.service",
            )
        self.assertEqual(str(rejected.exception), "driver_failed")
        open_probe.assert_not_called()
        stat_probe.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            (home / ".config" / "systemd" / "user").mkdir(parents=True)
            authority = LifecycleStatePaths(platform="linux", home=home)
            with ExitStack() as stack:
                dependencies, _run_directory, profile, _package = (
                    _enter_linux_host_dependencies(stack, authority)
                )
                wrong_inputs = (
                    (True, profile.task_definition),
                    ("fresh_task_definition", root / "foreign.service"),
                    ("fresh_task_definition", object()),
                )
                for purpose, path in wrong_inputs:
                    with self.subTest(case="wrong input", purpose=purpose):
                        with patch.object(
                            Path,
                            "home",
                            side_effect=AssertionError(
                                "invalid input must not read authority"
                            ),
                        ) as home_probe, patch(
                            "scripts.native_lifecycle_evidence.os.open",
                            side_effect=AssertionError("invalid input must not probe"),
                        ) as open_probe, patch(
                            "scripts.native_lifecycle_evidence.os.stat",
                            side_effect=AssertionError("invalid input must not probe"),
                        ) as stat_probe:
                            with self.assertRaisesRegex(
                                LifecycleEvidenceError, "driver_failed"
                            ) as rejected:
                                dependencies.inspect_path(purpose, path)
                        self.assertEqual(str(rejected.exception), "driver_failed")
                        home_probe.assert_not_called()
                        open_probe.assert_not_called()
                        stat_probe.assert_not_called()

        for existing_kind in ("directory", "file", "symlink"):
            with self.subTest(case="existing", kind=existing_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home = root / "authoritative-home"
                unit_parent = home / ".config" / "systemd" / "user"
                unit_parent.mkdir(parents=True)
                foreign = root / "PRIVATE_FOREIGN_UNIT"
                foreign.write_bytes(b"foreign unit remains unchanged")
                authority = LifecycleStatePaths(platform="linux", home=home)
                with ExitStack() as stack:
                    dependencies, run_directory, profile, _package = (
                        _enter_linux_host_dependencies(stack, authority)
                    )
                    unit = profile.task_definition
                    if existing_kind == "directory":
                        unit.mkdir()
                        marker = unit / "PRIVATE_MARKER"
                        marker.write_bytes(b"directory remains unchanged")
                    elif existing_kind == "file":
                        unit.write_bytes(b"unit remains unchanged")
                        marker = unit
                    else:
                        unit.symlink_to(foreign)
                        marker = foreign
                    before = unit.lstat()
                    payload = marker.read_bytes()
                    with ExitStack() as probe_stack:
                        probe_stack.enter_context(
                            patch.object(Path, "home", return_value=home)
                        )
                        prohibit_task_side_effects(probe_stack)
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_unavailable"
                        ) as unavailable:
                            dependencies.inspect_path(
                                "fresh_task_definition", unit
                            )
                    self.assertEqual(str(unavailable.exception), "driver_unavailable")
                    self.assertNotIn(str(root), str(unavailable.exception))
                    after = unit.lstat()
                    self.assertEqual(
                        (after.st_dev, after.st_ino, after.st_mode),
                        (before.st_dev, before.st_ino, before.st_mode),
                    )
                    self.assertEqual(marker.read_bytes(), payload)
                    self.assertEqual(list(run_directory.iterdir()), [])

        for uncertainty in ("home mismatch", "home error", "custom config"):
            with self.subTest(case=uncertainty), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home = root / "authoritative-home"
                (home / ".config" / "systemd" / "user").mkdir(parents=True)
                authority = LifecycleStatePaths(platform="linux", home=home)
                with ExitStack() as stack:
                    dependencies, run_directory, profile, _package = (
                        _enter_linux_host_dependencies(stack, authority)
                    )
                    if uncertainty == "home mismatch":
                        home_result: object = root / "different-home"
                        environment = {}
                    elif uncertainty == "home error":
                        home_result = RuntimeError("PRIVATE_HOME_FAILURE")
                        environment = {}
                    else:
                        home_result = home
                        environment = {
                            "XDG_CONFIG_HOME": str(root / "custom-config")
                        }
                    with patch.dict(
                        os.environ, environment, clear=True
                    ), patch.object(
                        Path,
                        "home",
                        return_value=home_result
                        if not isinstance(home_result, Exception)
                        else None,
                        side_effect=home_result
                        if isinstance(home_result, Exception)
                        else None,
                    ), patch(
                        "scripts.native_lifecycle_evidence.os.open",
                        side_effect=AssertionError("uncertain authority must not probe"),
                    ) as open_probe, patch(
                        "scripts.native_lifecycle_evidence.os.stat",
                        side_effect=AssertionError("uncertain authority must not probe"),
                    ) as stat_probe, self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as unavailable:
                        dependencies.inspect_path(
                            "fresh_task_definition", profile.task_definition
                        )
                    self.assertEqual(str(unavailable.exception), "driver_unavailable")
                    self.assertNotIn("PRIVATE", str(unavailable.exception))
                    open_probe.assert_not_called()
                    stat_probe.assert_not_called()
                    self.assertEqual(list(run_directory.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "requires native Linux path semantics")
    def test_linux_host_task_definition_rejects_final_missing_entry_swap(
        self,
    ) -> None:
        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from scripts.native_lifecycle_evidence import LifecycleEvidenceError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            unit_parent = home / ".config" / "systemd" / "user"
            unit_parent.mkdir(parents=True)
            parent_before = unit_parent.lstat()
            foreign = root / "PRIVATE_FOREIGN_UNIT"
            foreign.write_bytes(b"foreign unit remains unchanged")
            authority = LifecycleStatePaths(platform="linux", home=home)

            with ExitStack() as stack:
                dependencies, run_directory, profile, _package = (
                    _enter_linux_host_dependencies(stack, authority)
                )
                real_stat = os.stat
                missing_checks = 0

                def swap_before_final_missing(
                    path: object, *args: object, **kwargs: object
                ) -> os.stat_result:
                    nonlocal missing_checks
                    if (
                        path == "openusage-bar.service"
                        and kwargs.get("dir_fd") is not None
                        and kwargs.get("follow_symlinks") is False
                    ):
                        missing_checks += 1
                        if missing_checks == 2:
                            profile.task_definition.symlink_to(foreign)
                    return real_stat(path, *args, **kwargs)

                with patch.object(Path, "home", return_value=home), patch(
                    "scripts.native_lifecycle_evidence.os.stat",
                    side_effect=swap_before_final_missing,
                ):
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as unavailable:
                        dependencies.inspect_path(
                            "fresh_task_definition", profile.task_definition
                        )
                self.assertEqual(missing_checks, 2)
                self.assertEqual(str(unavailable.exception), "driver_unavailable")
                self.assertNotIn(str(root), str(unavailable.exception))
                self.assertNotIn("PRIVATE", str(unavailable.exception))
                self.assertTrue(profile.task_definition.is_symlink())
                self.assertEqual(os.readlink(profile.task_definition), str(foreign))
                self.assertEqual(
                    foreign.read_bytes(), b"foreign unit remains unchanged"
                )
                parent_after = unit_parent.lstat()
                self.assertEqual(
                    (parent_after.st_dev, parent_after.st_ino),
                    (parent_before.st_dev, parent_before.st_ino),
                )
                self.assertEqual(list(run_directory.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "requires native Linux path semantics")
    def test_linux_host_fresh_stable_collector_reproves_the_authoritative_runtime_root_absent(
        self,
    ) -> None:
        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativePathState,
            native_lifecycle_dependencies_for_host,
        )

        missing = NativePathState(False, "missing", 0, None, 0, None)

        for authority_kind in ("default", "custom"):
            with self.subTest(case="missing", authority=authority_kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home = root / "authoritative-home"
                (home / ".config" / "systemd" / "user").mkdir(parents=True)
                custom_data = root / "authoritative-xdg-data"
                if authority_kind == "default":
                    (home / ".local" / "share" / "usagehub").mkdir(parents=True)
                    xdg_data_home = None
                else:
                    (custom_data / "usagehub").mkdir(parents=True)
                    xdg_data_home = custom_data
                authority = LifecycleStatePaths(platform="linux", home=home)
                with ExitStack() as stack:
                    dependencies, run_directory, profile, package = (
                        _enter_linux_host_dependencies(
                            stack,
                            authority,
                            xdg_data_home=xdg_data_home,
                        )
                    )
                    self.assertEqual(
                        package.collector,
                        profile.runtime_root / "openusage-collector",
                    )
                    with patch.object(Path, "home", return_value=home):
                        self.assertEqual(
                            dependencies.inspect_path(
                                "fresh_task_definition",
                                profile.task_definition,
                            ),
                            missing,
                        )
                    with ExitStack() as probe_stack:
                        home_probe = probe_stack.enter_context(
                            patch.object(Path, "home", return_value=home)
                        )
                        _prohibit_lifecycle_broad_reads_and_mutation(probe_stack)
                        stable_state = dependencies.inspect_path(
                            "fresh_stable_collector", package.collector
                        )
                    home_probe.assert_called_once_with()
                    self.assertEqual(stable_state, missing)
                    self.assertFalse(profile.runtime_root.exists())
                    self.assertEqual(list(run_directory.iterdir()), [])

        with self.subTest(case="runtime root appears after task"), tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            (home / ".config" / "systemd" / "user").mkdir(parents=True)
            (home / ".local" / "share" / "usagehub").mkdir(parents=True)
            authority = LifecycleStatePaths(platform="linux", home=home)
            with ExitStack() as stack:
                dependencies, run_directory, profile, package = (
                    _enter_linux_host_dependencies(stack, authority)
                )
                with patch.object(Path, "home", return_value=home):
                    self.assertEqual(
                        dependencies.inspect_path(
                            "fresh_task_definition", profile.task_definition
                        ),
                        missing,
                    )
                profile.runtime_root.mkdir()
                marker = profile.runtime_root / "PRIVATE_RUNTIME_MARKER"
                marker.write_bytes(b"runtime root remains unchanged")
                with patch.object(Path, "home", return_value=home), self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_unavailable"
                ) as unavailable:
                    dependencies.inspect_path(
                        "fresh_stable_collector", package.collector
                    )
                self.assertEqual(str(unavailable.exception), "driver_unavailable")
                self.assertNotIn(str(root), str(unavailable.exception))
                self.assertNotIn("PRIVATE", str(unavailable.exception))
                self.assertFalse(package.collector.exists())
                self.assertEqual(
                    marker.read_bytes(), b"runtime root remains unchanged"
                )
                self.assertEqual(list(run_directory.iterdir()), [])

        uninitialized_collector = (
            Path(tempfile.gettempdir()).resolve()
            / "uninitialized-runtime"
            / "openusage-collector"
        )
        with self.subTest(case="before initialization"), patch(
            "scripts.native_lifecycle_evidence.sys.platform", "linux"
        ), patch(
            "scripts.native_lifecycle_evidence.host_platform_module.machine",
            return_value="x86_64",
        ), native_lifecycle_dependencies_for_host() as dependencies, patch(
            "scripts.native_lifecycle_evidence.os.open",
            side_effect=AssertionError("invalid order must not probe"),
        ) as open_probe, patch(
            "scripts.native_lifecycle_evidence.os.stat",
            side_effect=AssertionError("invalid order must not probe"),
        ) as stat_probe, self.assertRaisesRegex(
            LifecycleEvidenceError, "driver_failed"
        ) as rejected:
            dependencies.inspect_path(
                "fresh_stable_collector", uninitialized_collector
            )
        self.assertEqual(str(rejected.exception), "driver_failed")
        open_probe.assert_not_called()
        stat_probe.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            home.mkdir()
            authority = LifecycleStatePaths(platform="linux", home=home)
            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ), patch.object(
                LifecycleStatePaths,
                "for_current_user",
                return_value=authority,
            ), patch.dict(
                os.environ, {}, clear=True
            ), native_lifecycle_dependencies_for_host() as dependencies:
                dependencies.make_run_directory("linux", "x64")
                profile = dependencies.profile_paths("linux")
                with patch(
                    "scripts.native_lifecycle_evidence.os.open",
                    side_effect=AssertionError("missing package must not probe"),
                ) as open_probe, patch(
                    "scripts.native_lifecycle_evidence.os.stat",
                    side_effect=AssertionError("missing package must not probe"),
                ) as stat_probe, self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_failed"
                ) as rejected:
                    dependencies.inspect_path(
                        "fresh_stable_collector",
                        profile.runtime_root / "openusage-collector",
                    )
                self.assertEqual(str(rejected.exception), "driver_failed")
                open_probe.assert_not_called()
                stat_probe.assert_not_called()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            home.mkdir()
            authority = LifecycleStatePaths(platform="linux", home=home)
            with ExitStack() as stack:
                dependencies, run_directory, profile, package = (
                    _enter_linux_host_dependencies(stack, authority)
                )
                wrong_inputs = (
                    (True, package.collector),
                    ("fresh_stable_collector", root / "foreign-collector"),
                    ("fresh_stable_collector", object()),
                )
                for purpose, path in wrong_inputs:
                    with self.subTest(case="wrong input", purpose=purpose):
                        with patch.object(
                            Path,
                            "home",
                            side_effect=AssertionError(
                                "invalid input must not read authority"
                            ),
                        ) as home_probe, patch(
                            "scripts.native_lifecycle_evidence.os.open",
                            side_effect=AssertionError("invalid input must not probe"),
                        ) as open_probe, patch(
                            "scripts.native_lifecycle_evidence.os.stat",
                            side_effect=AssertionError("invalid input must not probe"),
                        ) as stat_probe:
                            with self.assertRaisesRegex(
                                LifecycleEvidenceError, "driver_failed"
                            ) as rejected:
                                dependencies.inspect_path(purpose, path)
                        self.assertEqual(str(rejected.exception), "driver_failed")
                        home_probe.assert_not_called()
                        open_probe.assert_not_called()
                        stat_probe.assert_not_called()
                self.assertEqual(list(run_directory.iterdir()), [])

        for uncertainty in (
            "home mismatch",
            "home error",
            "xdg data drift",
            "xdg config drift",
        ):
            with self.subTest(case=uncertainty), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home = root / "authoritative-home"
                home.mkdir()
                authority = LifecycleStatePaths(platform="linux", home=home)
                with ExitStack() as stack:
                    dependencies, run_directory, profile, package = (
                        _enter_linux_host_dependencies(stack, authority)
                    )
                    environment: dict[str, str] = {}
                    home_result: object = home
                    if uncertainty == "home mismatch":
                        home_result = root / "different-home"
                    elif uncertainty == "home error":
                        home_result = RuntimeError("PRIVATE_HOME_FAILURE")
                    elif uncertainty == "xdg data drift":
                        environment["XDG_DATA_HOME"] = str(root / "drifted-data")
                    else:
                        environment["XDG_CONFIG_HOME"] = str(root / "drifted-config")
                    with patch.dict(
                        os.environ, environment, clear=True
                    ), patch.object(
                        Path,
                        "home",
                        return_value=home_result
                        if not isinstance(home_result, Exception)
                        else None,
                        side_effect=home_result
                        if isinstance(home_result, Exception)
                        else None,
                    ), patch(
                        "scripts.native_lifecycle_evidence.os.open",
                        side_effect=AssertionError("uncertain authority must not probe"),
                    ) as open_probe, patch(
                        "scripts.native_lifecycle_evidence.os.stat",
                        side_effect=AssertionError("uncertain authority must not probe"),
                    ) as stat_probe, self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as unavailable:
                        dependencies.inspect_path(
                            "fresh_stable_collector", package.collector
                        )
                    self.assertEqual(str(unavailable.exception), "driver_unavailable")
                    self.assertNotIn("PRIVATE", str(unavailable.exception))
                    open_probe.assert_not_called()
                    stat_probe.assert_not_called()
                    self.assertEqual(list(run_directory.iterdir()), [])

    def test_fresh_baseline_consumes_linux_runtime_install_alias_before_task_definition(
        self,
    ) -> None:
        from scripts.native_lifecycle_evidence import (
            NativeLedgerState,
            NativeLifecycleDependencies,
            NativeListenerState,
            NativePackagePaths,
            NativePathState,
            NativeProfilePaths,
            NativeServiceState,
            _require_fresh_baseline,
        )

        root = Path(tempfile.gettempdir()).resolve() / "fresh-baseline-order"
        missing = NativePathState(False, "missing", 0, None, 0, None)
        service_absent = NativeServiceState(False, False, None)
        listener_absent = NativeListenerState(False, False)
        ledger_absent = NativeLedgerState(False, None, 0, None, False, 0)

        for target_platform in ("linux", "win"):
            with self.subTest(platform=target_platform):
                events: list[tuple[object, ...]] = []
                profile = NativeProfilePaths(
                    state_root=root / target_platform / "state",
                    config_root=root / target_platform / "config",
                    runtime_root=root / target_platform / "runtime",
                    task_definition=root / target_platform / "service-definition",
                )
                if target_platform == "linux":
                    package = NativePackagePaths(
                        install_root=profile.runtime_root,
                        app=None,
                        uninstaller=None,
                        collector=profile.runtime_root / "openusage-collector",
                    )
                    expected_purposes = (
                        "fresh_state_root",
                        "fresh_config_root",
                        "fresh_runtime_root",
                        "fresh_install_root",
                        "fresh_task_definition",
                        "fresh_stable_collector",
                        "fresh_execution_copy",
                        "fresh_sentinel",
                    )
                else:
                    install_root = root / "win" / "UsageHub"
                    package = NativePackagePaths(
                        install_root=install_root,
                        app=install_root / "UsageHub.exe",
                        uninstaller=install_root / "Uninstall UsageHub.exe",
                        collector=(
                            install_root
                            / "resources"
                            / "collector"
                            / "openusage-collector.exe"
                        ),
                    )
                    expected_purposes = (
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

                def inspect_path(purpose: str, path: Path) -> NativePathState:
                    events.append(("inspect_path", purpose, path))
                    return missing

                noop = lambda *args, **kwargs: None
                dependencies = NativeLifecycleDependencies(
                    make_run_directory=noop,
                    inspect_path=inspect_path,
                    copy_file=noop,
                    set_file_mode=noop,
                    remove_path=noop,
                    start_process=noop,
                    run_process=noop,
                    stop_process=noop,
                    read_registry_value=noop,
                    profile_paths=noop,
                    package_paths=noop,
                    inspect_service=lambda platform: service_absent,
                    inspect_listener=lambda platform, namespace: listener_absent,
                    inspect_ledger=lambda platform: ledger_absent,
                    network_events=noop,
                    credential_events=noop,
                    monotonic=noop,
                    wait=noop,
                )
                _require_fresh_baseline(
                    dependencies,
                    platform=target_platform,
                    profile_paths=profile,
                    package_paths=package,
                    execution_copy=root / target_platform / "execution-copy",
                    sentinel=root / target_platform / "sentinel",
                )
                self.assertEqual(
                    tuple(event[1] for event in events),
                    expected_purposes,
                )

    def test_linux_default_executor_baseline_consumes_only_default_gateway_endpoint_absence(
        self,
    ) -> None:
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativeLedgerState,
            NativeLifecycleDependencies,
            NativeLifecycleExecutor,
            NativeListenerState,
            NativePackagePaths,
            NativePathState,
            NativeProfilePaths,
            NativeServiceState,
        )

        root = Path(tempfile.gettempdir()).resolve() / "linux-baseline-endpoint"
        run_directory = root / "run"
        profile = NativeProfilePaths(
            state_root=root / "state",
            config_root=root / "config",
            runtime_root=root / "runtime",
            task_definition=root / "openusage-bar.service",
        )
        package = NativePackagePaths(
            install_root=profile.runtime_root,
            app=None,
            uninstaller=None,
            collector=profile.runtime_root / "openusage-collector",
        )
        missing = NativePathState(False, "missing", 0, None, 0, None)
        events: list[tuple[object, ...]] = []

        def make_run_directory(platform: str, arch: str) -> Path:
            events.append(("make_run_directory", platform, arch))
            return run_directory

        def profile_paths(platform: str) -> NativeProfilePaths:
            events.append(("profile_paths", platform))
            return profile

        def package_paths(
            platform: str, observed_profile: NativeProfilePaths
        ) -> NativePackagePaths:
            events.append(("package_paths", platform, observed_profile))
            return package

        def inspect_service(platform: str) -> NativeServiceState:
            events.append(("inspect_service", platform))
            return NativeServiceState(False, False, None)

        def inspect_listener(
            platform: str, namespace: str
        ) -> NativeListenerState:
            events.append(("inspect_listener", platform, namespace))
            if namespace == "gateway":
                raise AssertionError("generic Gateway must remain unavailable")
            return NativeListenerState(False, False)

        def inspect_path(purpose: str, path: Path) -> NativePathState:
            events.append(("inspect_path", purpose, path))
            return missing

        def inspect_ledger(platform: str) -> NativeLedgerState:
            events.append(("inspect_ledger", platform))
            return NativeLedgerState(False, None, 0, None, False, 0)

        def copy_blocker(source: Path, destination: Path) -> None:
            events.append(("copy_file", source, destination))
            raise LifecycleEvidenceError("driver_unavailable")

        def remove_path(path: Path) -> None:
            events.append(("remove_path", path))

        dependencies = NativeLifecycleDependencies(
            make_run_directory=make_run_directory,
            inspect_path=inspect_path,
            copy_file=copy_blocker,
            set_file_mode=lambda *args: None,
            remove_path=remove_path,
            start_process=lambda *args: None,
            run_process=lambda *args: None,
            stop_process=lambda *args: None,
            read_registry_value=lambda *args: None,
            profile_paths=profile_paths,
            package_paths=package_paths,
            inspect_service=inspect_service,
            inspect_listener=inspect_listener,
            inspect_ledger=inspect_ledger,
            network_events=lambda: (),
            credential_events=lambda: (),
            monotonic=lambda: 0.0,
            wait=lambda seconds: None,
        )
        executor = NativeLifecycleExecutor(
            dependencies=dependencies,
            host_platform="linux",
            host_machine="x86_64",
        )

        with self.assertRaisesRegex(
            LifecycleEvidenceError, "driver_unavailable"
        ):
            executor.execute(
                platform="linux",
                arch="x64",
                artifact=root / "OpenUsage-Bar.AppImage",
                artifact_sha256="a" * 64,
            )

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
                "remove_path",
            ],
        )
        self.assertEqual(
            [event for event in events if event[0] == "inspect_listener"],
            [
                ("inspect_listener", "linux", "local"),
                ("inspect_listener", "linux", "gateway_default_endpoint"),
            ],
        )
        self.assertEqual(
            [event[1] for event in events if event[0] == "inspect_path"],
            [
                "fresh_state_root",
                "fresh_config_root",
                "fresh_runtime_root",
                "fresh_install_root",
                "fresh_task_definition",
                "fresh_stable_collector",
                "fresh_execution_copy",
                "fresh_sentinel",
            ],
        )
        self.assertEqual(
            [event[0] for event in events[-2:]],
            ["copy_file", "remove_path"],
        )

    @unittest.skipIf(os.name == "nt", "requires native Linux path semantics")
    def test_linux_host_runtime_install_token_is_revoked_by_service_observation(
        self,
    ) -> None:
        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from scripts.native_lifecycle_evidence import (
            NativeServiceState,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            runtime_parent = home / ".local" / "share" / "usagehub"
            runtime_parent.mkdir(parents=True)
            runtime_root = runtime_parent / "runtime"
            marker = home / "PRIVATE_RUNTIME_TOKEN_MARKER"
            marker.write_bytes(b"token observation is read-only")
            authority = LifecycleStatePaths(platform="linux", home=home)

            with ExitStack() as stack:
                dependencies, run_directory, profile, package = (
                    _enter_linux_host_dependencies(stack, authority)
                )
                _inspect_fresh_runtime_root(
                    self, dependencies, home, profile.runtime_root
                )

                with patch(
                    "openusage_bar.platform_services.service_is_registered",
                    return_value=False,
                ) as service_probe:
                    self.assertEqual(
                        dependencies.inspect_service("linux"),
                        NativeServiceState(False, False, None),
                    )
                service_probe.assert_called_once_with(platform="linux", home=home)

                _assert_install_alias_rejected_without_probe(
                    self, dependencies, package.install_root
                )
                self.assertEqual(list(run_directory.iterdir()), [])

            self.assertFalse(runtime_root.exists())
            self.assertEqual(
                marker.read_bytes(), b"token observation is read-only"
            )

    @unittest.skipIf(os.name == "nt", "requires native Linux path semantics")
    def test_linux_host_runtime_install_token_is_revoked_by_other_observations(
        self,
    ) -> None:
        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
        )

        observations = (
            ("listener", lambda dependencies: dependencies.inspect_listener("linux", "local")),
            ("ledger", lambda dependencies: dependencies.inspect_ledger("linux")),
        )
        for case, observe in observations:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home = root / "authoritative-home"
                runtime_parent = home / ".local" / "share" / "usagehub"
                runtime_parent.mkdir(parents=True)
                runtime_root = runtime_parent / "runtime"
                marker = home / "PRIVATE_RUNTIME_TOKEN_MARKER"
                marker.write_bytes(b"failed observation revokes the token")
                authority = LifecycleStatePaths(platform="linux", home=home)

                with ExitStack() as stack:
                    dependencies, run_directory, profile, package = (
                        _enter_linux_host_dependencies(stack, authority)
                    )
                    _inspect_fresh_runtime_root(
                        self, dependencies, home, profile.runtime_root
                    )

                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_failed"
                    ) as observation_rejected:
                        observe(dependencies)
                    self.assertEqual(
                        str(observation_rejected.exception), "driver_failed"
                    )

                    _assert_install_alias_rejected_without_probe(
                        self, dependencies, package.install_root
                    )
                    self.assertEqual(list(run_directory.iterdir()), [])

                self.assertFalse(runtime_root.exists())
                self.assertEqual(
                    marker.read_bytes(), b"failed observation revokes the token"
                )

    @unittest.skipIf(os.name == "nt", "requires native Linux path semantics")
    def test_linux_host_runtime_install_token_is_revoked_by_remaining_callbacks(
        self,
    ) -> None:
        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from scripts.native_lifecycle_evidence import LifecycleEvidenceError

        for callback in ("copy_file", "start_process"):
            with self.subTest(callback=callback), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home = root / "authoritative-home"
                (home / ".local" / "share" / "usagehub").mkdir(parents=True)
                source = root / "UsageHub-0.8.6-linux-x86_64.AppImage"
                source.write_bytes(b"audited callback token source")
                authority = LifecycleStatePaths(platform="linux", home=home)

                with ExitStack() as stack:
                    dependencies, run_directory, profile, package = (
                        _enter_linux_host_dependencies(stack, authority)
                    )
                    _inspect_fresh_runtime_root(
                        self, dependencies, home, profile.runtime_root
                    )
                    if callback == "copy_file":
                        destination = run_directory / source.name
                        dependencies.copy_file(source, destination)
                        self.assertEqual(
                            destination.read_bytes(), b"audited callback token source"
                        )
                    else:
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_unavailable"
                        ) as unavailable:
                            dependencies.start_process()
                        self.assertEqual(
                            str(unavailable.exception), "driver_unavailable"
                        )

                    _assert_install_alias_rejected_without_probe(
                        self, dependencies, package.install_root
                    )

                self.assertFalse(run_directory.exists())
                self.assertEqual(
                    source.read_bytes(), b"audited callback token source"
                )

    @unittest.skipIf(os.name == "nt", "requires native Linux path semantics")
    def test_linux_host_hostile_path_validation_revokes_runtime_install_token(
        self,
    ) -> None:
        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from scripts.native_lifecycle_evidence import LifecycleEvidenceError

        class HostilePath(type(Path())):
            def is_absolute(self) -> bool:
                raise RuntimeError("PRIVATE_PATH_VALIDATION_FAILURE")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            (home / ".local" / "share" / "usagehub").mkdir(parents=True)
            authority = LifecycleStatePaths(platform="linux", home=home)
            with ExitStack() as stack:
                dependencies, run_directory, profile, package = (
                    _enter_linux_host_dependencies(stack, authority)
                )
                _inspect_fresh_runtime_root(
                    self, dependencies, home, profile.runtime_root
                )

                validation_error: Exception | None = None
                try:
                    dependencies.inspect_path(
                        "fresh_install_root",
                        HostilePath(str(package.install_root)),
                    )
                except Exception as error:
                    validation_error = error

                alias_error: Exception | None = None
                alias_result: object | None = None
                with patch.object(
                    Path,
                    "home",
                    side_effect=AssertionError("revoked token must not read authority"),
                ) as home_probe, patch(
                    "scripts.native_lifecycle_evidence.os.open",
                    side_effect=AssertionError("revoked token must not reprobe"),
                ) as open_probe, patch(
                    "scripts.native_lifecycle_evidence.os.stat",
                    side_effect=AssertionError("revoked token must not reprobe"),
                ) as stat_probe:
                    try:
                        alias_result = dependencies.inspect_path(
                            "fresh_install_root", package.install_root
                        )
                    except Exception as error:
                        alias_error = error

                home_probe.assert_not_called()
                open_probe.assert_not_called()
                stat_probe.assert_not_called()
                self.assertIsNone(alias_result)
                self.assertIs(type(alias_error), LifecycleEvidenceError)
                assert alias_error is not None
                self.assertEqual(str(alias_error), "driver_failed")
                self.assertIs(type(validation_error), LifecycleEvidenceError)
                assert validation_error is not None
                self.assertEqual(str(validation_error), "driver_failed")
                self.assertNotIn("PRIVATE", str(validation_error))
                self.assertEqual(list(run_directory.iterdir()), [])

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

    @unittest.skipIf(os.name == "nt", "requires native Linux path semantics")
    def test_linux_host_service_probe_observes_only_the_canonical_active_collector_command(
        self,
    ) -> None:
        import stat
        from dataclasses import replace
        from unittest.mock import patch

        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from openusage_bar.platform_services import (
            LinuxCollectorServiceState,
            systemd_unit,
        )
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativeServiceState,
            native_lifecycle_dependencies_for_host,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            home.mkdir()
            authority = LifecycleStatePaths(platform="linux", home=home)
            state_root = home / ".local" / "state" / "openusage-bar"
            runtime_root = home / ".local" / "share" / "usagehub" / "runtime"
            collector = runtime_root / "openusage-collector"
            unit = home / ".config" / "systemd" / "user" / "openusage-bar.service"
            api_socket = state_root / "openusage.sock"
            command = (
                str(collector),
                "daemon",
                "--interval",
                "300",
                "--api-transport",
                "unix",
                "--api-socket",
                str(api_socket),
            )
            unit_bytes = systemd_unit(
                interval=300,
                api_socket=str(api_socket),
                command=str(collector),
            ).encode("utf-8")
            observation = LinuxCollectorServiceState(
                unit_file_id="unit-dev:unit-ino",
                unit_size_bytes=len(unit_bytes),
                unit_sha256=hashlib.sha256(unit_bytes).hexdigest(),
                unit_id="openusage-bar.service",
                load_state="loaded",
                active_state="active",
                sub_state="running",
                unit_file_state="enabled",
                fragment_path=unit,
                drop_in_paths=(),
                needs_reload=False,
                main_pid=4312,
                process_uid=os.getuid(),
                process_start_time_ticks=987654,
                process_executable=collector,
                process_executable_file_id="collector-dev:collector-ino",
                process_argv_nul=("\0".join(command) + "\0").encode(),
            )
            before = tuple(
                (path.relative_to(root).as_posix(), stat.S_IFMT(path.lstat().st_mode))
                for path in (root, *sorted(root.rglob("*")))
            )

            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ), patch.object(
                LifecycleStatePaths,
                "for_current_user",
                return_value=authority,
            ), patch(
                "scripts.native_lifecycle_evidence.Path.home",
                return_value=home,
            ), patch.dict(
                os.environ,
                {},
                clear=True,
            ), patch(
                "openusage_bar.platform_services.service_is_registered",
                return_value=True,
            ), patch(
                "openusage_bar.platform_services.read_current_user_collector_service_state",
                return_value=observation,
                create=True,
            ) as read_service_state, native_lifecycle_dependencies_for_host() as dependencies:
                dependencies.make_run_directory("linux", "x64")
                profile = dependencies.profile_paths("linux")
                package = dependencies.package_paths("linux", profile)
                self.assertEqual(package.collector, collector)
                self.assertEqual(
                    dependencies.inspect_service("linux"),
                    NativeServiceState(True, True, command),
                )
                read_service_state.return_value = replace(
                    observation,
                    process_uid=os.getuid() + 1,
                )
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_unavailable"
                ) as foreign_process:
                    dependencies.inspect_service("linux")
                self.assertEqual(
                    str(foreign_process.exception), "driver_unavailable"
                )
                self.assertNotIn(str(root), str(foreign_process.exception))
                read_service_state.return_value = replace(
                    observation,
                    drop_in_paths=(root / "PRIVATE-drop-in.conf",),
                )
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_unavailable"
                ) as hostile_drop_in:
                    dependencies.inspect_service("linux")
                self.assertEqual(
                    str(hostile_drop_in.exception), "driver_unavailable"
                )
                self.assertNotIn("PRIVATE", str(hostile_drop_in.exception))

            self.assertEqual(read_service_state.call_count, 3)
            self.assertTrue(
                all(call.args == () and call.kwargs == {} for call in read_service_state.call_args_list)
            )
            after = tuple(
                (path.relative_to(root).as_posix(), stat.S_IFMT(path.lstat().st_mode))
                for path in (root, *sorted(root.rglob("*")))
            )
            self.assertEqual(after, before)

    @unittest.skipIf(os.name == "nt", "requires native Linux path semantics")
    def test_linux_host_service_probe_rejects_custom_xdg_config_home(
        self,
    ) -> None:
        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            native_lifecycle_dependencies_for_host,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            home.mkdir()
            custom_config = root / "authoritative-xdg-config"
            unit = custom_config / "systemd" / "user" / "openusage-bar.service"
            unit.parent.mkdir(parents=True)
            unit.write_bytes(b"PRIVATE_INACTIVE_CUSTOM_UNIT")
            authority = LifecycleStatePaths(platform="linux", home=home)

            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ), patch.object(
                LifecycleStatePaths,
                "for_current_user",
                return_value=authority,
            ), patch.dict(
                os.environ,
                {"XDG_CONFIG_HOME": str(custom_config)},
                clear=True,
            ), native_lifecycle_dependencies_for_host() as dependencies:
                dependencies.make_run_directory("linux", "x64")
                dependencies.profile_paths("linux")
                with patch(
                    "openusage_bar.platform_services.service_is_registered",
                    return_value=False,
                ):
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as unavailable:
                        dependencies.inspect_service("linux")
                self.assertEqual(str(unavailable.exception), "driver_unavailable")
                self.assertNotIn(str(root), str(unavailable.exception))
                self.assertNotIn("PRIVATE", str(unavailable.exception))
                self.assertEqual(
                    unit.read_bytes(), b"PRIVATE_INACTIVE_CUSTOM_UNIT"
                )

    @unittest.skipIf(os.name == "nt", "requires native Linux path semantics")
    def test_linux_host_service_probe_proves_only_authoritative_absence(
        self,
    ) -> None:
        import stat
        from unittest.mock import patch

        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from openusage_bar.platform_services import ServiceCommandError
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativeServiceState,
            native_lifecycle_dependencies_for_host,
        )

        def tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
            paths = (root, *sorted(root.rglob("*")))
            snapshot: list[tuple[object, ...]] = []
            for path in paths:
                metadata = path.lstat()
                relative = "." if path == root else path.relative_to(root).as_posix()
                payload: object
                if stat.S_ISREG(metadata.st_mode):
                    payload = path.read_bytes()
                elif stat.S_ISLNK(metadata.st_mode):
                    payload = os.readlink(path)
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
            marker = root / "PRIVATE_SERVICE_PROBE_MARKER"
            marker.write_bytes(b"service probe is read-only")
            authority = LifecycleStatePaths(platform="linux", home=home)
            before = tree_snapshot(root)

            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ), native_lifecycle_dependencies_for_host() as dependencies, patch.object(
                LifecycleStatePaths,
                "for_current_user",
                return_value=authority,
            ), patch.dict(os.environ, {}, clear=True):
                dependencies.profile_paths("linux")

                with patch(
                    "openusage_bar.platform_services.service_is_registered",
                    return_value=False,
                ) as service_probe:
                    self.assertEqual(
                        dependencies.inspect_service("linux"),
                        NativeServiceState(False, False, None),
                    )
                service_probe.assert_called_once_with(
                    platform="linux", home=authority.home
                )
                self.assertEqual(tree_snapshot(root), before)

                for platform in ("win", True):
                    with self.subTest(platform=platform), patch(
                        "openusage_bar.platform_services.service_is_registered",
                        return_value=False,
                    ) as service_probe:
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_failed"
                        ) as rejected:
                            dependencies.inspect_service(platform)
                        self.assertEqual(str(rejected.exception), "driver_failed")
                        self.assertNotIn(str(root), str(rejected.exception))
                        service_probe.assert_not_called()

                with patch(
                    "openusage_bar.platform_services.service_is_registered",
                    return_value=True,
                ) as service_probe, patch(
                    "openusage_bar.platform_services.read_current_user_collector_service_state"
                ) as read_service_state:
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_failed"
                    ) as missing_package:
                        dependencies.inspect_service("linux")
                    self.assertEqual(
                        str(missing_package.exception), "driver_failed"
                    )
                    service_probe.assert_called_once_with(
                        platform="linux", home=authority.home
                    )
                    read_service_state.assert_not_called()

                for probe_result in (
                    ServiceCommandError(),
                    RuntimeError("PRIVATE_SERVICE_PROBE_FAILURE"),
                ):
                    with self.subTest(probe=type(probe_result)):
                        with patch(
                            "openusage_bar.platform_services.service_is_registered",
                            side_effect=probe_result,
                        ) as service_probe:
                            with self.assertRaisesRegex(
                                LifecycleEvidenceError, "driver_unavailable"
                            ) as unavailable:
                                dependencies.inspect_service("linux")
                            self.assertEqual(
                                str(unavailable.exception), "driver_unavailable"
                            )
                            self.assertNotIn("PRIVATE_", str(unavailable.exception))
                        service_probe.assert_called_once_with(
                            platform="linux", home=authority.home
                        )

                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_failed"
                ) as stale_absence:
                    dependencies.inspect_listener("linux", "local")
                self.assertEqual(str(stale_absence.exception), "driver_failed")
                self.assertEqual(tree_snapshot(root), before)

            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ), native_lifecycle_dependencies_for_host() as dependencies, patch(
                "openusage_bar.platform_services.service_is_registered",
                return_value=False,
            ) as service_probe:
                for platform in ("linux", "win", True):
                    with self.subTest(uninitialized=platform):
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_failed"
                        ) as rejected:
                            dependencies.inspect_service(platform)
                        self.assertEqual(str(rejected.exception), "driver_failed")
                        self.assertNotIn(str(root), str(rejected.exception))
                service_probe.assert_not_called()

            self.assertEqual(tree_snapshot(root), before)

    @unittest.skipIf(os.name == "nt", "requires native Linux path semantics")
    def test_linux_host_local_listener_probe_proves_only_authoritative_absence(
        self,
    ) -> None:
        import socket
        import stat
        from unittest.mock import patch

        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from scripts import native_lifecycle_evidence as lifecycle_evidence
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativeListenerState,
            native_lifecycle_dependencies_for_host,
        )

        def tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
            snapshot: list[tuple[object, ...]] = []
            for path in (root, *sorted(root.rglob("*"))):
                metadata = path.lstat()
                if stat.S_ISREG(metadata.st_mode):
                    payload: object = path.read_bytes()
                elif stat.S_ISLNK(metadata.st_mode):
                    payload = os.readlink(path)
                else:
                    payload = None
                snapshot.append(
                    (
                        "." if path == root else path.relative_to(root).as_posix(),
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_mode,
                        metadata.st_size,
                        metadata.st_mtime_ns,
                        metadata.st_ctime_ns,
                        metadata.st_nlink,
                        payload,
                    )
                )
            return tuple(snapshot)

        with tempfile.TemporaryDirectory(dir="/tmp", prefix="oul-") as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            home.mkdir()
            marker = root / "PRIVATE_LISTENER_PROBE_MARKER"
            marker.write_bytes(b"listener probe is read-only")
            authority = LifecycleStatePaths(platform="linux", home=home)
            socket_path = (
                home
                / ".local"
                / "state"
                / "openusage-bar"
                / "openusage.sock"
            )
            before = tree_snapshot(root)

            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ), native_lifecycle_dependencies_for_host() as dependencies, patch.object(
                LifecycleStatePaths,
                "for_current_user",
                return_value=authority,
            ), patch.dict(os.environ, {}, clear=True), patch(
                "openusage_bar.platform_services.service_is_registered",
                return_value=False,
            ), patch(
                "openusage_bar.lifecycle_state.current_user_runtime_is_active",
                side_effect=AssertionError("runtime connectivity is not absence"),
            ) as runtime_probe:
                dependencies.profile_paths("linux")
                dependencies.inspect_service("linux")

                self.assertEqual(
                    dependencies.inspect_listener("linux", "local"),
                    NativeListenerState(False, False),
                )
                runtime_probe.assert_not_called()
                self.assertEqual(tree_snapshot(root), before)

                for platform, namespace in (
                    ("win", "local"),
                    (True, "local"),
                    ("linux", "private"),
                    ("linux", True),
                ):
                    with self.subTest(platform=platform, namespace=namespace):
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_failed"
                        ) as rejected:
                            dependencies.inspect_listener(platform, namespace)
                        self.assertEqual(str(rejected.exception), "driver_failed")
                        self.assertNotIn(str(root), str(rejected.exception))
                        runtime_probe.assert_not_called()

                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_unavailable"
                ) as unavailable:
                    dependencies.inspect_listener("linux", "gateway")
                self.assertEqual(str(unavailable.exception), "driver_unavailable")
                runtime_probe.assert_not_called()

                socket_path.parent.mkdir(parents=True)
                socket_path.write_bytes(b"not a socket")
                case_before = tree_snapshot(root)
                dependencies.inspect_service("linux")
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_unavailable"
                ) as regular_unavailable:
                    dependencies.inspect_listener("linux", "local")
                self.assertEqual(
                    str(regular_unavailable.exception), "driver_unavailable"
                )
                self.assertEqual(tree_snapshot(root), case_before)
                socket_path.unlink()

                socket_path.symlink_to(marker)
                case_before = tree_snapshot(root)
                dependencies.inspect_service("linux")
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_unavailable"
                ) as symlink_unavailable:
                    dependencies.inspect_listener("linux", "local")
                self.assertEqual(
                    str(symlink_unavailable.exception), "driver_unavailable"
                )
                self.assertEqual(tree_snapshot(root), case_before)
                socket_path.unlink()

                bound_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    bound_socket.bind(str(socket_path))
                    case_before = tree_snapshot(root)
                    dependencies.inspect_service("linux")
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as socket_unavailable:
                        dependencies.inspect_listener("linux", "local")
                    self.assertEqual(
                        str(socket_unavailable.exception), "driver_unavailable"
                    )
                    self.assertEqual(tree_snapshot(root), case_before)
                finally:
                    bound_socket.close()
                    socket_path.unlink(missing_ok=True)

                case_before = tree_snapshot(root)
                dependencies.inspect_service("linux")
                self.assertEqual(
                    dependencies.inspect_listener("linux", "local"),
                    NativeListenerState(False, False),
                )
                self.assertEqual(tree_snapshot(root), case_before)

                real_stat = os.stat

                def failed_socket_stat(path, *args, **kwargs):
                    if (
                        path == "openusage.sock"
                        and kwargs.get("dir_fd") is not None
                        and kwargs.get("follow_symlinks") is False
                    ):
                        raise PermissionError("PRIVATE_SOCKET_AUTHORITY")
                    return real_stat(path, *args, **kwargs)

                case_before = tree_snapshot(root)
                dependencies.inspect_service("linux")
                with patch(
                    "scripts.native_lifecycle_evidence.os.stat",
                    side_effect=failed_socket_stat,
                ):
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as lstat_unavailable:
                        dependencies.inspect_listener("linux", "local")
                    self.assertEqual(
                        str(lstat_unavailable.exception), "driver_unavailable"
                    )
                    self.assertNotIn("PRIVATE_", str(lstat_unavailable.exception))
                self.assertEqual(tree_snapshot(root), case_before)
                runtime_probe.assert_not_called()

                for flag_name in ("O_NOFOLLOW", "O_DIRECTORY"):
                    dependencies.inspect_service("linux")
                    with self.subTest(flag=flag_name), patch.object(
                        lifecycle_evidence.os,
                        flag_name,
                        0,
                    ):
                        case_before = tree_snapshot(root)
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_unavailable"
                        ) as flag_unavailable:
                            dependencies.inspect_listener("linux", "local")
                        self.assertEqual(
                            str(flag_unavailable.exception), "driver_unavailable"
                        )
                        self.assertEqual(tree_snapshot(root), case_before)

                socket_path.parent.rmdir()
                socket_path.parent.parent.rmdir()
                socket_path.parent.parent.parent.rmdir()
                foreign_ancestor = root / "foreign-listener-state"
                foreign_ancestor.mkdir()
                foreign_marker = foreign_ancestor / "PRIVATE_FOREIGN_SOCKET"
                foreign_marker.write_bytes(b"foreign ancestor remains unchanged")
                (home / ".local").symlink_to(
                    foreign_ancestor,
                    target_is_directory=True,
                )
                case_before = tree_snapshot(root)
                dependencies.inspect_service("linux")
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_unavailable"
                ) as ancestor_unavailable:
                    dependencies.inspect_listener("linux", "local")
                self.assertEqual(
                    str(ancestor_unavailable.exception), "driver_unavailable"
                )
                self.assertEqual(tree_snapshot(root), case_before)
                (home / ".local").unlink()
                runtime_probe.assert_not_called()

                public_listener_root = socket_path.parent
                public_listener_root.mkdir(parents=True)
                original_listener_root = public_listener_root.with_name(
                    "openusage-bar-original"
                )
                replacement_seed = root / "replacement-openusage-bar"
                replacement_seed.mkdir()
                replacement_socket = replacement_seed / "openusage.sock"
                replacement_socket.write_bytes(b"replacement socket authority")
                swapped = False
                swapped_facts: dict[str, tuple[int, int]] = {}

                def swap_before_final_socket_stat(path, *args, **kwargs):
                    nonlocal swapped
                    if (
                        not swapped
                        and path == "openusage.sock"
                        and kwargs.get("dir_fd") is not None
                        and kwargs.get("follow_symlinks") is False
                    ):
                        public_listener_root.rename(original_listener_root)
                        replacement_seed.rename(public_listener_root)
                        original_metadata = original_listener_root.lstat()
                        replacement_metadata = public_listener_root.lstat()
                        swapped_facts.update(
                            {
                                "original": (
                                    original_metadata.st_dev,
                                    original_metadata.st_ino,
                                ),
                                "replacement": (
                                    replacement_metadata.st_dev,
                                    replacement_metadata.st_ino,
                                ),
                            }
                        )
                        swapped = True
                    return real_stat(path, *args, **kwargs)

                dependencies.inspect_service("linux")
                with patch(
                    "scripts.native_lifecycle_evidence.os.stat",
                    side_effect=swap_before_final_socket_stat,
                ):
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as swapped_unavailable:
                        dependencies.inspect_listener("linux", "local")
                    self.assertEqual(
                        str(swapped_unavailable.exception), "driver_unavailable"
                    )
                self.assertTrue(swapped)
                original_metadata = original_listener_root.lstat()
                replacement_metadata = public_listener_root.lstat()
                self.assertEqual(
                    (original_metadata.st_dev, original_metadata.st_ino),
                    swapped_facts["original"],
                )
                self.assertEqual(
                    (replacement_metadata.st_dev, replacement_metadata.st_ino),
                    swapped_facts["replacement"],
                )
                self.assertEqual(tuple(original_listener_root.iterdir()), ())
                self.assertEqual(
                    (public_listener_root / "openusage.sock").read_bytes(),
                    b"replacement socket authority",
                )
                runtime_probe.assert_not_called()

            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ), native_lifecycle_dependencies_for_host() as dependencies, patch.object(
                LifecycleStatePaths,
                "for_current_user",
                return_value=authority,
            ), patch.dict(os.environ, {}, clear=True), patch(
                "openusage_bar.lifecycle_state.current_user_runtime_is_active",
                side_effect=AssertionError("runtime connectivity is not absence"),
            ) as runtime_probe:
                dependencies.profile_paths("linux")
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_failed"
                ) as rejected:
                    dependencies.inspect_listener("linux", "local")
                self.assertEqual(str(rejected.exception), "driver_failed")
                self.assertNotIn(str(root), str(rejected.exception))
                runtime_probe.assert_not_called()

            reactivated_home = root / "reactivated-authoritative-home"
            reactivated_home.mkdir()
            reactivated_authority = LifecycleStatePaths(
                platform="linux",
                home=reactivated_home,
            )
            case_before = tree_snapshot(root)
            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ), native_lifecycle_dependencies_for_host() as dependencies, patch.object(
                LifecycleStatePaths,
                "for_current_user",
                return_value=reactivated_authority,
            ), patch.dict(os.environ, {}, clear=True), patch(
                "openusage_bar.platform_services.service_is_registered",
                side_effect=(False, True),
            ) as service_probe, patch(
                "openusage_bar.lifecycle_state.current_user_runtime_is_active",
                side_effect=AssertionError("runtime connectivity is not absence"),
            ) as runtime_probe:
                dependencies.profile_paths("linux")
                dependencies.inspect_service("linux")
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_unavailable"
                ) as reactivated:
                    dependencies.inspect_listener("linux", "local")
                self.assertEqual(str(reactivated.exception), "driver_unavailable")
                self.assertEqual(service_probe.call_count, 2)
                self.assertEqual(
                    service_probe.call_args_list[0],
                    service_probe.call_args_list[1],
                )
                service_probe.assert_called_with(
                    platform="linux",
                    home=reactivated_home,
                )
                runtime_probe.assert_not_called()
            self.assertEqual(tree_snapshot(root), case_before)

            single_use_home = root / "single-use-authoritative-home"
            single_use_home.mkdir()
            single_use_authority = LifecycleStatePaths(
                platform="linux",
                home=single_use_home,
            )
            case_before = tree_snapshot(root)
            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ), native_lifecycle_dependencies_for_host() as dependencies, patch.object(
                LifecycleStatePaths,
                "for_current_user",
                return_value=single_use_authority,
            ), patch.dict(os.environ, {}, clear=True), patch(
                "openusage_bar.platform_services.service_is_registered",
                side_effect=(False, False),
            ) as service_probe, patch(
                "openusage_bar.lifecycle_state.current_user_runtime_is_active",
                side_effect=AssertionError("runtime connectivity is not absence"),
            ) as runtime_probe:
                dependencies.profile_paths("linux")
                dependencies.inspect_service("linux")
                self.assertEqual(
                    dependencies.inspect_listener("linux", "local"),
                    NativeListenerState(False, False),
                )
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_failed"
                ) as consumed:
                    dependencies.inspect_listener("linux", "local")
                self.assertEqual(str(consumed.exception), "driver_failed")
                self.assertEqual(service_probe.call_count, 2)
                runtime_probe.assert_not_called()
            self.assertEqual(tree_snapshot(root), case_before)

            sandwich_home = root / "sandwich-authoritative-home"
            sandwich_home.mkdir()
            sandwich_authority = LifecycleStatePaths(
                platform="linux",
                home=sandwich_home,
            )
            sandwich_socket = (
                sandwich_home
                / ".local"
                / "state"
                / "openusage-bar"
                / "openusage.sock"
            )
            service_calls = 0
            created_socket_facts: tuple[int, int] | None = None

            def create_socket_during_final_service_probe(**kwargs) -> bool:
                nonlocal service_calls, created_socket_facts
                self.assertEqual(
                    kwargs,
                    {"platform": "linux", "home": sandwich_home},
                )
                service_calls += 1
                if service_calls == 2:
                    sandwich_socket.parent.mkdir(parents=True)
                    sandwich_socket.write_bytes(b"created during service recheck")
                    metadata = sandwich_socket.lstat()
                    created_socket_facts = (metadata.st_dev, metadata.st_ino)
                return False

            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ), native_lifecycle_dependencies_for_host() as dependencies, patch.object(
                LifecycleStatePaths,
                "for_current_user",
                return_value=sandwich_authority,
            ), patch.dict(os.environ, {}, clear=True), patch(
                "openusage_bar.platform_services.service_is_registered",
                side_effect=create_socket_during_final_service_probe,
            ) as service_probe, patch(
                "openusage_bar.lifecycle_state.current_user_runtime_is_active",
                side_effect=AssertionError("runtime connectivity is not absence"),
            ) as runtime_probe:
                dependencies.profile_paths("linux")
                dependencies.inspect_service("linux")
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_unavailable"
                ) as sandwich_unavailable:
                    dependencies.inspect_listener("linux", "local")
                self.assertEqual(
                    str(sandwich_unavailable.exception), "driver_unavailable"
                )
                self.assertEqual(service_probe.call_count, 2)
                assert created_socket_facts is not None
                metadata = sandwich_socket.lstat()
                self.assertEqual(
                    (metadata.st_dev, metadata.st_ino),
                    created_socket_facts,
                )
                self.assertEqual(
                    sandwich_socket.read_bytes(),
                    b"created during service recheck",
                )
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_failed"
                ) as consumed:
                    dependencies.inspect_listener("linux", "local")
                self.assertEqual(str(consumed.exception), "driver_failed")
                self.assertEqual(service_probe.call_count, 2)
                runtime_probe.assert_not_called()

    def test_linux_host_gateway_listener_proves_only_current_netns_absence(
        self,
    ) -> None:
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativeListenerState,
        )

        requested = lambda sequence, _family: [_netlink_done(sequence)]
        with _GatewayHarness(self, requested) as harness:
            marker = harness.root / "PRIVATE_GATEWAY_LISTENER_MARKER"
            marker.write_bytes(b"gateway listener probe is read-only")
            before = _file_snapshot(harness.root)

            with self.assertRaisesRegex(
                LifecycleEvidenceError, "driver_unavailable"
            ) as generic_unavailable:
                harness.dependencies.inspect_listener("linux", "gateway")
            self.assertEqual(
                str(generic_unavailable.exception), "driver_unavailable"
            )
            self.assertEqual(harness.created, [])
            self.assertEqual(
                harness.dependencies.inspect_listener(
                    "linux", "gateway_default_endpoint"
                ),
                NativeListenerState(False, False),
            )

            self.assertEqual(
                harness.created[0].requested_families,
                _GATEWAY_FAMILIES,
            )
            self.assertTrue(harness.created[0].closed)
            self.assertEqual(harness.created[0].pending, [])
            harness.connect_probe.assert_not_called()
            self.assertEqual(harness.netns_probe.call_count, 2)
            self.assertEqual(_file_snapshot(harness.root), before)

    def test_linux_host_gateway_listener_parses_multipart_rows_and_rejects_target_port(
        self,
    ) -> None:
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativeListenerState,
        )

        for target_family in (None, 2, 10):
            with self.subTest(target_family=target_family):
                def responses(sequence: int, family: int) -> list[object]:
                    port = (
                        _GATEWAY_PORT
                        if family == target_family
                        else _UNRELATED_PORT
                    )
                    return [
                        _netlink_diagnostic(
                            family,
                            sequence=sequence,
                            port=port,
                        )
                        + _netlink_done(sequence)
                    ]

                with _GatewayHarness(self, responses) as harness:
                    if target_family is None:
                        self.assertEqual(
                            harness.dependencies.inspect_listener(
                                "linux", "gateway_default_endpoint"
                            ),
                            NativeListenerState(False, False),
                        )
                    else:
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_unavailable"
                        ) as unavailable:
                            harness.dependencies.inspect_listener(
                                "linux", "gateway_default_endpoint"
                            )
                        self.assertEqual(
                            str(unavailable.exception), "driver_unavailable"
                        )

                    self.assertEqual(len(harness.created), 1)
                    self.assertTrue(harness.created[0].closed)
                    if target_family is None:
                        self.assertEqual(harness.created[0].pending, [])
                        self.assertEqual(
                            harness.created[0].requested_families,
                            _GATEWAY_FAMILIES,
                        )
                        self.assertEqual(harness.netns_probe.call_count, 2)

    def test_linux_host_gateway_listener_requires_response_headers_to_echo_requester_port_id(
        self,
    ) -> None:
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativeListenerState,
        )

        requester_port_id = 23
        for response_port_id in (requester_port_id, 0, 24):
            with self.subTest(response_port_id=response_port_id):
                def responses(sequence: int, family: int) -> list[object]:
                    return [
                        _netlink_diagnostic(
                            family,
                            sequence=sequence,
                            port_id=response_port_id,
                        )
                        + _netlink_done(
                            sequence,
                            status=0,
                            port_id=response_port_id,
                        )
                    ]

                with _GatewayHarness(
                    self,
                    responses,
                    socket_options={"port_id": requester_port_id},
                ) as harness:
                    if response_port_id == requester_port_id:
                        self.assertEqual(
                            harness.dependencies.inspect_listener(
                                "linux", "gateway_default_endpoint"
                            ),
                            NativeListenerState(False, False),
                        )
                        self.assertEqual(
                            harness.created[0].requested_families,
                            _GATEWAY_FAMILIES,
                        )
                    else:
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_unavailable"
                        ) as unavailable:
                            harness.dependencies.inspect_listener(
                                "linux", "gateway_default_endpoint"
                            )
                        self.assertEqual(
                            str(unavailable.exception), "driver_unavailable"
                        )
                        self.assertNotIn(
                            str(harness.home), str(unavailable.exception)
                        )
                    self.assertEqual(len(harness.created), 1)
                    self.assertTrue(harness.created[0].closed)
                    harness.connect_probe.assert_not_called()

    def test_linux_host_gateway_listener_requires_stable_current_netns_identity(
        self,
    ) -> None:
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativeListenerState,
        )

        namespace_path = "/proc/thread-self/ns/net"
        responses = lambda sequence, _family: [_netlink_done(sequence)]
        for case, facts in (
            ("same", _stable_netns_facts()),
            ("drift", _stable_netns_facts(final_inode=42)),
        ):
            with self.subTest(case=case):
                with _GatewayHarness(
                    self,
                    responses,
                    netns_facts=facts,
                ) as harness:
                    marker = harness.root / "PRIVATE_NETNS_IDENTITY_MARKER"
                    marker.write_bytes(b"netns identity probe is read-only")
                    if case == "same":
                        self.assertEqual(
                            harness.dependencies.inspect_listener(
                                "linux", "gateway_default_endpoint"
                            ),
                            NativeListenerState(False, False),
                        )
                    else:
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_unavailable"
                        ) as unavailable:
                            harness.dependencies.inspect_listener(
                                "linux", "gateway_default_endpoint"
                            )
                        self.assertEqual(
                            str(unavailable.exception), "driver_unavailable"
                        )
                        self.assertNotIn("PRIVATE_", str(unavailable.exception))
                        self.assertNotIn(namespace_path, str(unavailable.exception))

                    self.assertEqual(harness.netns_probe.call_count, 2)
                    for observed in harness.netns_probe.call_args_list:
                        self.assertEqual(observed.args, (namespace_path,))
                        self.assertEqual(observed.kwargs, {})
                    self.assertEqual(len(harness.created), 1)
                    self.assertTrue(harness.created[0].closed)
                    self.assertEqual(harness.created[0].pending, [])
                    harness.connect_probe.assert_not_called()
                    self.assertEqual(
                        marker.read_bytes(),
                        b"netns identity probe is read-only",
                    )

    def test_linux_host_gateway_listener_bounds_the_complete_four_family_dump(
        self,
    ) -> None:
        from scripts.native_lifecycle_evidence import LifecycleEvidenceError

        cases = (
            ("message_cap", (1025, 1024, 1024, 1024), 88),
            ("byte_cap", (18, 18, 18, 18), 60 * 1024),
        )
        for case, rows_per_family, row_length in cases:
            with self.subTest(case=case):
                request_index = 0

                def responses(sequence: int, family: int) -> list[object]:
                    nonlocal request_index
                    row_count = rows_per_family[request_index]
                    request_index += 1
                    diagnostic = _netlink_diagnostic(
                        family,
                        sequence=sequence,
                    )
                    if row_length == 88:
                        row = diagnostic
                    else:
                        body = diagnostic[16:]
                        attribute_length = row_length - 16 - len(body)
                        self.assertEqual(attribute_length % 4, 0)
                        body += (
                            struct.pack("=HH", attribute_length, 4)
                            + b"x" * (attribute_length - 4)
                        )
                        row = (
                            _netlink_header(
                                _NETLINK_DIAG_BY_FAMILY,
                                sequence=sequence,
                                declared_length=row_length,
                            )
                            + body
                        )
                    rows_per_datagram = max(
                        1, (64 * 1024 - 16) // len(row)
                    )
                    pending: list[object] = []
                    remaining = row_count
                    while remaining:
                        chunk = min(remaining, rows_per_datagram)
                        payload = row * chunk
                        self.assertLessEqual(len(payload), 64 * 1024)
                        pending.append(payload)
                        remaining -= chunk
                    pending.append(_netlink_done(sequence))
                    return pending

                with _GatewayHarness(self, responses) as harness:
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as unavailable:
                        harness.dependencies.inspect_listener(
                            "linux", "gateway_default_endpoint"
                        )

                    self.assertEqual(
                        str(unavailable.exception), "driver_unavailable"
                    )
                    self.assertNotIn(
                        str(harness.home), str(unavailable.exception)
                    )
                    self.assertEqual(len(harness.created), 1)
                    self.assertTrue(harness.created[0].closed)

    def test_linux_host_gateway_listener_rejects_ambiguous_transport_metadata(
        self,
    ) -> None:
        from scripts.native_lifecycle_evidence import LifecycleEvidenceError

        responses = lambda sequence, _family: [_netlink_done(sequence)]
        cases = (
            ("nonzero_groups", {"port_id": 23, "groups": 1}),
            ("boolean_port_id", {"port_id": True}),
            ("oversized_port_id", {"port_id": 1 << 32}),
            ("nonzero_recv_flags", {"port_id": 23, "message_flags": 1}),
        )
        for case, socket_options in cases:
            with self.subTest(case=case):
                with _GatewayHarness(
                    self,
                    responses,
                    socket_options=socket_options,
                ) as harness:
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as unavailable:
                        harness.dependencies.inspect_listener(
                            "linux", "gateway_default_endpoint"
                        )

                    self.assertEqual(
                        str(unavailable.exception), "driver_unavailable"
                    )
                    self.assertNotIn(
                        str(harness.home), str(unavailable.exception)
                    )
                    self.assertEqual(len(harness.created), 1)
                    self.assertTrue(harness.created[0].closed)

    def test_linux_host_gateway_listener_requires_exact_recvmsg_value_types(
        self,
    ) -> None:
        from scripts.native_lifecycle_evidence import LifecycleEvidenceError

        responses = lambda sequence, _family: [_netlink_done(sequence)]
        cases = (
            ("bytearray_payload", {"payload_transform": bytearray}),
            ("tuple_ancillary", {"ancillary": ()}),
            ("integer_ancillary", {"ancillary": 0}),
            ("boolean_source_pid", {"source": (False, 0)}),
            ("boolean_source_groups", {"source": (0, False)}),
            ("nonzero_source_pid", {"source": (1, 0)}),
            ("nonzero_source_groups", {"source": (0, 1)}),
        )
        for case, socket_options in cases:
            with self.subTest(case=case):
                with _GatewayHarness(
                    self,
                    responses,
                    socket_options=socket_options,
                ) as harness:
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as unavailable:
                        harness.dependencies.inspect_listener(
                            "linux", "gateway_default_endpoint"
                        )

                    self.assertEqual(
                        str(unavailable.exception), "driver_unavailable"
                    )
                    self.assertNotIn(
                        str(harness.home), str(unavailable.exception)
                    )
                    self.assertEqual(len(harness.created), 1)
                    self.assertTrue(harness.created[0].closed)

    def test_linux_host_gateway_listener_rejects_incomplete_or_hostile_netlink_protocol(
        self,
    ) -> None:
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativeListenerState,
        )

        def kernel_done_responses(
            sequence: int,
            _family: int,
        ) -> list[object]:
            return [_netlink_done(sequence, status=0)]

        with _GatewayHarness(self, kernel_done_responses) as kernel_harness:
            self.assertEqual(
                kernel_harness.dependencies.inspect_listener(
                    "linux", "gateway_default_endpoint"
                ),
                NativeListenerState(False, False),
            )
            self.assertEqual(
                kernel_harness.created[0].requested_families,
                _GATEWAY_FAMILIES,
            )
            self.assertTrue(kernel_harness.created[0].closed)
            self.assertEqual(kernel_harness.created[0].pending, [])

        def responses_for(
            case: str,
            sequence: int,
            family: int,
        ) -> list[object]:
            done = _netlink_done(sequence)
            if case == "dump_interrupted":
                return [_netlink_done(sequence, flags=0x12)]
            if case == "done_status_error":
                return [_netlink_done(sequence, status=-1)]
            if case == "nlmsg_error":
                return [_netlink_header(2, sequence=sequence)]
            if case == "nlmsg_overrun":
                return [_netlink_header(4, sequence=sequence)]
            if case == "missing_done":
                return [
                    _netlink_diagnostic(family, sequence=sequence),
                    TimeoutError(),
                ]
            if case == "sequence_mismatch":
                return [_netlink_done(sequence + 1)]
            if case == "nonzero_pid":
                return [
                    _netlink_header(
                        _NETLINK_DONE,
                        sequence=sequence,
                        port_id=1,
                    )
                ]
            if case == "family_mismatch":
                other_family = 10 if family == 2 else 2
                return [
                    _netlink_diagnostic(other_family, sequence=sequence)
                    + done
                ]
            if case == "state_mismatch":
                return [
                    _netlink_diagnostic(
                        family,
                        sequence=sequence,
                        state=1,
                    )
                    + done
                ]
            if case == "length_below_header":
                return [
                    _netlink_header(
                        _NETLINK_DONE,
                        sequence=sequence,
                        declared_length=15,
                    )
                ]
            if case == "length_beyond_remaining":
                return [
                    _netlink_header(
                        _NETLINK_DONE,
                        sequence=sequence,
                        declared_length=17,
                    )
                ]
            if case == "unaligned_trailing":
                return [
                    _netlink_header(
                        _NETLINK_DIAG_BY_FAMILY,
                        sequence=sequence,
                        declared_length=17,
                    )
                    + b"x"
                ]
            if case == "trailing_after_done":
                return [done + b"x"]
            if case == "recv_timeout":
                return [TimeoutError()]
            if case == "partial_send":
                return [done]
            raise AssertionError(case)

        cases = (
            "dump_interrupted",
            "done_status_error",
            "nlmsg_error",
            "nlmsg_overrun",
            "missing_done",
            "sequence_mismatch",
            "nonzero_pid",
            "family_mismatch",
            "state_mismatch",
            "length_below_header",
            "length_beyond_remaining",
            "unaligned_trailing",
            "trailing_after_done",
            "partial_send",
            "recv_timeout",
        )
        for case in cases:
            with self.subTest(case=case):
                def responses(sequence: int, family: int) -> list[object]:
                    return responses_for(case, sequence, family)

                options = {"send_delta": -1} if case == "partial_send" else {}
                with _GatewayHarness(
                    self,
                    responses,
                    socket_options=options,
                ) as harness:
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as unavailable:
                        harness.dependencies.inspect_listener(
                            "linux", "gateway_default_endpoint"
                        )

                    self.assertEqual(
                        str(unavailable.exception), "driver_unavailable"
                    )
                    self.assertNotIn(
                        str(harness.home), str(unavailable.exception)
                    )
                    self.assertEqual(len(harness.created), 1)
                    self.assertTrue(harness.created[0].closed)
                    harness.connect_probe.assert_not_called()

    def test_linux_host_gateway_listener_has_one_two_second_absolute_deadline(
        self,
    ) -> None:
        from scripts.native_lifecycle_evidence import LifecycleEvidenceError

        class FakeClock:
            value = 100.0

            def monotonic(self) -> float:
                return self.value

        clock = FakeClock()

        def observe_timeout(seconds: float) -> None:
            self.assertIsNot(type(seconds), bool)
            self.assertGreater(seconds, 0.0)
            expected = min(1.0, 102.0 - clock.value)
            self.assertGreater(expected, 0.0)
            self.assertAlmostEqual(seconds, expected, places=9)

        def observe_receive() -> None:
            clock.value += 0.3

        def responses(sequence: int, family: int) -> list[object]:
            return [
                _netlink_diagnostic(family, sequence=sequence),
                _netlink_done(sequence),
            ]

        with _GatewayHarness(
            self,
            responses,
            monotonic=clock.monotonic,
            socket_options={
                "timeout_observer": observe_timeout,
                "receive_observer": observe_receive,
                "require_operation_timeout": True,
            },
        ) as harness:
            with self.assertRaisesRegex(
                LifecycleEvidenceError, "driver_unavailable"
            ) as unavailable:
                harness.dependencies.inspect_listener(
                    "linux", "gateway_default_endpoint"
                )

            self.assertEqual(str(unavailable.exception), "driver_unavailable")
            self.assertNotIn(str(harness.home), str(unavailable.exception))
            self.assertEqual(len(harness.created), 1)
            self.assertTrue(harness.created[0].closed)
            with self.subTest("every send is covered by the absolute deadline"):
                self.assertFalse(harness.created[0].send_without_timeout)
                self.assertGreaterEqual(harness.created[0].successful_sends, 1)
                self.assertTrue(
                    any(timeout < 1.0 for timeout in harness.created[0].timeouts)
                )

    def test_linux_host_gateway_listener_validates_the_clock_through_final_netns_check(
        self,
    ) -> None:
        from scripts.native_lifecycle_evidence import LifecycleEvidenceError

        class FakeClock:
            def __init__(self, *values: object) -> None:
                self.values = iter(values)

            def monotonic(self) -> object:
                return next(self.values)

        responses = lambda sequence, _family: [
            _netlink_done(sequence, status=0)
        ]
        stable_fact = _stable_netns_facts()[0]
        final_clock_value = [100.0]
        netns_calls = 0

        def final_netns_crosses_deadline(_path: str) -> object:
            nonlocal netns_calls
            netns_calls += 1
            if netns_calls == 2:
                final_clock_value[0] = 102.1
            return stable_fact

        def final_clock() -> float:
            return final_clock_value[0]

        with _GatewayHarness(
            self,
            responses,
            monotonic=final_clock,
            netns_facts=final_netns_crosses_deadline,
        ) as harness:
            with self.assertRaisesRegex(
                LifecycleEvidenceError, "driver_unavailable"
            ) as unavailable:
                harness.dependencies.inspect_listener(
                    "linux", "gateway_default_endpoint"
                )
            self.assertEqual(str(unavailable.exception), "driver_unavailable")
            self.assertNotIn(str(harness.home), str(unavailable.exception))
            self.assertEqual(netns_calls, 2)
            self.assertEqual(len(harness.created), 1)
            self.assertTrue(harness.created[0].closed)
            harness.connect_probe.assert_not_called()

        clock_cases = (
            ("initial_bool", (True,), 0),
            ("initial_nan", (float("nan"),), 0),
            ("initial_infinity", (float("inf"),), 0),
            ("subsequent_bool", (100.0, True), 1),
            ("subsequent_nan", (100.0, float("nan")), 1),
            ("subsequent_infinity", (100.0, float("inf")), 1),
            ("clock_regression", (100.0, 99.0), 1),
        )
        for case, readings, expected_socket_count in clock_cases:
            with self.subTest(case=case):
                clock = FakeClock(*readings)
                with _GatewayHarness(
                    self,
                    responses,
                    monotonic=clock.monotonic,
                ) as harness:
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as unavailable:
                        harness.dependencies.inspect_listener(
                            "linux", "gateway_default_endpoint"
                        )
                    self.assertEqual(
                        str(unavailable.exception), "driver_unavailable"
                    )
                    self.assertNotIn(
                        str(harness.home), str(unavailable.exception)
                    )
                    self.assertEqual(
                        len(harness.created), expected_socket_count
                    )
                    self.assertTrue(
                        all(created.closed for created in harness.created)
                    )
                    harness.connect_probe.assert_not_called()

    @unittest.skipUnless(
        sys.platform.startswith("linux")
        and platform.machine().casefold() in {"x86_64", "amd64"},
        "requires a real Linux x64 network namespace",
    )
    def test_linux_x64_host_gateway_listener_observes_the_real_kernel_port(
        self,
    ) -> None:
        import socket

        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativeListenerState,
            native_lifecycle_dependencies_for_host,
        )

        absent = NativeListenerState(False, False)
        with native_lifecycle_dependencies_for_host() as dependencies:
            dependencies.profile_paths("linux")
            self.assertEqual(
                dependencies.inspect_listener(
                    "linux", "gateway_default_endpoint"
                ),
                absent,
            )

            with socket.socket(
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
            ) as listener:
                listener.bind(("127.0.0.1", 17823))
                listener.listen(1)
                self.assertEqual(listener.getsockname(), ("127.0.0.1", 17823))
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_unavailable"
                ) as unavailable:
                    dependencies.inspect_listener(
                        "linux", "gateway_default_endpoint"
                    )
                self.assertEqual(
                    str(unavailable.exception), "driver_unavailable"
                )

            self.assertEqual(
                dependencies.inspect_listener(
                    "linux", "gateway_default_endpoint"
                ),
                absent,
            )

    @unittest.skipIf(os.name == "nt", "requires native Linux path semantics")
    def test_linux_host_ledger_probe_proves_only_authoritative_absence(
        self,
    ) -> None:
        import stat
        from unittest.mock import patch

        from openusage_bar.lifecycle_state import LifecycleStatePaths
        from scripts import native_lifecycle_evidence as lifecycle_evidence
        from scripts.native_lifecycle_evidence import (
            LifecycleEvidenceError,
            NativeLedgerState,
            native_lifecycle_dependencies_for_host,
        )

        def tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
            snapshot: list[tuple[object, ...]] = []
            for path in (root, *sorted(root.rglob("*"))):
                metadata = path.lstat()
                if stat.S_ISREG(metadata.st_mode):
                    payload: object = path.read_bytes()
                elif stat.S_ISLNK(metadata.st_mode):
                    payload = os.readlink(path)
                else:
                    payload = None
                snapshot.append(
                    (
                        "." if path == root else path.relative_to(root).as_posix(),
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_mode,
                        metadata.st_size,
                        metadata.st_mtime_ns,
                        metadata.st_ctime_ns,
                        metadata.st_nlink,
                        payload,
                    )
                )
            return tuple(snapshot)

        missing = NativeLedgerState(False, None, 0, None, False, 0)
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="oul-") as directory:
            root = Path(directory)
            home = root / "authoritative-home"
            home.mkdir()
            marker = root / "PRIVATE_LEDGER_PROBE_MARKER"
            marker.write_bytes(b"ledger probe is read-only")
            authority = LifecycleStatePaths(platform="linux", home=home)
            ledger_path = (
                home
                / ".local"
                / "state"
                / "openusage-bar"
                / "activity.sqlite3"
            )
            ledger_family = tuple(
                ledger_path.parent / name
                for name in (
                    "activity.sqlite3",
                    "activity.sqlite3-wal",
                    "activity.sqlite3-shm",
                    "activity.sqlite3-journal",
                )
            )

            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ), native_lifecycle_dependencies_for_host() as dependencies, patch.object(
                LifecycleStatePaths,
                "for_current_user",
                return_value=authority,
            ), patch.dict(os.environ, {}, clear=True), patch(
                "openusage_bar.platform_services.service_is_registered",
                return_value=False,
            ), patch(
                "openusage_bar.lifecycle_state.current_user_runtime_is_active",
                side_effect=AssertionError("runtime connectivity is not absence"),
            ) as runtime_probe:
                dependencies.profile_paths("linux")

                def refresh_local_absence() -> None:
                    dependencies.inspect_service("linux")
                    dependencies.inspect_listener("linux", "local")

                refresh_local_absence()

                before = tree_snapshot(root)
                self.assertEqual(dependencies.inspect_ledger("linux"), missing)
                self.assertEqual(tree_snapshot(root), before)
                with patch(
                    "scripts.native_lifecycle_evidence.os.open",
                    side_effect=AssertionError("consumed ledger token must gate first"),
                ) as os_probe:
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_failed"
                    ) as consumed:
                        dependencies.inspect_ledger("linux")
                    self.assertEqual(str(consumed.exception), "driver_failed")
                    os_probe.assert_not_called()

                for platform in ("win", True):
                    refresh_local_absence()
                    with self.subTest(platform=platform), patch(
                        "scripts.native_lifecycle_evidence.os.open",
                        side_effect=AssertionError("ledger probe must not start"),
                    ) as os_probe:
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_failed"
                        ) as rejected:
                            dependencies.inspect_ledger(platform)
                        self.assertEqual(str(rejected.exception), "driver_failed")
                        self.assertNotIn(str(root), str(rejected.exception))
                        os_probe.assert_not_called()

                ledger_path.parent.mkdir(parents=True)
                refresh_local_absence()
                case_before = tree_snapshot(root)
                self.assertEqual(dependencies.inspect_ledger("linux"), missing)
                self.assertEqual(tree_snapshot(root), case_before)

                for sidecar in ledger_family[1:]:
                    sidecar.write_bytes(b"residual SQLite sidecar")
                    refresh_local_absence()
                    case_before = tree_snapshot(root)
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as sidecar_unavailable:
                        dependencies.inspect_ledger("linux")
                    self.assertEqual(
                        str(sidecar_unavailable.exception), "driver_unavailable"
                    )
                    self.assertEqual(tree_snapshot(root), case_before)
                    sidecar.unlink()

                ledger_path.write_bytes(b"not authoritative ledger evidence")
                refresh_local_absence()
                case_before = tree_snapshot(root)
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_unavailable"
                ) as regular_unavailable:
                    dependencies.inspect_ledger("linux")
                self.assertEqual(
                    str(regular_unavailable.exception), "driver_unavailable"
                )
                self.assertEqual(tree_snapshot(root), case_before)
                with patch(
                    "scripts.native_lifecycle_evidence.os.open",
                    side_effect=AssertionError(
                        "failed ledger probe must consume its local token"
                    ),
                ) as os_probe:
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_failed"
                    ) as consumed_failure:
                        dependencies.inspect_ledger("linux")
                    self.assertEqual(
                        str(consumed_failure.exception), "driver_failed"
                    )
                    os_probe.assert_not_called()
                ledger_path.unlink()

                ledger_path.symlink_to(marker)
                refresh_local_absence()
                case_before = tree_snapshot(root)
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_unavailable"
                ) as symlink_unavailable:
                    dependencies.inspect_ledger("linux")
                self.assertEqual(
                    str(symlink_unavailable.exception), "driver_unavailable"
                )
                self.assertEqual(tree_snapshot(root), case_before)
                ledger_path.unlink()

                ledger_path.mkdir()
                refresh_local_absence()
                case_before = tree_snapshot(root)
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_unavailable"
                ) as directory_unavailable:
                    dependencies.inspect_ledger("linux")
                self.assertEqual(
                    str(directory_unavailable.exception), "driver_unavailable"
                )
                self.assertEqual(tree_snapshot(root), case_before)
                ledger_path.rmdir()

                real_stat = os.stat

                def failed_ledger_stat(path, *args, **kwargs):
                    if (
                        path == "activity.sqlite3"
                        and kwargs.get("dir_fd") is not None
                        and kwargs.get("follow_symlinks") is False
                    ):
                        raise PermissionError("PRIVATE_LEDGER_AUTHORITY")
                    return real_stat(path, *args, **kwargs)

                case_before = tree_snapshot(root)
                refresh_local_absence()
                with patch(
                    "scripts.native_lifecycle_evidence.os.stat",
                    side_effect=failed_ledger_stat,
                ):
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as stat_unavailable:
                        dependencies.inspect_ledger("linux")
                    self.assertEqual(
                        str(stat_unavailable.exception), "driver_unavailable"
                    )
                    self.assertNotIn("PRIVATE_", str(stat_unavailable.exception))
                self.assertEqual(tree_snapshot(root), case_before)

                for flag_name in ("O_NOFOLLOW", "O_DIRECTORY"):
                    refresh_local_absence()
                    with self.subTest(flag=flag_name), patch.object(
                        lifecycle_evidence.os,
                        flag_name,
                        0,
                    ):
                        case_before = tree_snapshot(root)
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_unavailable"
                        ) as flag_unavailable:
                            dependencies.inspect_ledger("linux")
                        self.assertEqual(
                            str(flag_unavailable.exception), "driver_unavailable"
                        )
                        self.assertEqual(tree_snapshot(root), case_before)

                real_close = os.close
                close_failed = False

                def close_then_fail(descriptor: int) -> None:
                    nonlocal close_failed
                    real_close(descriptor)
                    if not close_failed:
                        close_failed = True
                        raise OSError("PRIVATE_LEDGER_CLOSE")

                refresh_local_absence()
                with patch(
                    "scripts.native_lifecycle_evidence.os.close",
                    side_effect=close_then_fail,
                ):
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as close_unavailable:
                        dependencies.inspect_ledger("linux")
                    self.assertEqual(
                        str(close_unavailable.exception), "driver_unavailable"
                    )
                    self.assertNotIn("PRIVATE_", str(close_unavailable.exception))
                self.assertTrue(close_failed)

                refresh_local_absence()
                ledger_path.parent.rmdir()
                ledger_path.parent.parent.rmdir()
                ledger_path.parent.parent.parent.rmdir()
                foreign_ancestor = root / "foreign-ledger-state"
                foreign_ancestor.mkdir()
                foreign_marker = foreign_ancestor / "activity.sqlite3"
                foreign_marker.write_bytes(b"foreign ledger remains unchanged")
                (home / ".local").symlink_to(
                    foreign_ancestor,
                    target_is_directory=True,
                )
                case_before = tree_snapshot(root)
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_unavailable"
                ) as ancestor_unavailable:
                    dependencies.inspect_ledger("linux")
                self.assertEqual(
                    str(ancestor_unavailable.exception), "driver_unavailable"
                )
                self.assertEqual(tree_snapshot(root), case_before)
                (home / ".local").unlink()

                public_state_root = ledger_path.parent
                public_state_root.mkdir(parents=True)
                original_state_root = public_state_root.with_name(
                    "openusage-bar-original"
                )
                replacement_seed = root / "replacement-ledger-state"
                replacement_seed.mkdir()
                replacement_ledger = replacement_seed / "activity.sqlite3"
                replacement_ledger.write_bytes(b"replacement ledger authority")
                swapped = False
                ledger_stat_calls = 0

                def swap_before_final_ledger_stat(path, *args, **kwargs):
                    nonlocal ledger_stat_calls, swapped
                    if (
                        path == "activity.sqlite3"
                        and kwargs.get("dir_fd") is not None
                        and kwargs.get("follow_symlinks") is False
                    ):
                        ledger_stat_calls += 1
                        if not swapped and ledger_stat_calls == 4:
                            public_state_root.rename(original_state_root)
                            replacement_seed.rename(public_state_root)
                            swapped = True
                    return real_stat(path, *args, **kwargs)

                refresh_local_absence()
                with patch(
                    "scripts.native_lifecycle_evidence.os.stat",
                    side_effect=swap_before_final_ledger_stat,
                ):
                    with self.assertRaisesRegex(
                        LifecycleEvidenceError, "driver_unavailable"
                    ) as swapped_unavailable:
                        dependencies.inspect_ledger("linux")
                    self.assertEqual(
                        str(swapped_unavailable.exception), "driver_unavailable"
                    )
                self.assertTrue(swapped)
                self.assertEqual(tuple(original_state_root.iterdir()), ())
                self.assertEqual(
                    (public_state_root / "activity.sqlite3").read_bytes(),
                    b"replacement ledger authority",
                )
                runtime_probe.assert_not_called()

            for injected_name in ("openusage.sock", "activity.sqlite3-wal"):
                with self.subTest(final_service_injection=injected_name):
                    sandwich_home = root / f"sandwich-{injected_name.replace('.', '-')}"
                    sandwich_state = (
                        sandwich_home / ".local" / "state" / "openusage-bar"
                    )
                    sandwich_state.mkdir(parents=True)
                    sandwich_authority = LifecycleStatePaths(
                        platform="linux",
                        home=sandwich_home,
                    )
                    injected_entry = sandwich_state / injected_name
                    service_calls = 0
                    injected_identity: tuple[int, int] | None = None

                    def inject_during_ledger_service_recheck(
                        *, platform: str, home: Path
                    ) -> bool:
                        nonlocal service_calls, injected_identity
                        self.assertEqual(platform, "linux")
                        self.assertEqual(home, sandwich_home)
                        service_calls += 1
                        if service_calls == 3:
                            injected_entry.write_bytes(
                                b"created during ledger service recheck"
                            )
                            metadata = injected_entry.lstat()
                            injected_identity = (metadata.st_dev, metadata.st_ino)
                        return False

                    with patch(
                        "scripts.native_lifecycle_evidence.sys.platform", "linux"
                    ), patch(
                        "scripts.native_lifecycle_evidence.host_platform_module.machine",
                        return_value="x86_64",
                    ), native_lifecycle_dependencies_for_host() as dependencies, patch.object(
                        LifecycleStatePaths,
                        "for_current_user",
                        return_value=sandwich_authority,
                    ), patch.dict(os.environ, {}, clear=True), patch(
                        "openusage_bar.platform_services.service_is_registered",
                        side_effect=inject_during_ledger_service_recheck,
                    ) as service_probe, patch(
                        "openusage_bar.lifecycle_state.current_user_runtime_is_active",
                        side_effect=AssertionError(
                            "runtime connectivity is not absence"
                        ),
                    ) as runtime_probe:
                        dependencies.profile_paths("linux")
                        dependencies.inspect_service("linux")
                        dependencies.inspect_listener("linux", "local")
                        with self.assertRaisesRegex(
                            LifecycleEvidenceError, "driver_unavailable"
                        ) as changed_during_sandwich:
                            dependencies.inspect_ledger("linux")
                        self.assertEqual(
                            str(changed_during_sandwich.exception),
                            "driver_unavailable",
                        )
                        self.assertEqual(service_probe.call_count, 3)
                        assert injected_identity is not None
                        metadata = injected_entry.lstat()
                        self.assertEqual(
                            (metadata.st_dev, metadata.st_ino), injected_identity
                        )
                        self.assertEqual(
                            injected_entry.read_bytes(),
                            b"created during ledger service recheck",
                        )
                        with patch(
                            "scripts.native_lifecycle_evidence.os.open",
                            side_effect=AssertionError(
                                "consumed ledger token must gate first"
                            ),
                        ) as os_probe:
                            with self.assertRaisesRegex(
                                LifecycleEvidenceError, "driver_failed"
                            ) as consumed:
                                dependencies.inspect_ledger("linux")
                            self.assertEqual(
                                str(consumed.exception), "driver_failed"
                            )
                            os_probe.assert_not_called()
                        self.assertEqual(service_probe.call_count, 3)
                        runtime_probe.assert_not_called()

            with patch(
                "scripts.native_lifecycle_evidence.sys.platform", "linux"
            ), patch(
                "scripts.native_lifecycle_evidence.host_platform_module.machine",
                return_value="x86_64",
            ), native_lifecycle_dependencies_for_host() as dependencies, patch.object(
                LifecycleStatePaths,
                "for_current_user",
                return_value=authority,
            ), patch.dict(os.environ, {}, clear=True), patch(
                "openusage_bar.platform_services.service_is_registered",
                return_value=False,
            ) as service_probe, patch(
                "scripts.native_lifecycle_evidence.os.open",
                side_effect=AssertionError("ledger probe must not start"),
            ) as os_probe:
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_failed"
                ):
                    dependencies.inspect_ledger("linux")
                dependencies.profile_paths("linux")
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_failed"
                ):
                    dependencies.inspect_ledger("linux")
                dependencies.inspect_service("linux")
                with self.assertRaisesRegex(
                    LifecycleEvidenceError, "driver_failed"
                ):
                    dependencies.inspect_ledger("linux")
                os_probe.assert_not_called()
                self.assertEqual(service_probe.call_count, 1)

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
                dependencies.network_events()
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
