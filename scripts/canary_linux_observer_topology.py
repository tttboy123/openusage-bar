"""Ephemeral Linux/x64 Observer topology diagnostic.

This module is intentionally separate from native lifecycle and release evidence.
It assumes an exclusive disposable hosted user session and does not claim UI,
Gateway, ledger, persistence, or resistance to hostile same-UID concurrency.
Each runtime fact also proves only that BoundedHTTPClient.open and
HeadlessKeychain.get process-local counters are zero around one Local API
health transaction; it does not prove all network or credential activity.
"""

from __future__ import annotations

import hashlib
import os
import math
import platform
import pwd
import signal
import stat
import subprocess
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from openusage_bar.bounded_process import run_bounded
from openusage_bar.local_api import (
    LinuxLocalAPIState,
    read_current_user_local_api_state,
)
from openusage_bar.platform_services import (
    LinuxCollectorServiceAbsenceState,
    LinuxCollectorServiceState,
    read_current_user_collector_service_absence_state,
    read_current_user_collector_service_state,
)
from scripts.canary_onefile_local_api import (
    SharedClientBoundaryZeroWindow,
    closed_shared_client_boundary_zero_window_values,
    evaluate_onefile_shared_client_boundary_window,
    read_onefile_shared_client_boundary_snapshot,
)


class LinuxObserverTopologyCanaryError(RuntimeError):
    """A path-free topology diagnostic failure."""

    def __init__(self, stage: str = "unknown") -> None:
        if stage not in {
            "unknown",
            "initial-absence",
            "launch",
            "runtime-before",
            "runtime-before-ui-exited",
            "runtime-before-service",
            "runtime-before-boundary",
            "runtime-before-local",
            "runtime-before-local-authority",
            "runtime-before-local-authority-home",
            "runtime-before-local-authority-local",
            "runtime-before-local-authority-local-open",
            "runtime-before-local-authority-local-type",
            "runtime-before-local-authority-local-identity",
            "runtime-before-local-authority-local-owner",
            "runtime-before-local-authority-local-writable",
            "runtime-before-local-authority-state",
            "runtime-before-local-authority-root",
            "runtime-before-local-socket",
            "runtime-before-local-connect-peer",
            "runtime-before-local-proc",
            "runtime-before-local-http",
            "runtime-before-local-revalidate",
            "runtime-before-local-cleanup",
            "runtime-before-service-after",
            "runtime-before-mapping",
            "stop",
            "runtime-after",
            "preserve",
            "final-absence",
        }:
            stage = "unknown"
        self.stage = stage
        super().__init__("Linux Observer topology canary failed")


@dataclass(frozen=True, repr=False)
class LinuxObserverTopologySummary:
    """Closed result for the two facts proved by this hosted diagnostic."""

    service_survived_ui_stop: bool
    service_absent_after_preserve: bool

    def __post_init__(self) -> None:
        if (
            type(self.service_survived_ui_stop) is not bool
            or self.service_survived_ui_stop is not True
            or type(self.service_absent_after_preserve) is not bool
            or self.service_absent_after_preserve is not True
        ):
            raise ValueError("Linux Observer topology summary invalid")

    def __repr__(self) -> str:
        return "<LinuxObserverTopologySummary closed>"


@dataclass(frozen=True, repr=False)
class LinuxObserverRuntimeFact:
    service_before: LinuxCollectorServiceState
    local_api: LinuxLocalAPIState
    service_after: LinuxCollectorServiceState
    shared_client_boundary: SharedClientBoundaryZeroWindow

    def __post_init__(self) -> None:
        if (
            type(self.service_before) is not LinuxCollectorServiceState
            or type(self.local_api) is not LinuxLocalAPIState
            or type(self.service_after) is not LinuxCollectorServiceState
            or self.service_before != self.service_after
            or type(self.shared_client_boundary)
            is not SharedClientBoundaryZeroWindow
            or closed_shared_client_boundary_zero_window_values(
                self.shared_client_boundary
            )
            is None
        ):
            raise ValueError("Linux Observer runtime fact invalid")

    def __repr__(self) -> str:
        return "<LinuxObserverRuntimeFact closed>"


_APPIMAGE_MODE = 0o700
_READINESS_SECONDS = 30.0
_STOP_SECONDS = 5.0
_PRESERVE_SECONDS = 180.0


def _file_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _closed_environment() -> dict[str, str]:
    current_uid = os.getuid()
    current_home = pwd.getpwuid(current_uid).pw_dir
    expected_runtime = f"/run/user/{current_uid}"
    expected_bus = f"unix:path={expected_runtime}/systemd/private"
    selected = {
        "HOME": os.environ.get("HOME"),
        "XDG_DATA_HOME": os.environ.get("XDG_DATA_HOME"),
        "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR"),
        "DBUS_SESSION_BUS_ADDRESS": os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
        "TMPDIR": os.environ.get("TMPDIR"),
        "DISPLAY": os.environ.get("DISPLAY"),
        "XAUTHORITY": os.environ.get("XAUTHORITY"),
    }
    if (
        selected["HOME"] != current_home
        or type(selected["XDG_DATA_HOME"]) is not str
        or not os.path.isabs(selected["XDG_DATA_HOME"])
        or selected["XDG_RUNTIME_DIR"] != expected_runtime
        or selected["DBUS_SESSION_BUS_ADDRESS"] != expected_bus
        or type(selected["TMPDIR"]) is not str
        or not os.path.isabs(selected["TMPDIR"])
        or type(selected["DISPLAY"]) is not str
        or not selected["DISPLAY"].startswith(":")
        or any(character not in ":.0123456789" for character in selected["DISPLAY"])
        or type(selected["XAUTHORITY"]) is not str
        or not os.path.isabs(selected["XAUTHORITY"])
        or os.environ.get("XDG_CONFIG_HOME") not in {None, ""}
    ):
        raise LinuxObserverTopologyCanaryError
    xauthority = selected["XAUTHORITY"]
    tmp_directory = selected["TMPDIR"]
    try:
        xauthority_metadata = os.lstat(xauthority)
        if (
            os.path.commonpath((tmp_directory, xauthority)) != tmp_directory
            or not stat.S_ISREG(xauthority_metadata.st_mode)
            or xauthority_metadata.st_uid != current_uid
            or xauthority_metadata.st_nlink != 1
            or stat.S_IMODE(xauthority_metadata.st_mode) & 0o077 != 0
        ):
            raise LinuxObserverTopologyCanaryError
    except Exception:
        raise LinuxObserverTopologyCanaryError from None
    return {
        **{key: value for key, value in selected.items() if value is not None},
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONNOUSERSITE": "1",
        "APPIMAGE_EXTRACT_AND_RUN": "1",
    }


class _AppImageProcessLease:
    def __init__(
        self,
        *,
        appimage: str,
        descriptor: int,
        signature: tuple[int, ...],
        process: subprocess.Popen[bytes],
    ) -> None:
        self._appimage = appimage
        self._descriptor = descriptor
        self._signature = signature
        self._process = process
        self._stopped = False
        self._closed = False

    def __enter__(self) -> "_AppImageProcessLease":
        return self

    def _binding_is_stable(self) -> bool:
        try:
            return (
                _file_signature(os.fstat(self._descriptor)) == self._signature
                and _file_signature(os.lstat(self._appimage)) == self._signature
            )
        except Exception:
            return False

    def stop(self) -> None:
        if self._stopped or not self._binding_is_stable():
            raise LinuxObserverTopologyCanaryError
        process = self._process
        if type(process.pid) is not int or process.pid <= 0:
            raise LinuxObserverTopologyCanaryError
        try:
            if os.getpgid(process.pid) != process.pid:
                raise LinuxObserverTopologyCanaryError
            os.killpg(process.pid, signal.SIGTERM)
            if not _wait_for_reserved_leader(process.pid, _STOP_SECONDS):
                os.killpg(process.pid, signal.SIGKILL)
                if not _wait_for_reserved_leader(process.pid, _STOP_SECONDS):
                    raise LinuxObserverTopologyCanaryError
            if _process_group_has_live_member(process.pid):
                os.killpg(process.pid, signal.SIGKILL)
                _wait_for_group_members_to_exit(process.pid)
            result = process.wait(timeout=_STOP_SECONDS)
            if type(result) is not int:
                raise LinuxObserverTopologyCanaryError
            if not self._binding_is_stable():
                raise LinuxObserverTopologyCanaryError
            self._stopped = True
        except LinuxObserverTopologyCanaryError:
            raise
        except Exception:
            raise LinuxObserverTopologyCanaryError from None

    def leader_has_exited(self) -> bool:
        try:
            status = os.waitid(
                os.P_PID,
                self._process.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except Exception:
            raise LinuxObserverTopologyCanaryError from None
        return status is not None and status.si_pid == self._process.pid

    def close(self) -> None:
        if self._closed:
            return
        failed = False
        if not self._stopped:
            try:
                os.killpg(self._process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                failed = True
            try:
                _wait_for_reserved_leader(self._process.pid, _STOP_SECONDS)
                _wait_for_group_members_to_exit(self._process.pid)
                result = self._process.wait(timeout=_STOP_SECONDS)
                if type(result) is not int:
                    failed = True
            except Exception:
                failed = True
            try:
                if _process_group_has_live_member(self._process.pid):
                    failed = True
            except Exception:
                failed = True
        try:
            os.close(self._descriptor)
        except Exception:
            failed = True
        self._closed = True
        if failed:
            raise LinuxObserverTopologyCanaryError

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def _read_absence() -> LinuxCollectorServiceAbsenceState:
    observed = read_current_user_collector_service_absence_state()
    if type(observed) is not LinuxCollectorServiceAbsenceState:
        raise LinuxObserverTopologyCanaryError
    return observed


def _observe_runtime(*, remaining_timeout) -> LinuxObserverRuntimeFact:
    try:
        service_before = read_current_user_collector_service_state()
    except Exception:
        raise LinuxObserverTopologyCanaryError("runtime-before-service") from None
    current_uid = os.getuid()
    current_gid = os.getgid()
    current_home = pwd.getpwuid(current_uid).pw_dir
    expected_socket = os.path.join(
        current_home,
        ".local",
        "state",
        "openusage-bar",
        "openusage.sock",
    )
    try:
        boundary_peer_before, boundary_before = (
            read_onefile_shared_client_boundary_snapshot(
                expected_socket,
                remaining_timeout=remaining_timeout,
            )
        )
    except Exception:
        raise LinuxObserverTopologyCanaryError("runtime-before-boundary") from None
    try:
        local_state = read_current_user_local_api_state()
    except Exception as error:
        stage = getattr(error, "stage", "unknown")
        if stage in {
            "authority",
            "authority-home",
            "authority-local",
            "authority-local-open",
            "authority-local-type",
            "authority-local-identity",
            "authority-local-owner",
            "authority-local-writable",
            "authority-state",
            "authority-root",
            "socket",
            "connect-peer",
            "proc",
            "http",
            "revalidate",
            "cleanup",
        }:
            raise LinuxObserverTopologyCanaryError(
                f"runtime-before-local-{stage}"
            ) from None
        raise LinuxObserverTopologyCanaryError("runtime-before-local") from None
    try:
        boundary_peer_after, boundary_after = (
            read_onefile_shared_client_boundary_snapshot(
                expected_socket,
                remaining_timeout=remaining_timeout,
            )
        )
    except Exception:
        raise LinuxObserverTopologyCanaryError("runtime-before-boundary") from None
    try:
        service_after = read_current_user_collector_service_state()
    except Exception:
        raise LinuxObserverTopologyCanaryError(
            "runtime-before-service-after"
        ) from None
    try:
        remaining_timeout()
    except Exception:
        raise LinuxObserverTopologyCanaryError(
            "runtime-before-service-after"
        ) from None
    if (
        type(service_before) is not LinuxCollectorServiceState
        or type(local_state) is not LinuxLocalAPIState
        or type(service_after) is not LinuxCollectorServiceState
        or service_after != service_before
    ):
        raise LinuxObserverTopologyCanaryError("runtime-before-mapping")

    data_home = os.environ.get("XDG_DATA_HOME")
    if type(data_home) is not str or not os.path.isabs(data_home):
        raise LinuxObserverTopologyCanaryError("runtime-before-mapping")
    expected_collector = os.path.join(
        data_home,
        "usagehub",
        "runtime",
        "openusage-collector",
    )
    expected_fragment = os.path.join(
        current_home,
        ".config",
        "systemd",
        "user",
        "openusage-bar.service",
    )
    expected_argv = (
        expected_collector,
        "daemon",
        "--interval",
        "1800",
        "--api-transport",
        "unix",
        "--api-socket",
        expected_socket,
    )
    expected_argv_nul = ("\0".join(expected_argv) + "\0").encode("utf-8")
    from openusage_bar.platform_services import systemd_unit

    expected_unit = systemd_unit(
        interval=1800,
        api_socket=expected_socket,
        command=expected_collector,
    ).encode("utf-8")
    expected_path_sha256 = hashlib.sha256(
        os.fsencode(service_before.process_executable)
    ).hexdigest()
    expected_argv_sha256 = hashlib.sha256(
        service_before.process_argv_nul
    ).hexdigest()
    expected_cgroup = (
        "0::/user.slice/"
        f"user-{current_uid}.slice/user@{current_uid}.service/"
        "app.slice/openusage-bar.service\n"
    ).encode("ascii")
    expected_cgroup_sha256 = hashlib.sha256(expected_cgroup).hexdigest()
    peer_is_main = (
        local_state.peer_pid == service_before.main_pid
        and local_state.peer_start_time_ticks
        == service_before.process_start_time_ticks
    )
    peer_is_direct_child = (
        local_state.peer_pid != service_before.main_pid
        and local_state.peer_parent_pid == service_before.main_pid
        and local_state.peer_start_time_ticks
        > service_before.process_start_time_ticks
    )
    if (
        service_before.unit_size_bytes != len(expected_unit)
        or service_before.unit_sha256
        != hashlib.sha256(expected_unit).hexdigest()
        or service_before.unit_id != "openusage-bar.service"
        or service_before.load_state != "loaded"
        or service_before.active_state != "active"
        or service_before.sub_state != "running"
        or service_before.unit_file_state != "enabled"
        or service_before.fragment_path != Path(expected_fragment)
        or service_before.drop_in_paths != ()
        or service_before.needs_reload is not False
        or service_before.process_uid != current_uid
        or service_before.process_executable != Path(expected_collector)
        or service_before.process_argv_nul != expected_argv_nul
        or local_state.socket_mode != 0o600
        or local_state.socket_uid != current_uid
        or local_state.peer_uid != current_uid
        or local_state.peer_uid != service_before.process_uid
        or local_state.peer_gid != current_gid
        or not (peer_is_main or peer_is_direct_child)
        or local_state.peer_executable_file_id
        != service_before.process_executable_file_id
        or local_state.peer_executable_signature_sha256
        != service_before.process_executable_signature_sha256
        or local_state.peer_executable_path_sha256 != expected_path_sha256
        or local_state.peer_argv_sha256 != expected_argv_sha256
        or local_state.peer_cgroup_sha256 != expected_cgroup_sha256
        or local_state.http_status != 200
        or local_state.schema_version != "1.0"
        or local_state.health_ok is not True
        or local_state.health_status != "ok"
    ):
        raise LinuxObserverTopologyCanaryError
    try:
        health_peer = struct.pack(
            "=3i",
            local_state.peer_pid,
            local_state.peer_uid,
            local_state.peer_gid,
        )
        boundary_window = evaluate_onefile_shared_client_boundary_window(
            health_peer=health_peer,
            peer_before=boundary_peer_before,
            counters_before=boundary_before,
            peer_after=boundary_peer_after,
            counters_after=boundary_after,
        )
        boundary_values = closed_shared_client_boundary_zero_window_values(
            boundary_window
        )
        if boundary_values is None:
            raise LinuxObserverTopologyCanaryError("runtime-before-boundary")
        boundary_window = SharedClientBoundaryZeroWindow(*boundary_values)
    except Exception:
        raise LinuxObserverTopologyCanaryError("runtime-before-boundary") from None
    try:
        remaining_timeout()
    except Exception:
        raise LinuxObserverTopologyCanaryError("runtime-before-boundary") from None
    return LinuxObserverRuntimeFact(
        service_before,
        local_state,
        service_after,
        boundary_window,
    )


def _start_appimage_lease(appimage: str) -> _AppImageProcessLease:
    descriptor: int | None = None
    process: subprocess.Popen[bytes] | None = None
    lease: _AppImageProcessLease | None = None
    try:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        descriptor = os.open(appimage, flags)
        metadata = os.fstat(descriptor)
        signature = _file_signature(metadata)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or stat.S_IMODE(metadata.st_mode) != _APPIMAGE_MODE
            or _file_signature(os.lstat(appimage)) != signature
        ):
            raise LinuxObserverTopologyCanaryError
        alias = f"/proc/self/fd/{descriptor}"
        process = subprocess.Popen(
            (alias,),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=_closed_environment(),
            pass_fds=(descriptor,),
        )
        lease = _AppImageProcessLease(
            appimage=appimage,
            descriptor=descriptor,
            signature=signature,
            process=process,
        )
        if (
            type(process.pid) is not int
            or process.pid <= 0
            or os.getpgid(process.pid) != process.pid
        ):
            raise LinuxObserverTopologyCanaryError
        return lease
    except Exception:
        if lease is not None:
            try:
                lease.close()
            except Exception:
                pass
        elif descriptor is not None:
            try:
                os.close(descriptor)
            except Exception:
                pass
        raise LinuxObserverTopologyCanaryError from None


def _wait_for_reserved_leader(process_id: int, timeout: float) -> bool:
    started = time.monotonic()
    if (
        isinstance(started, bool)
        or not isinstance(started, (int, float))
        or not math.isfinite(started)
    ):
        raise LinuxObserverTopologyCanaryError
    deadline = float(started) + timeout
    last = float(started)
    while True:
        try:
            status = os.waitid(
                os.P_PID,
                process_id,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except Exception:
            raise LinuxObserverTopologyCanaryError from None
        if status is not None and status.si_pid == process_id:
            return True
        current = time.monotonic()
        if (
            isinstance(current, bool)
            or not isinstance(current, (int, float))
            or not math.isfinite(current)
            or float(current) < last
            or float(current) >= deadline
        ):
            return False
        last = float(current)
        time.sleep(min(0.05, deadline - last))


def _process_group_has_live_member(process_group_id: int) -> bool:
    try:
        entries = os.listdir("/proc")
    except Exception:
        raise LinuxObserverTopologyCanaryError from None
    for entry in entries:
        if not entry.isascii() or not entry.isdecimal():
            continue
        try:
            raw = Path("/proc", entry, "stat").read_bytes()
            if len(raw) > 4096:
                raise LinuxObserverTopologyCanaryError
            closing = raw.rfind(b")")
            fields = raw[closing + 2 :].split()
            if closing <= 0 or len(fields) < 3:
                raise LinuxObserverTopologyCanaryError
            state = fields[0]
            group = int(fields[2])
        except (FileNotFoundError, ProcessLookupError):
            continue
        except Exception:
            raise LinuxObserverTopologyCanaryError from None
        if group == process_group_id and state != b"Z":
            return True
    return False


def _wait_for_group_members_to_exit(process_group_id: int) -> None:
    started = time.monotonic()
    if (
        isinstance(started, bool)
        or not isinstance(started, (int, float))
        or not math.isfinite(started)
    ):
        raise LinuxObserverTopologyCanaryError
    deadline = float(started) + _STOP_SECONDS
    last = float(started)
    while _process_group_has_live_member(process_group_id):
        current = time.monotonic()
        if (
            isinstance(current, bool)
            or not isinstance(current, (int, float))
            or not math.isfinite(current)
            or float(current) < last
            or float(current) >= deadline
        ):
            raise LinuxObserverTopologyCanaryError
        last = float(current)
        time.sleep(min(0.05, deadline - last))


def _run_preserve_uninstall(appimage: str) -> None:
    descriptor: int | None = None
    failed = False
    try:
        descriptor = os.open(
            appimage,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        metadata = os.fstat(descriptor)
        signature = _file_signature(metadata)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or stat.S_IMODE(metadata.st_mode) != _APPIMAGE_MODE
            or _file_signature(os.lstat(appimage)) != signature
        ):
            raise LinuxObserverTopologyCanaryError
        completed = run_bounded(
            (f"/proc/self/fd/{descriptor}", "--usagehub-uninstall"),
            timeout=_PRESERVE_SECONDS,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_closed_environment(),
            pass_fds=(descriptor,),
        )
        if (
            type(completed.returncode) is not int
            or completed.returncode != 0
            or _file_signature(os.fstat(descriptor)) != signature
            or _file_signature(os.lstat(appimage)) != signature
        ):
            raise LinuxObserverTopologyCanaryError
    except Exception:
        failed = True
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except Exception:
                failed = True
    if failed:
        raise LinuxObserverTopologyCanaryError


def _wait_for_runtime(
    lease: _AppImageProcessLease | None = None,
) -> LinuxObserverRuntimeFact:
    started = time.monotonic()
    if (
        isinstance(started, bool)
        or not isinstance(started, (int, float))
        or not math.isfinite(started)
    ):
        raise LinuxObserverTopologyCanaryError
    deadline = float(started) + _READINESS_SECONDS
    last = float(started)
    last_stage = "runtime-before"
    while True:
        try:
            def remaining_timeout() -> float:
                nonlocal last
                current = time.monotonic()
                if (
                    isinstance(current, bool)
                    or not isinstance(current, (int, float))
                    or not math.isfinite(current)
                    or float(current) < last
                    or float(current) >= deadline
                ):
                    raise LinuxObserverTopologyCanaryError(last_stage)
                last = float(current)
                return min(1.0, deadline - last)

            return _observe_runtime(remaining_timeout=remaining_timeout)
        except LinuxObserverTopologyCanaryError as error:
            last_stage = error.stage
            if lease is not None and lease.leader_has_exited():
                raise LinuxObserverTopologyCanaryError(
                    "runtime-before-ui-exited"
                ) from None
            current = time.monotonic()
            if (
                isinstance(current, bool)
                or not isinstance(current, (int, float))
                or not math.isfinite(current)
                or float(current) < last
                or float(current) >= deadline
            ):
                raise LinuxObserverTopologyCanaryError(last_stage) from None
            last = float(current)
            time.sleep(min(0.1, deadline - last))


def run_linux_observer_topology_canary(
    appimage: str,
) -> LinuxObserverTopologySummary:
    """Prove one service survives its owning UI group's bounded stop."""

    try:
        if (
            sys.platform != "linux"
            or platform.machine().casefold() not in {"x86_64", "amd64"}
            or type(appimage) is not str
            or not appimage
            or "\0" in appimage
            or not os.path.isabs(appimage)
        ):
            raise LinuxObserverTopologyCanaryError
        try:
            absence_before = _read_absence()
            absence_before_again = _read_absence()
        except Exception:
            raise LinuxObserverTopologyCanaryError("initial-absence") from None
        if (
            type(absence_before) is not LinuxCollectorServiceAbsenceState
            or type(absence_before_again) is not LinuxCollectorServiceAbsenceState
            or absence_before_again != absence_before
        ):
            raise LinuxObserverTopologyCanaryError("initial-absence")
        try:
            lease_context = _start_appimage_lease(appimage)
        except Exception:
            raise LinuxObserverTopologyCanaryError("launch") from None
        with lease_context as lease:
            try:
                runtime_before = _wait_for_runtime(lease)
            except LinuxObserverTopologyCanaryError:
                raise
            except Exception:
                raise LinuxObserverTopologyCanaryError("runtime-before") from None
            if type(runtime_before) is not LinuxObserverRuntimeFact:
                raise LinuxObserverTopologyCanaryError("runtime-before")
            try:
                lease.stop()
            except Exception:
                raise LinuxObserverTopologyCanaryError("stop") from None
            try:
                runtime_after = _wait_for_runtime()
            except Exception:
                raise LinuxObserverTopologyCanaryError("runtime-after") from None
            if (
                type(runtime_after) is not LinuxObserverRuntimeFact
                or runtime_after != runtime_before
            ):
                raise LinuxObserverTopologyCanaryError("runtime-after")
        try:
            _run_preserve_uninstall(appimage)
        except Exception:
            raise LinuxObserverTopologyCanaryError("preserve") from None
        try:
            absence_after = _read_absence()
            absence_after_again = _read_absence()
        except Exception:
            raise LinuxObserverTopologyCanaryError("final-absence") from None
        if (
            type(absence_after) is not LinuxCollectorServiceAbsenceState
            or type(absence_after_again) is not LinuxCollectorServiceAbsenceState
            or absence_after_again != absence_after
        ):
            raise LinuxObserverTopologyCanaryError("final-absence")
        return LinuxObserverTopologySummary(True, True)
    except LinuxObserverTopologyCanaryError:
        raise
    except Exception:
        raise LinuxObserverTopologyCanaryError from None


def main(arguments: tuple[str, ...] | None = None) -> int:
    """Run silently; only the process status is public."""

    try:
        selected = tuple(sys.argv[1:] if arguments is None else arguments)
    except Exception:
        return 2
    if (
        len(selected) != 2
        or type(selected[0]) is not str
        or selected[0] != "--appimage"
        or type(selected[1]) is not str
        or not os.path.isabs(selected[1])
    ):
        return 2
    try:
        run_linux_observer_topology_canary(selected[1])
    except LinuxObserverTopologyCanaryError as error:
        return {
            "initial-absence": 10,
            "launch": 11,
            "runtime-before": 12,
            "runtime-before-ui-exited": 17,
            "runtime-before-service": 18,
            "runtime-before-local": 19,
            "runtime-before-service-after": 20,
            "runtime-before-mapping": 21,
            "runtime-before-local-authority": 22,
            "runtime-before-local-socket": 23,
            "runtime-before-local-connect-peer": 24,
            "runtime-before-local-proc": 25,
            "runtime-before-local-http": 26,
            "runtime-before-local-revalidate": 27,
            "runtime-before-local-cleanup": 28,
            "runtime-before-local-authority-home": 29,
            "runtime-before-local-authority-local": 30,
            "runtime-before-local-authority-state": 31,
            "runtime-before-local-authority-root": 32,
            "runtime-before-local-authority-local-open": 33,
            "runtime-before-local-authority-local-type": 34,
            "runtime-before-local-authority-local-identity": 35,
            "runtime-before-local-authority-local-owner": 36,
            "runtime-before-local-authority-local-writable": 37,
            "runtime-before-boundary": 38,
            "stop": 13,
            "runtime-after": 14,
            "preserve": 15,
            "final-absence": 16,
        }.get(error.stage, 1)
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
