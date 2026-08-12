#!/usr/bin/env python3
"""Closed evaluation seam for the frozen Linux onefile Local API canary.

This is an instantaneous diagnostic.
Scope: exclusive, disposable GitHub-hosted Linux/x64 runner.
It is not lifecycle or release evidence.
Assumption: no hostile concurrent same-UID namespace/PID-PGID reuse.
Do not reuse its cleanup logic as a runtime deletion primitive.
"""

from __future__ import annotations

import os
import errno
import json
import math
import platform
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Callable

from openusage_bar.shared_client_boundary import (
    SharedClientBoundaryAttemptCounters,
)


_PROC_FACT_LIMIT = 65_536
_CANARY_DEADLINE_SECONDS = 15.0
_HEALTH_RESPONSE_LIMIT = 81_920
_HEALTH_REQUEST = (
    b"GET /v1/health HTTP/1.1\r\n"
    b"Host: localhost\r\n"
    b"Accept: application/json\r\n"
    b"Connection: close\r\n\r\n"
)
_BOUNDARY_REQUEST = (
    b"GET /_internal/v1/shared-client-boundary-attempts HTTP/1.1\r\n"
    b"Host: localhost\r\n"
    b"Accept: application/json\r\n"
    b"Connection: close\r\n\r\n"
)
_HTTP_FIELD_NAME_CHARACTERS = frozenset(
    "!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)


class OnefileLocalAPICanaryError(RuntimeError):
    """A path-free frozen-process topology failure."""

    def __init__(self) -> None:
        super().__init__("onefile Local API canary failed")


@dataclass(frozen=True, repr=False)
class OnefileProcessFacts:
    pid: int
    uid: int
    gid: int
    ppid: int
    start_time_ticks: int
    cgroup: str
    executable_dev: int
    executable_ino: int
    executable_size_bytes: int
    executable_mode: int
    executable_mtime_ns: int
    executable_ctime_ns: int
    executable_nlink: int
    argv_nul: bytes

    def __post_init__(self) -> None:
        integer_values = (
            self.pid,
            self.uid,
            self.gid,
            self.ppid,
            self.start_time_ticks,
            self.executable_dev,
            self.executable_ino,
            self.executable_size_bytes,
            self.executable_mode,
            self.executable_mtime_ns,
            self.executable_ctime_ns,
            self.executable_nlink,
        )
        if (
            any(type(value) is not int for value in integer_values)
            or self.pid <= 0
            or self.uid < 0
            or self.gid < 0
            or self.ppid < 0
            or self.start_time_ticks <= 0
            or self.executable_dev < 0
            or self.executable_ino <= 0
            or self.executable_size_bytes <= 0
            or not stat.S_ISREG(self.executable_mode)
            or self.executable_mode & 0o100 == 0
            or self.executable_mode & 0o022 != 0
            or self.executable_mtime_ns < 0
            or self.executable_ctime_ns < 0
            or self.executable_nlink != 1
            or type(self.cgroup) is not str
            or not self.cgroup
            or len(self.cgroup) > 4_096
            or not self.cgroup.endswith("\n")
            or any(
                not character.isprintable() and character != "\n"
                for character in self.cgroup
            )
            or type(self.argv_nul) is not bytes
            or not self.argv_nul
            or len(self.argv_nul) > 65_536
            or not self.argv_nul.endswith(b"\0")
            or b"\0\0" in self.argv_nul
        ):
            raise ValueError("onefile process facts invalid")


@dataclass(frozen=True)
class OnefileDirectChildTopology:
    stable_direct_child: bool

    def __post_init__(self) -> None:
        if self.stable_direct_child is not True:
            raise ValueError("onefile direct-child topology invalid")


@dataclass(frozen=True, repr=False)
class OnefileLocalAPISummary:
    local_api_health_transaction_succeeded: bool
    stable_direct_child: bool
    shared_client_boundary_attempts_zero: bool

    def __post_init__(self) -> None:
        if (
            self.local_api_health_transaction_succeeded is not True
            or self.stable_direct_child is not True
            or self.shared_client_boundary_attempts_zero is not True
        ):
            raise ValueError("onefile Local API summary invalid")


@dataclass(frozen=True, repr=False)
class SharedClientBoundaryZeroWindow:
    process_epoch_sha256: str
    bounded_http_open_attempts_zero: bool
    headless_keychain_get_attempts_zero: bool

    def __post_init__(self) -> None:
        if (
            type(self.process_epoch_sha256) is not str
            or len(self.process_epoch_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.process_epoch_sha256
            )
            or self.bounded_http_open_attempts_zero is not True
            or self.headless_keychain_get_attempts_zero is not True
        ):
            raise ValueError("shared client boundary zero window invalid")


def closed_shared_client_boundary_zero_window_values(
    value: object,
) -> tuple[str, bool, bool] | None:
    if type(value) is not SharedClientBoundaryZeroWindow:
        return None
    fields = (
        value.process_epoch_sha256,
        value.bounded_http_open_attempts_zero,
        value.headless_keychain_get_attempts_zero,
    )
    if (
        type(fields[0]) is not str
        or len(fields[0]) != 64
        or any(character not in "0123456789abcdef" for character in fields[0])
        or fields[1] is not True
        or fields[2] is not True
    ):
        return None
    return fields


def _closed_shared_boundary_values(
    value: object,
) -> tuple[str, int, int] | None:
    if type(value) is not SharedClientBoundaryAttemptCounters:
        return None
    fields = (
        value.process_epoch_sha256,
        value.bounded_http_open_attempts,
        value.headless_keychain_get_attempts,
    )
    if (
        type(fields[0]) is not str
        or len(fields[0]) != 64
        or any(character not in "0123456789abcdef" for character in fields[0])
        or type(fields[1]) is not int
        or type(fields[2]) is not int
        or not 0 <= fields[1] < 1 << 64
        or not 0 <= fields[2] < 1 << 64
    ):
        return None
    return fields


def _is_current_unix_peer(value: object) -> bool:
    if type(value) is not bytes or len(value) != 12:
        return False
    try:
        peer_pid, peer_uid, peer_gid = struct.unpack("=3i", value)
    except Exception:
        return False
    return (
        peer_pid > 0
        and peer_uid == os.getuid()
        and peer_gid == os.getgid()
    )


def evaluate_onefile_shared_client_boundary_window(
    *,
    health_peer: bytes,
    peer_before: bytes,
    counters_before: object,
    peer_after: bytes,
    counters_after: object,
) -> SharedClientBoundaryZeroWindow:
    before_values = _closed_shared_boundary_values(counters_before)
    after_values = _closed_shared_boundary_values(counters_after)
    if (
        not _is_current_unix_peer(health_peer)
        or not _is_current_unix_peer(peer_before)
        or not _is_current_unix_peer(peer_after)
        or peer_before != health_peer
        or peer_after != health_peer
        or before_values is None
        or after_values is None
        or before_values != after_values
        or before_values[1:] != (0, 0)
    ):
        raise OnefileLocalAPICanaryError
    return SharedClientBoundaryZeroWindow(before_values[0], True, True)


def read_onefile_shared_client_boundary_snapshot(
    socket_path: str,
    *,
    remaining_timeout: Callable[[], float],
) -> tuple[bytes, SharedClientBoundaryAttemptCounters]:
    """Read one strict private snapshot and return its stable Unix peer bytes."""

    client: socket.socket | None = None
    failed = False
    observed: tuple[bytes, SharedClientBoundaryAttemptCounters] | None = None

    def next_timeout() -> float:
        value = remaining_timeout()
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or value <= 0
            or value > 1.0
        ):
            raise OnefileLocalAPICanaryError
        return float(value)

    try:
        if (
            type(socket_path) is not str
            or not socket_path
            or not os.path.isabs(socket_path)
            or "\0" in socket_path
        ):
            raise OnefileLocalAPICanaryError
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(next_timeout())
        client.connect(socket_path)
        peer_before = client.getsockopt(1, 17, 12)
        if type(peer_before) is not bytes or len(peer_before) != 12:
            raise OnefileLocalAPICanaryError
        peer_pid, peer_uid, peer_gid = struct.unpack("=3i", peer_before)
        if (
            peer_pid <= 0
            or peer_uid != os.getuid()
            or peer_gid != os.getgid()
        ):
            raise OnefileLocalAPICanaryError
        client.settimeout(next_timeout())
        client.sendall(_BOUNDARY_REQUEST)
        response = bytearray()
        while True:
            client.settimeout(next_timeout())
            chunk = client.recv(65_536)
            if type(chunk) is not bytes:
                raise OnefileLocalAPICanaryError
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > _HEALTH_RESPONSE_LIMIT:
                raise OnefileLocalAPICanaryError
        next_timeout()
        peer_after = client.getsockopt(1, 17, 12)
        if type(peer_after) is not bytes or peer_after != peer_before:
            raise OnefileLocalAPICanaryError
        next_timeout()
        counters = _parse_shared_client_boundary_response(bytes(response))
        next_timeout()
        observed = (peer_before, counters)
    except Exception:
        failed = True
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                failed = True
            if not failed and observed is not None:
                try:
                    next_timeout()
                except Exception:
                    failed = True
    if failed or observed is None:
        raise OnefileLocalAPICanaryError
    return observed


def _parse_shared_client_boundary_response(
    response: bytes,
) -> SharedClientBoundaryAttemptCounters:
    if type(response) is not bytes or len(response) > _HEALTH_RESPONSE_LIMIT:
        raise OnefileLocalAPICanaryError
    separator = response.find(b"\r\n\r\n")
    if separator < 0 or separator > 16_384:
        raise OnefileLocalAPICanaryError
    try:
        lines = response[:separator].decode("ascii").split("\r\n")
    except Exception:
        raise OnefileLocalAPICanaryError from None
    if not lines or len(lines) > 65 or lines[0] != "HTTP/1.1 200 OK":
        raise OnefileLocalAPICanaryError
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            raise OnefileLocalAPICanaryError
        name, value = line.split(":", 1)
        normalized_name = name.casefold()
        if (
            not name
            or name != name.strip()
            or any(character not in _HTTP_FIELD_NAME_CHARACTERS for character in name)
            or normalized_name in headers
            or any(
                character != "\t" and not (" " <= character <= "~")
                for character in value
            )
        ):
            raise OnefileLocalAPICanaryError
        headers[normalized_name] = value.strip(" \t")
    length = headers.get("content-length")
    if (
        "transfer-encoding" in headers
        or length is None
        or not length.isascii()
        or not length.isdecimal()
        or str(int(length)) != length
        or headers.get("content-type") != "application/json; charset=utf-8"
        or headers.get("cache-control") != "no-store"
        or headers.get("x-content-type-options") != "nosniff"
        or headers.get("connection", "").casefold() != "close"
    ):
        raise OnefileLocalAPICanaryError
    body = response[separator + 4 :]
    if int(length) != len(body) or len(body) > 65_536:
        raise OnefileLocalAPICanaryError

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if type(key) is not str or key in result:
                raise OnefileLocalAPICanaryError
            result[key] = value
        return result

    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=unique_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                OnefileLocalAPICanaryError()
            ),
        )
        if type(payload) is not dict or set(payload) != {
            "apiVersion",
            "object",
            "processEpochSha256",
            "boundedHttpOpenAttempts",
            "headlessKeychainGetAttempts",
        }:
            raise OnefileLocalAPICanaryError
        if (
            type(payload["apiVersion"]) is not str
            or payload["apiVersion"] != "local-api-internal-diagnostics/v1"
            or type(payload["object"]) is not str
            or payload["object"] != "sharedClientBoundaryAttempts"
        ):
            raise OnefileLocalAPICanaryError
        return SharedClientBoundaryAttemptCounters(
            payload["processEpochSha256"],
            payload["boundedHttpOpenAttempts"],
            payload["headlessKeychainGetAttempts"],
        )
    except OnefileLocalAPICanaryError:
        raise
    except Exception:
        raise OnefileLocalAPICanaryError from None


def evaluate_onefile_local_api_peer(
    *,
    parent_pid: int,
    peer_credentials: bytes,
    read_process_facts: Callable[[int], OnefileProcessFacts],
) -> OnefileDirectChildTopology:
    """Require one stable, exact direct child without exposing raw facts."""

    try:
        if (
            type(parent_pid) is not int
            or parent_pid <= 0
            or type(peer_credentials) is not bytes
            or len(peer_credentials) != 12
            or not callable(read_process_facts)
        ):
            raise OnefileLocalAPICanaryError
        peer_pid, peer_uid, peer_gid = struct.unpack("=3i", peer_credentials)
        if peer_pid <= 0 or peer_pid == parent_pid:
            raise OnefileLocalAPICanaryError
        parent_before = read_process_facts(parent_pid)
        child_before = read_process_facts(peer_pid)
        child_after = read_process_facts(peer_pid)
        parent_after = read_process_facts(parent_pid)
        if (
            type(parent_before) is not OnefileProcessFacts
            or type(parent_after) is not OnefileProcessFacts
            or type(child_before) is not OnefileProcessFacts
            or type(child_after) is not OnefileProcessFacts
            or parent_before != parent_after
            or child_before != child_after
            or parent_before.pid != parent_pid
            or child_before.pid != peer_pid
            or peer_uid != child_before.uid
            or peer_gid != child_before.gid
            or parent_before.uid != os.getuid()
            or child_before.uid != os.getuid()
            or parent_before.gid != os.getgid()
            or child_before.gid != os.getgid()
            or child_before.ppid != parent_pid
            or child_before.start_time_ticks
            <= parent_before.start_time_ticks
            or child_before.cgroup != parent_before.cgroup
            or _executable_signature(child_before)
            != _executable_signature(parent_before)
            or child_before.argv_nul != parent_before.argv_nul
        ):
            raise OnefileLocalAPICanaryError
        return OnefileDirectChildTopology(True)
    except OnefileLocalAPICanaryError:
        raise
    except Exception:
        raise OnefileLocalAPICanaryError from None


def read_linux_process_facts(pid: int) -> OnefileProcessFacts:
    """Read one stable, bounded Linux process snapshot through held proc fds."""

    proc_descriptor: int | None = None
    pid_descriptor: int | None = None
    failed = False
    observed: OnefileProcessFacts | None = None
    try:
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
        close_on_exec = getattr(os, "O_CLOEXEC", 0)
        if (
            sys.platform != "linux"
            or type(pid) is not int
            or pid <= 0
            or directory_flag == 0
            or nofollow_flag == 0
            or close_on_exec == 0
        ):
            raise OnefileLocalAPICanaryError
        directory_flags = (
            os.O_RDONLY | directory_flag | nofollow_flag | close_on_exec
        )
        proc_descriptor = os.open("/proc", directory_flags)
        proc_metadata = os.fstat(proc_descriptor)
        if (
            not stat.S_ISDIR(proc_metadata.st_mode)
            or proc_metadata.st_uid != 0
        ):
            raise OnefileLocalAPICanaryError
        pid_descriptor = os.open(
            str(pid),
            directory_flags,
            dir_fd=proc_descriptor,
        )
        pid_metadata = os.fstat(pid_descriptor)
        if (
            not stat.S_ISDIR(pid_metadata.st_mode)
            or pid_metadata.st_uid != os.getuid()
            or pid_metadata.st_gid != os.getgid()
        ):
            raise OnefileLocalAPICanaryError

        identity_before = _read_process_identity(pid_descriptor, pid)
        cgroup_before = _read_proc_text(pid_descriptor, "cgroup")
        executable_before = _read_process_executable(pid_descriptor)
        argv_before = _read_proc_bytes(pid_descriptor, "cmdline")
        identity_middle = _read_process_identity(pid_descriptor, pid)
        cgroup_after = _read_proc_text(pid_descriptor, "cgroup")
        executable_after = _read_process_executable(pid_descriptor)
        argv_after = _read_proc_bytes(pid_descriptor, "cmdline")
        identity_after = _read_process_identity(pid_descriptor, pid)
        if (
            identity_before != identity_middle
            or identity_before != identity_after
            or cgroup_before != cgroup_after
            or executable_before != executable_after
            or argv_before != argv_after
        ):
            raise OnefileLocalAPICanaryError
        uid, gid, parent_pid, start_time_ticks = identity_before
        observed = OnefileProcessFacts(
            pid=pid,
            uid=uid,
            gid=gid,
            ppid=parent_pid,
            start_time_ticks=start_time_ticks,
            cgroup=cgroup_before,
            executable_dev=executable_before[0],
            executable_ino=executable_before[1],
            executable_size_bytes=executable_before[2],
            executable_mode=executable_before[3],
            executable_mtime_ns=executable_before[4],
            executable_ctime_ns=executable_before[5],
            executable_nlink=executable_before[6],
            argv_nul=argv_before,
        )
    except Exception:
        failed = True
    finally:
        for descriptor in (pid_descriptor, proc_descriptor):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except Exception:
                    failed = True
    if failed or observed is None:
        raise OnefileLocalAPICanaryError
    return observed


def run_onefile_local_api_canary(collector: str) -> OnefileLocalAPISummary:
    """Run one isolated frozen collector and return only its topology result."""

    process: subprocess.Popen[bytes] | None = None
    client: socket.socket | None = None
    run_root: str | None = None
    observed: OnefileLocalAPISummary | None = None
    failed = False
    collector_signature: tuple[int, ...] | None = None
    run_root_signature: tuple[int, ...] | None = None
    process_group_clean = True
    try:
        if (
            sys.platform != "linux"
            or platform.machine().casefold() not in {"x86_64", "amd64"}
            or type(collector) is not str
            or not collector
            or "\0" in collector
            or not os.path.isabs(collector)
        ):
            raise OnefileLocalAPICanaryError
        collector_metadata = os.lstat(collector)
        collector_signature = _collector_signature(collector_metadata)
        collector_executable_signature = (
            collector_metadata.st_dev,
            collector_metadata.st_ino,
            collector_metadata.st_size,
            collector_metadata.st_mode,
            collector_metadata.st_mtime_ns,
            collector_metadata.st_ctime_ns,
            collector_metadata.st_nlink,
        )
        if (
            not stat.S_ISREG(collector_metadata.st_mode)
            or collector_metadata.st_uid != os.getuid()
            or collector_metadata.st_nlink != 1
            or collector_metadata.st_size <= 0
            or stat.S_IMODE(collector_metadata.st_mode) & 0o100 == 0
            or stat.S_IMODE(collector_metadata.st_mode) & 0o022 != 0
        ):
            raise OnefileLocalAPICanaryError

        started = time.monotonic()
        if (
            isinstance(started, bool)
            or not isinstance(started, (int, float))
            or not math.isfinite(started)
        ):
            raise OnefileLocalAPICanaryError
        deadline = float(started) + _CANARY_DEADLINE_SECONDS
        last_time = float(started)

        def remaining_timeout() -> float:
            nonlocal last_time
            current = time.monotonic()
            if (
                isinstance(current, bool)
                or not isinstance(current, (int, float))
                or not math.isfinite(current)
                or float(current) < last_time
                or float(current) >= deadline
            ):
                raise OnefileLocalAPICanaryError
            last_time = float(current)
            return min(1.0, deadline - last_time)

        run_root = tempfile.mkdtemp(prefix="oub-onefile-canary-", dir="/tmp")
        run_root_metadata = os.lstat(run_root)
        run_root_signature = _directory_identity(run_root_metadata)
        if (
            not stat.S_ISDIR(run_root_metadata.st_mode)
            or run_root_metadata.st_uid != os.getuid()
            or stat.S_IMODE(run_root_metadata.st_mode) != 0o700
        ):
            raise OnefileLocalAPICanaryError
        isolated_directories = {
            "HOME": os.path.join(run_root, "home"),
            "XDG_DATA_HOME": os.path.join(run_root, "data"),
            "XDG_CONFIG_HOME": os.path.join(run_root, "config"),
            "XDG_CACHE_HOME": os.path.join(run_root, "cache"),
            "TMPDIR": os.path.join(run_root, "tmp"),
        }
        for directory in isolated_directories.values():
            os.mkdir(directory, 0o700)
            os.chown(directory, os.getuid(), os.getgid())
            os.chmod(directory, 0o700)
            metadata = os.lstat(directory)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_gid != os.getgid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise OnefileLocalAPICanaryError
        child_environment = {
            **isolated_directories,
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONNOUSERSITE": "1",
        }
        socket_path = os.path.join(run_root, "openusage.sock")
        process = subprocess.Popen(
            (
                collector,
                "--offline",
                "daemon",
                "--interval",
                "60",
                "--api-transport",
                "unix",
                "--api-socket",
                socket_path,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=child_environment,
        )
        process_group_clean = False
        if type(process.pid) is not int or process.pid <= 0:
            raise OnefileLocalAPICanaryError

        while client is None:
            candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            promoted = False
            try:
                candidate.settimeout(remaining_timeout())
                candidate.connect(socket_path)
                client = candidate
                promoted = True
            except OSError as error:
                if error.errno not in {errno.ENOENT, errno.ECONNREFUSED}:
                    raise OnefileLocalAPICanaryError from None
                if process.poll() is not None:
                    raise OnefileLocalAPICanaryError
                time.sleep(min(0.05, remaining_timeout()))
            finally:
                if not promoted:
                    try:
                        candidate.close()
                    except Exception:
                        raise OnefileLocalAPICanaryError from None

        socket_metadata = os.lstat(socket_path)
        if (
            not stat.S_ISSOCK(socket_metadata.st_mode)
            or socket_metadata.st_uid != os.getuid()
            or socket_metadata.st_nlink != 1
            or stat.S_IMODE(socket_metadata.st_mode) != 0o600
        ):
            raise OnefileLocalAPICanaryError
        peer_before = client.getsockopt(1, 17, 12)
        if type(peer_before) is not bytes or len(peer_before) != 12:
            raise OnefileLocalAPICanaryError
        peer_pid, peer_uid, peer_gid = struct.unpack("=3i", peer_before)
        if (
            peer_pid <= 0
            or peer_uid != os.getuid()
            or peer_gid != os.getgid()
        ):
            raise OnefileLocalAPICanaryError
        parent_before = read_linux_process_facts(process.pid)
        child_before = read_linux_process_facts(peer_pid)
        boundary_peer_before, boundary_before = (
            read_onefile_shared_client_boundary_snapshot(
                socket_path,
                remaining_timeout=remaining_timeout,
            )
        )
        client.settimeout(remaining_timeout())
        client.sendall(_HEALTH_REQUEST)
        response = bytearray()
        while True:
            client.settimeout(remaining_timeout())
            chunk = client.recv(65_536)
            if type(chunk) is not bytes:
                raise OnefileLocalAPICanaryError
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > _HEALTH_RESPONSE_LIMIT:
                raise OnefileLocalAPICanaryError
        peer_after = client.getsockopt(1, 17, 12)
        if type(peer_after) is not bytes or peer_after != peer_before:
            raise OnefileLocalAPICanaryError
        boundary_peer_after, boundary_after = (
            read_onefile_shared_client_boundary_snapshot(
                socket_path,
                remaining_timeout=remaining_timeout,
            )
        )
        boundary_window = evaluate_onefile_shared_client_boundary_window(
            health_peer=peer_before,
            peer_before=boundary_peer_before,
            counters_before=boundary_before,
            peer_after=boundary_peer_after,
            counters_after=boundary_after,
        )
        if type(boundary_window) is not SharedClientBoundaryZeroWindow:
            raise OnefileLocalAPICanaryError
        child_after = read_linux_process_facts(peer_pid)
        parent_after = read_linux_process_facts(process.pid)
        if any(
            _executable_signature(facts) != collector_executable_signature
            for facts in (
                parent_before,
                child_before,
                child_after,
                parent_after,
            )
        ):
            raise OnefileLocalAPICanaryError
        from openusage_bar.local_api import _parse_local_api_health_response

        status_code, health = _parse_local_api_health_response(bytes(response))
        if (
            status_code != 200
            or health.get("schemaVersion") != "1.0"
            or health.get("health") != {"ok": True, "status": "ok"}
        ):
            raise OnefileLocalAPICanaryError
        cached_facts = iter(
            (
                (process.pid, parent_before),
                (peer_pid, child_before),
                (peer_pid, child_after),
                (process.pid, parent_after),
            )
        )

        def read_cached_facts(pid: int) -> OnefileProcessFacts:
            expected_pid, facts = next(cached_facts)
            if type(pid) is not int or pid != expected_pid:
                raise OnefileLocalAPICanaryError
            return facts

        topology = evaluate_onefile_local_api_peer(
            parent_pid=process.pid,
            peer_credentials=peer_before,
            read_process_facts=read_cached_facts,
        )
        if (
            type(topology) is not OnefileDirectChildTopology
            or topology.stable_direct_child is not True
        ):
            raise OnefileLocalAPICanaryError
        try:
            next(cached_facts)
        except StopIteration:
            pass
        else:
            raise OnefileLocalAPICanaryError
        observed = OnefileLocalAPISummary(True, True, True)
        remaining_timeout()
    except Exception:
        failed = True
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                failed = True
        if process is not None:
            leader_observed = False
            leader_reaped = False
            try:
                leader_status = process.poll()
                if leader_status is not None and type(leader_status) is not int:
                    raise OnefileLocalAPICanaryError
                leader_observed = True
            except Exception:
                failed = True
            group_missing = False
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                group_missing = True
            except Exception:
                failed = True
            try:
                wait_status = process.wait(timeout=2.0)
                if type(wait_status) is not int:
                    raise OnefileLocalAPICanaryError
                leader_reaped = True
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                failed = True
            if not group_missing:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    group_missing = True
                except Exception:
                    failed = True
                if not group_missing:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except Exception:
                        failed = True
            if not leader_reaped:
                try:
                    wait_status = process.wait(timeout=2.0)
                    if type(wait_status) is not int:
                        raise OnefileLocalAPICanaryError
                    leader_reaped = True
                except Exception:
                    failed = True
            if not group_missing:
                try:
                    cleanup_started = time.monotonic()
                except Exception:
                    failed = True
                    cleanup_started = None
                if cleanup_started is None or (
                    isinstance(cleanup_started, bool)
                    or not isinstance(cleanup_started, (int, float))
                    or not math.isfinite(cleanup_started)
                ):
                    failed = True
                else:
                    cleanup_deadline = float(cleanup_started) + 2.0
                    cleanup_last = float(cleanup_started)
                    while True:
                        try:
                            os.killpg(process.pid, 0)
                        except ProcessLookupError:
                            group_missing = True
                            break
                        except Exception:
                            failed = True
                            break
                        current = time.monotonic()
                        if (
                            isinstance(current, bool)
                            or not isinstance(current, (int, float))
                            or not math.isfinite(current)
                            or float(current) < cleanup_last
                            or float(current) >= cleanup_deadline
                        ):
                            failed = True
                            break
                        cleanup_last = float(current)
                        try:
                            time.sleep(
                                min(0.05, cleanup_deadline - cleanup_last)
                            )
                        except Exception:
                            failed = True
                            break
            process_group_clean = (
                leader_observed and leader_reaped and group_missing
            )
            if not process_group_clean:
                failed = True
        if (
            process_group_clean
            and run_root is not None
            and run_root_signature is not None
        ):
            cleanup_root = run_root + ".owned-cleanup"
            try:
                if os.path.lexists(cleanup_root):
                    raise OnefileLocalAPICanaryError
                os.rename(run_root, cleanup_root)
                moved_signature = _directory_identity(os.lstat(cleanup_root))
                if moved_signature != run_root_signature:
                    if not os.path.lexists(run_root):
                        os.rename(cleanup_root, run_root)
                    raise OnefileLocalAPICanaryError
                shutil.rmtree(cleanup_root)
                if os.path.lexists(cleanup_root) or os.path.lexists(run_root):
                    raise OnefileLocalAPICanaryError
            except Exception:
                failed = True
        if collector_signature is not None:
            try:
                if _collector_signature(os.lstat(collector)) != collector_signature:
                    failed = True
            except Exception:
                failed = True
    if failed or type(observed) is not OnefileLocalAPISummary:
        raise OnefileLocalAPICanaryError
    return observed


def _collector_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_dev,
        metadata.st_ino,
    )


def _read_process_identity(
    pid_descriptor: int,
    pid: int,
) -> tuple[int, int, int, int]:
    try:
        status = _read_proc_bytes(pid_descriptor, "status").decode("ascii")
        process_stat = (
            _read_proc_bytes(pid_descriptor, "stat").decode("ascii").strip()
        )
    except Exception:
        raise OnefileLocalAPICanaryError from None
    uid = _parse_status_identity(status, "Uid:")
    gid = _parse_status_identity(status, "Gid:")
    closing_parenthesis = process_stat.rfind(")")
    if (
        not process_stat.startswith(f"{pid} (")
        or closing_parenthesis <= len(str(pid)) + 2
        or closing_parenthesis + 2 >= len(process_stat)
        or process_stat[closing_parenthesis + 1] != " "
    ):
        raise OnefileLocalAPICanaryError
    remaining = process_stat[closing_parenthesis + 2 :].split()
    if len(remaining) <= 19:
        raise OnefileLocalAPICanaryError
    parent_text = remaining[1]
    start_time_text = remaining[19]
    if (
        not parent_text.isascii()
        or not parent_text.isdecimal()
        or not start_time_text.isascii()
        or not start_time_text.isdecimal()
    ):
        raise OnefileLocalAPICanaryError
    parent_pid = int(parent_text)
    start_time_ticks = int(start_time_text)
    if (
        parent_pid < 0
        or str(parent_pid) != parent_text
        or start_time_ticks <= 0
        or str(start_time_ticks) != start_time_text
    ):
        raise OnefileLocalAPICanaryError
    return uid, gid, parent_pid, start_time_ticks


def _parse_status_identity(status: str, label: str) -> int:
    lines = [line for line in status.splitlines() if line.startswith(label)]
    if len(lines) != 1:
        raise OnefileLocalAPICanaryError
    fields = lines[0].split()
    if len(fields) != 5 or fields[0] != label:
        raise OnefileLocalAPICanaryError
    values = fields[1:]
    if any(not value.isascii() or not value.isdecimal() for value in values):
        raise OnefileLocalAPICanaryError
    parsed = tuple(int(value) for value in values)
    if len(set(parsed)) != 1 or any(
        str(number) != value
        for number, value in zip(parsed, values, strict=True)
    ):
        raise OnefileLocalAPICanaryError
    return parsed[0]


def _read_process_executable(pid_descriptor: int) -> tuple[int, ...]:
    try:
        metadata = os.stat(
            "exe",
            dir_fd=pid_descriptor,
            follow_symlinks=True,
        )
    except Exception:
        raise OnefileLocalAPICanaryError from None
    signature = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mode,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_gid != os.getgid()
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or stat.S_IMODE(metadata.st_mode) & 0o100 == 0
        or stat.S_IMODE(metadata.st_mode) & 0o022 != 0
    ):
        raise OnefileLocalAPICanaryError
    return signature


def _read_proc_text(pid_descriptor: int, name: str) -> str:
    try:
        value = _read_proc_bytes(pid_descriptor, name).decode("ascii")
    except Exception:
        raise OnefileLocalAPICanaryError from None
    if (
        not value
        or not value.endswith("\n")
        or any(not character.isprintable() and character != "\n" for character in value)
    ):
        raise OnefileLocalAPICanaryError
    return value


def _read_proc_bytes(pid_descriptor: int, name: str) -> bytes:
    if name not in {"status", "stat", "cgroup", "cmdline"}:
        raise OnefileLocalAPICanaryError
    descriptor: int | None = None
    failed = False
    payload: bytes | None = None
    try:
        nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
        close_on_exec = getattr(os, "O_CLOEXEC", 0)
        if nofollow_flag == 0 or close_on_exec == 0:
            raise OnefileLocalAPICanaryError
        descriptor = os.open(
            name,
            os.O_RDONLY | nofollow_flag | close_on_exec,
            dir_fd=pid_descriptor,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_gid != os.getgid()
            or metadata.st_nlink != 1
        ):
            raise OnefileLocalAPICanaryError
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, _PROC_FACT_LIMIT + 1 - size)
            if type(chunk) is not bytes:
                raise OnefileLocalAPICanaryError
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _PROC_FACT_LIMIT:
                raise OnefileLocalAPICanaryError
        payload = b"".join(chunks)
        if not payload:
            raise OnefileLocalAPICanaryError
    except Exception:
        failed = True
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except Exception:
                failed = True
    if failed or payload is None:
        raise OnefileLocalAPICanaryError
    return payload


def main(arguments: tuple[str, ...] | list[str] | None = None) -> int:
    """Run the canary without ever rendering private observation details."""

    try:
        selected = tuple(sys.argv[1:] if arguments is None else arguments)
    except Exception:
        sys.stderr.write("onefile_local_api_canary_failed\n")
        return 2
    if (
        len(selected) != 2
        or selected[0] != "--collector"
        or type(selected[1]) is not str
        or not selected[1]
        or "\0" in selected[1]
        or not os.path.isabs(selected[1])
    ):
        sys.stderr.write("onefile_local_api_canary_failed\n")
        return 2
    try:
        summary = run_onefile_local_api_canary(selected[1])
        if (
            type(summary) is not OnefileLocalAPISummary
            or summary.local_api_health_transaction_succeeded is not True
            or summary.stable_direct_child is not True
            or summary.shared_client_boundary_attempts_zero is not True
        ):
            raise OnefileLocalAPICanaryError
    except Exception:
        sys.stderr.write("onefile_local_api_canary_failed\n")
        return 1
    sys.stdout.write('{"stableDirectChild":true}\n')
    return 0


def _executable_signature(facts: OnefileProcessFacts) -> tuple[int, ...]:
    return (
        facts.executable_dev,
        facts.executable_ino,
        facts.executable_size_bytes,
        facts.executable_mode,
        facts.executable_mtime_ns,
        facts.executable_ctime_ns,
        facts.executable_nlink,
    )


if __name__ == "__main__":
    raise SystemExit(main())
