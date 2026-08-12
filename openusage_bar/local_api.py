"""Version 1 read-only local API for scheduler and native UI consumers.

The API deliberately exposes the same camelCase envelopes as ``QueryService``.
Unix-domain HTTP is the default transport. TCP is an explicit loopback-only
opt-in protected by a high-entropy bearer token. There are no write, refresh,
configuration, credential, remote-bind, CORS-wildcard, or TLS endpoints.
"""

from __future__ import annotations

import errno
import ctypes
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import socket
import socketserver
import stat
import struct
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Collection
from urllib.parse import parse_qsl, urlsplit

if os.name != "nt":
    import grp
    import pwd

from .capabilities import registry as default_registry
from .config import ID_PATTERN
from .provider_catalog import ObserverPlatformResolver
from .provider_catalog import catalog as default_catalog
from .query import MAX_LIMIT, SCHEMA_VERSION, QueryService, to_wire
from .shared_client_boundary import shared_client_boundary_attempt_counters
from .windows_file_security import native_windows_file_security


LOCAL_API_SCHEMA = json.loads(
    (Path(__file__).with_name("resources") / "local-api-v1.schema.json")
    .read_text(encoding="utf-8")
)


MAX_REQUEST_LINE = 8_192
MAX_BODY_BYTES = 0
MAX_QUERY_BYTES = 4_096
MAX_QUERY_FIELDS = 16
MAX_ID_COUNT = 50
MAX_ID_LENGTH = 128
MAX_CURSOR = 2**63 - 1
DEFAULT_MAX_THREADS = 32
DEFAULT_CLIENT_TIMEOUT = 5.0
DEFAULT_REQUEST_DEADLINE = 15.0
DEFAULT_RATE_LIMIT_CAPACITY = 120
DEFAULT_RATE_LIMIT_REFILL_PER_SECOND = 2.0
_MAX_TOKEN_PATH_LENGTH = 4_096
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_FILE_SECURITY = native_windows_file_security()


@dataclass(frozen=True)
class APIProblem(Exception):
    status: HTTPStatus
    code: str
    message: str
    retry_after: int | None = None


class LocalAPIObservationError(RuntimeError):
    """A path-free failure to observe the current-user Local API."""

    def __init__(self, stage: str = "unknown") -> None:
        if stage not in {
            "unknown",
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
            stage = "unknown"
        self.stage = stage
        super().__init__("Local API observation failed")


@dataclass(frozen=True, repr=False)
class LinuxLocalAPIState:
    """Closed Linux Unix-socket and health observation."""

    socket_file_id: str
    socket_mode: int
    socket_uid: int
    peer_pid: int
    peer_uid: int
    peer_gid: int
    peer_parent_pid: int
    peer_start_time_ticks: int
    peer_executable_file_id: str
    peer_executable_signature_sha256: str
    peer_executable_path_sha256: str
    peer_argv_sha256: str
    peer_cgroup_sha256: str
    http_status: int
    schema_version: str
    health_ok: bool
    health_status: str

    def __post_init__(self) -> None:
        if (
            type(self.socket_file_id) is not str
            or not self.socket_file_id
            or type(self.socket_mode) is not int
            or self.socket_mode != 0o600
            or type(self.socket_uid) is not int
            or self.socket_uid < 0
            or type(self.peer_pid) is not int
            or self.peer_pid <= 0
            or type(self.peer_uid) is not int
            or self.peer_uid < 0
            or type(self.peer_gid) is not int
            or self.peer_gid < 0
            or type(self.peer_parent_pid) is not int
            or self.peer_parent_pid <= 0
            or type(self.peer_start_time_ticks) is not int
            or self.peer_start_time_ticks <= 0
            or type(self.peer_executable_file_id) is not str
            or not self.peer_executable_file_id
            or type(self.peer_executable_signature_sha256) is not str
            or len(self.peer_executable_signature_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.peer_executable_signature_sha256
            )
            or type(self.peer_executable_path_sha256) is not str
            or len(self.peer_executable_path_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.peer_executable_path_sha256
            )
            or type(self.peer_argv_sha256) is not str
            or len(self.peer_argv_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.peer_argv_sha256
            )
            or type(self.peer_cgroup_sha256) is not str
            or len(self.peer_cgroup_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.peer_cgroup_sha256
            )
            or type(self.http_status) is not int
            or self.http_status != 200
            or type(self.schema_version) is not str
            or self.schema_version != "1.0"
            or self.health_ok is not True
            or type(self.health_status) is not str
            or self.health_status != "ok"
        ):
            raise ValueError("Linux Local API state invalid")


def _linux_local_api_ancestor_is_private(
    metadata: os.stat_result,
    *,
    current_uid: int,
    current_gid: int,
) -> bool:
    mode = stat.S_IMODE(metadata.st_mode)
    if metadata.st_uid != current_uid or mode & 0o002 != 0:
        return False
    if mode & 0o020 == 0:
        return True
    try:
        account = pwd.getpwuid(current_uid)
        group = grp.getgrgid(current_gid)
    except Exception:
        return False
    return (
        metadata.st_gid == current_gid
        and account.pw_gid == current_gid
        and account.pw_name == group.gr_name
        and list(group.gr_mem) == []
    )


@dataclass(frozen=True, repr=False)
class _LinuxLocalAPIPeerProcessFact:
    uid: int
    gid: int
    parent_pid: int
    start_time_ticks: int
    executable_path: str
    executable_signature: tuple[int, ...]
    argv_nul: bytes
    cgroup: bytes


class _LinuxOpenHow(ctypes.Structure):
    _fields_ = (
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    )


def _stat_linux_canonical_local_socket(home: Path) -> os.stat_result:
    """Bind the full canonical socket path without following any symlink."""

    canonical = home / ".local" / "state" / "openusage-bar" / "openusage.sock"
    try:
        active_kernel = os.uname().sysname
    except Exception:
        raise LocalAPIObservationError from None
    if active_kernel == "Linux":
        open_path = getattr(os, "O_PATH", 0)
        close_on_exec = getattr(os, "O_CLOEXEC", 0)
        if open_path == 0 or close_on_exec == 0:
            raise LocalAPIObservationError
        how = _LinuxOpenHow(
            flags=open_path | os.O_NOFOLLOW | close_on_exec,
            mode=0,
            resolve=0x04,  # RESOLVE_NO_SYMLINKS
        )
        descriptor: int | None = None
        failed = False
        metadata: os.stat_result | None = None
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            syscall = libc.syscall
            syscall.restype = ctypes.c_long
            result = syscall(
                ctypes.c_long(437),  # __NR_openat2 on supported Linux arches
                ctypes.c_int(-100),  # AT_FDCWD
                ctypes.c_char_p(os.fsencode(canonical)),
                ctypes.byref(how),
                ctypes.c_size_t(ctypes.sizeof(how)),
            )
            if result < 0:
                raise LocalAPIObservationError
            descriptor = int(result)
            metadata = os.fstat(descriptor)
        except Exception:
            failed = True
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except Exception:
                    failed = True
        if failed or metadata is None:
            raise LocalAPIObservationError
        return metadata

    # Cross-host tests cannot invoke a foreign syscall.  Preserve the same
    # nofollow semantics by reopening the fixed chain component by component.
    descriptors: list[int] = []
    failed = False
    failure_stage = "authority-home"
    metadata = None
    try:
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
        if directory_flag == 0 or nofollow_flag == 0:
            raise LocalAPIObservationError
        flags = os.O_RDONLY | directory_flag | nofollow_flag
        descriptor = os.open(home, flags)
        descriptors.append(descriptor)
        for name in (".local", "state", "openusage-bar"):
            descriptor = os.open(name, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        metadata = os.stat(
            "openusage.sock",
            dir_fd=descriptor,
            follow_symlinks=False,
        )
    except Exception:
        failed = True
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except Exception:
                failed = True
    if failed or metadata is None:
        raise LocalAPIObservationError
    return metadata


def _read_linux_local_api_proc_file(
    pid_descriptor: int,
    name: str,
) -> bytes:
    if name not in {"status", "stat", "cgroup", "cmdline"}:
        raise LocalAPIObservationError
    descriptor: int | None = None
    failed = False
    payload: bytes | None = None
    try:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        close_on_exec = getattr(os, "O_CLOEXEC", 0)
        if nofollow == 0 or close_on_exec == 0:
            raise LocalAPIObservationError
        descriptor = os.open(
            name,
            os.O_RDONLY | nofollow | close_on_exec,
            dir_fd=pid_descriptor,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_gid != os.getgid()
            or metadata.st_nlink != 1
        ):
            raise LocalAPIObservationError
        collected = bytearray()
        limit = 65_536
        while True:
            block = os.read(
                descriptor,
                min(8192, limit + 1 - len(collected)),
            )
            if type(block) is not bytes:
                raise LocalAPIObservationError
            if not block:
                break
            collected.extend(block)
            if len(collected) > limit:
                raise LocalAPIObservationError
        after = os.fstat(descriptor)
        if (
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_nlink,
        ) != (
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_nlink,
        ):
            raise LocalAPIObservationError
        payload = bytes(collected)
    except Exception:
        failed = True
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except Exception:
                failed = True
    if failed or payload is None or not payload:
        raise LocalAPIObservationError
    return payload


def _parse_linux_local_api_identity(
    *,
    pid: int,
    status_payload: bytes,
    stat_payload: bytes,
) -> tuple[int, int, int, int]:
    try:
        status_text = status_payload.decode("ascii")
        stat_text = stat_payload.decode("ascii").strip()
    except Exception:
        raise LocalAPIObservationError from None

    def exact_identity(label: str) -> int:
        lines = [
            line for line in status_text.splitlines() if line.startswith(label)
        ]
        if len(lines) != 1:
            raise LocalAPIObservationError
        fields = lines[0].split()
        if (
            len(fields) != 5
            or fields[0] != label
            or any(
                not value.isascii() or not value.isdecimal()
                for value in fields[1:]
            )
        ):
            raise LocalAPIObservationError
        values = tuple(int(value) for value in fields[1:])
        if len(set(values)) != 1 or any(
            str(value) != raw
            for value, raw in zip(values, fields[1:], strict=True)
        ):
            raise LocalAPIObservationError
        return values[0]

    uid = exact_identity("Uid:")
    gid = exact_identity("Gid:")
    closing_parenthesis = stat_text.rfind(")")
    if (
        not stat_text.startswith(f"{pid} (")
        or closing_parenthesis <= len(str(pid)) + 2
        or closing_parenthesis + 2 >= len(stat_text)
        or stat_text[closing_parenthesis + 1] != " "
    ):
        raise LocalAPIObservationError
    remaining = stat_text[closing_parenthesis + 2 :].split()
    if len(remaining) <= 19:
        raise LocalAPIObservationError
    parent_text = remaining[1]
    start_text = remaining[19]
    if (
        not parent_text.isascii()
        or not parent_text.isdecimal()
        or not start_text.isascii()
        or not start_text.isdecimal()
    ):
        raise LocalAPIObservationError
    parent_pid = int(parent_text)
    start_time_ticks = int(start_text)
    if (
        parent_pid < 0
        or str(parent_pid) != parent_text
        or start_time_ticks <= 0
        or str(start_time_ticks) != start_text
    ):
        raise LocalAPIObservationError
    return uid, gid, parent_pid, start_time_ticks


def _read_linux_local_api_peer_process(
    *,
    pid_descriptor: int,
    pid: int,
    current_uid: int,
    current_gid: int,
) -> _LinuxLocalAPIPeerProcessFact:
    status_payload = _read_linux_local_api_proc_file(pid_descriptor, "status")
    stat_payload = _read_linux_local_api_proc_file(pid_descriptor, "stat")
    cgroup = _read_linux_local_api_proc_file(pid_descriptor, "cgroup")
    argv_nul = _read_linux_local_api_proc_file(pid_descriptor, "cmdline")
    uid, gid, parent_pid, start_time_ticks = _parse_linux_local_api_identity(
        pid=pid,
        status_payload=status_payload,
        stat_payload=stat_payload,
    )
    try:
        executable_path = os.readlink("exe", dir_fd=pid_descriptor)
        executable = Path(executable_path)
        executable_metadata = os.stat(
            "exe",
            dir_fd=pid_descriptor,
            follow_symlinks=True,
        )
    except Exception:
        raise LocalAPIObservationError from None
    executable_signature = (
        executable_metadata.st_dev,
        executable_metadata.st_ino,
        executable_metadata.st_mode,
        executable_metadata.st_uid,
        executable_metadata.st_gid,
        executable_metadata.st_nlink,
        executable_metadata.st_size,
        executable_metadata.st_mtime_ns,
        executable_metadata.st_ctime_ns,
    )
    if (
        uid != current_uid
        or gid != current_gid
        or type(executable_path) is not str
        or not executable.is_absolute()
        or ".." in executable.parts
        or any(not character.isprintable() for character in executable_path)
        or not stat.S_ISREG(executable_metadata.st_mode)
        or executable_metadata.st_uid != current_uid
        or executable_metadata.st_gid != current_gid
        or executable_metadata.st_nlink != 1
        or executable_metadata.st_size <= 0
        or stat.S_IMODE(executable_metadata.st_mode) & 0o100 == 0
        or stat.S_IMODE(executable_metadata.st_mode) & 0o022 != 0
        or not argv_nul.endswith(b"\0")
        or b"\0\0" in argv_nul
        or not cgroup.endswith(b"\n")
    ):
        raise LocalAPIObservationError
    try:
        cgroup_text = cgroup.decode("ascii")
    except UnicodeError:
        raise LocalAPIObservationError from None
    if any(
        not character.isprintable() and character != "\n"
        for character in cgroup_text
    ):
        raise LocalAPIObservationError
    return _LinuxLocalAPIPeerProcessFact(
        uid=uid,
        gid=gid,
        parent_pid=parent_pid,
        start_time_ticks=start_time_ticks,
        executable_path=executable_path,
        executable_signature=executable_signature,
        argv_nul=argv_nul,
        cgroup=cgroup,
    )


def read_current_user_local_api_state() -> LinuxLocalAPIState:
    """Observe one bounded canonical Linux Local API health transaction."""

    from .lifecycle_state import LifecycleStatePaths

    directory_descriptors: list[int] = []
    client: socket.socket | None = None
    failed = False
    failure_stage = "authority"
    observed: LinuxLocalAPIState | None = None

    def directory_signature(metadata: os.stat_result) -> tuple[int, ...]:
        return (
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_nlink,
        )

    def socket_signature(metadata: os.stat_result) -> tuple[int, ...]:
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

    try:
        if sys.platform != "linux":
            raise LocalAPIObservationError
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
        if directory_flag == 0 or nofollow_flag == 0:
            raise LocalAPIObservationError
        flags = os.O_RDONLY | directory_flag | nofollow_flag
        current_uid = os.getuid()
        current_gid = os.getgid()
        authority = LifecycleStatePaths.for_current_user(platform="linux")
        current_home = Path.home()
        if (
            type(authority) is not LifecycleStatePaths
            or authority.platform != "linux"
            or not isinstance(authority.home, Path)
            or not authority.home.is_absolute()
            or authority.home != current_home
        ):
            raise LocalAPIObservationError

        home_public = os.stat(authority.home, follow_symlinks=False)
        home_descriptor = os.open(authority.home, flags)
        directory_descriptors.append(home_descriptor)
        home_opened = os.fstat(home_descriptor)
        if (
            stat.S_ISLNK(home_public.st_mode)
            or not stat.S_ISDIR(home_public.st_mode)
            or not stat.S_ISDIR(home_opened.st_mode)
            or directory_signature(home_public)
            != directory_signature(home_opened)
            or home_opened.st_uid != current_uid
            or stat.S_IMODE(home_opened.st_mode) & 0o022 != 0
        ):
            raise LocalAPIObservationError

        bindings: list[tuple[int, str, tuple[int, ...]]] = []
        parent_descriptor = home_descriptor
        for name in (".local", "state", "openusage-bar"):
            failure_stage = {
                ".local": "authority-local",
                "state": "authority-state",
                "openusage-bar": "authority-root",
            }[name]
            try:
                child_descriptor = os.open(
                    name,
                    flags,
                    dir_fd=parent_descriptor,
                )
            except Exception:
                if name == ".local":
                    failure_stage = "authority-local-open"
                raise
            directory_descriptors.append(child_descriptor)
            opened = os.fstat(child_descriptor)
            public = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if name == ".local" and (
                stat.S_ISLNK(public.st_mode)
                or not stat.S_ISDIR(public.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
            ):
                failure_stage = "authority-local-type"
                raise LocalAPIObservationError
            if directory_signature(public) != directory_signature(opened):
                if name == ".local":
                    failure_stage = "authority-local-identity"
                raise LocalAPIObservationError
            if opened.st_uid != current_uid:
                if name == ".local":
                    failure_stage = "authority-local-owner"
                raise LocalAPIObservationError
            if not _linux_local_api_ancestor_is_private(
                opened,
                current_uid=current_uid,
                current_gid=current_gid,
            ):
                if name == ".local":
                    failure_stage = "authority-local-writable"
                raise LocalAPIObservationError
            bindings.append(
                (parent_descriptor, name, directory_signature(opened))
            )
            parent_descriptor = child_descriptor

        failure_stage = "authority-root"
        state_root_descriptor = directory_descriptors[-1]
        state_root = os.fstat(state_root_descriptor)
        if (
            state_root.st_uid != current_uid
            or stat.S_IMODE(state_root.st_mode) != 0o700
        ):
            raise LocalAPIObservationError

        failure_stage = "socket"
        socket_before = os.stat(
            "openusage.sock",
            dir_fd=state_root_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISSOCK(socket_before.st_mode)
            or socket_before.st_uid != current_uid
            or stat.S_IMODE(socket_before.st_mode) != 0o600
            or socket_before.st_nlink != 1
        ):
            raise LocalAPIObservationError
        expected_socket_signature = socket_signature(socket_before)

        started = time.monotonic()
        if (
            isinstance(started, bool)
            or not isinstance(started, (int, float))
            or not math.isfinite(started)
        ):
            raise LocalAPIObservationError
        deadline = float(started) + 2.0
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
                raise LocalAPIObservationError
            last_time = float(current)
            return float(min(1.0, deadline - last_time))

        failure_stage = "connect-peer"
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(remaining_timeout())
        client.connect(
            f"/proc/self/fd/{state_root_descriptor}/openusage.sock"
        )
        peer_before_raw = client.getsockopt(1, 17, 12)
        if type(peer_before_raw) is not bytes or len(peer_before_raw) != 12:
            raise LocalAPIObservationError
        peer_before = struct.unpack("=3i", peer_before_raw)
        if (
            peer_before[0] <= 0
            or peer_before[1] != current_uid
            or peer_before[2] != current_gid
        ):
            raise LocalAPIObservationError

        failure_stage = "proc"
        close_on_exec = getattr(os, "O_CLOEXEC", 0)
        if close_on_exec == 0:
            raise LocalAPIObservationError
        proc_descriptor = os.open("/proc", flags | close_on_exec)
        directory_descriptors.append(proc_descriptor)
        proc_metadata = os.fstat(proc_descriptor)
        if (
            not stat.S_ISDIR(proc_metadata.st_mode)
            or proc_metadata.st_uid != 0
        ):
            raise LocalAPIObservationError
        peer_descriptor = os.open(
            str(peer_before[0]),
            flags | close_on_exec,
            dir_fd=proc_descriptor,
        )
        directory_descriptors.append(peer_descriptor)
        peer_directory_metadata = os.fstat(peer_descriptor)
        if (
            not stat.S_ISDIR(peer_directory_metadata.st_mode)
            or peer_directory_metadata.st_uid != current_uid
            or peer_directory_metadata.st_gid != current_gid
        ):
            raise LocalAPIObservationError
        remaining_timeout()
        peer_process_before = _read_linux_local_api_peer_process(
            pid_descriptor=peer_descriptor,
            pid=peer_before[0],
            current_uid=current_uid,
            current_gid=current_gid,
        )
        remaining_timeout()

        failure_stage = "http"
        request = (
            b"GET /v1/health HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Accept: application/json\r\n"
            b"Connection: close\r\n\r\n"
        )
        client.settimeout(remaining_timeout())
        client.sendall(request)
        response = bytearray()
        while True:
            client.settimeout(remaining_timeout())
            chunk = client.recv(65_536)
            if type(chunk) is not bytes:
                raise LocalAPIObservationError
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > 81_920:
                raise LocalAPIObservationError

        peer_after_raw = client.getsockopt(1, 17, 12)
        if (
            type(peer_after_raw) is not bytes
            or len(peer_after_raw) != 12
            or peer_after_raw != peer_before_raw
        ):
            raise LocalAPIObservationError
        remaining_timeout()
        peer_process_after = _read_linux_local_api_peer_process(
            pid_descriptor=peer_descriptor,
            pid=peer_before[0],
            current_uid=current_uid,
            current_gid=current_gid,
        )
        remaining_timeout()
        if peer_process_after != peer_process_before:
            raise LocalAPIObservationError

        status_code, payload = _parse_local_api_health_response(
            bytes(response)
        )
        failure_stage = "revalidate"
        home_after = os.stat(authority.home, follow_symlinks=False)
        if directory_signature(home_after) != directory_signature(home_opened):
            raise LocalAPIObservationError
        for parent_fd, name, expected in bindings:
            public = os.stat(
                name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if directory_signature(public) != expected:
                raise LocalAPIObservationError
        socket_after = _stat_linux_canonical_local_socket(authority.home)
        if socket_signature(socket_after) != expected_socket_signature:
            raise LocalAPIObservationError
        remaining_timeout()
        observed = LinuxLocalAPIState(
            socket_file_id=f"{socket_before.st_dev}:{socket_before.st_ino}",
            socket_mode=stat.S_IMODE(socket_before.st_mode),
            socket_uid=socket_before.st_uid,
            peer_pid=peer_before[0],
            peer_uid=peer_before[1],
            peer_gid=peer_before[2],
            peer_parent_pid=peer_process_before.parent_pid,
            peer_start_time_ticks=peer_process_before.start_time_ticks,
            peer_executable_file_id=(
                f"{peer_process_before.executable_signature[0]}:"
                f"{peer_process_before.executable_signature[1]}"
            ),
            peer_executable_signature_sha256=hashlib.sha256(
                struct.pack(
                    ">9Q", *peer_process_before.executable_signature
                )
            ).hexdigest(),
            peer_executable_path_sha256=hashlib.sha256(
                os.fsencode(peer_process_before.executable_path)
            ).hexdigest(),
            peer_argv_sha256=hashlib.sha256(
                peer_process_before.argv_nul
            ).hexdigest(),
            peer_cgroup_sha256=hashlib.sha256(
                peer_process_before.cgroup
            ).hexdigest(),
            http_status=status_code,
            schema_version=payload["schemaVersion"],
            health_ok=payload["health"]["ok"],
            health_status=payload["health"]["status"],
        )
    except Exception:
        failed = True
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                failed = True
                failure_stage = "cleanup"
        for descriptor in reversed(directory_descriptors):
            try:
                os.close(descriptor)
            except Exception:
                failed = True
                failure_stage = "cleanup"
    if failed or observed is None:
        raise LocalAPIObservationError(failure_stage)
    return observed


def _parse_local_api_health_response(
    response: bytes,
) -> tuple[int, dict[str, Any]]:
    if type(response) is not bytes or len(response) > 81_920:
        raise LocalAPIObservationError
    separator = response.find(b"\r\n\r\n")
    if separator < 0 or separator > 16_384:
        raise LocalAPIObservationError
    try:
        header_lines = response[:separator].decode("ascii").split("\r\n")
    except (UnicodeDecodeError, ValueError):
        raise LocalAPIObservationError from None
    if (
        not header_lines
        or len(header_lines) > 65
        or header_lines[0] != "HTTP/1.1 200 OK"
    ):
        raise LocalAPIObservationError
    headers: dict[str, str] = {}
    for line in header_lines[1:]:
        if ":" not in line:
            raise LocalAPIObservationError
        name, value = line.split(":", 1)
        normalized = name.strip().casefold()
        if not normalized or normalized in headers:
            raise LocalAPIObservationError
        headers[normalized] = value.strip()
    content_length = headers.get("content-length")
    if (
        "transfer-encoding" in headers
        or content_length is None
        or not content_length.isascii()
        or not content_length.isdecimal()
        or headers.get("content-type")
        != "application/json; charset=utf-8"
        or headers.get("connection", "").casefold() != "close"
    ):
        raise LocalAPIObservationError
    body = response[separator + 4 :]
    if int(content_length) != len(body) or len(body) > 65_536:
        raise LocalAPIObservationError

    def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if type(key) is not str or key in result:
                raise LocalAPIObservationError
            result[key] = value
        return result

    def reject_constant(_value: str) -> object:
        raise LocalAPIObservationError

    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_constant,
        )
    except LocalAPIObservationError:
        raise
    except Exception:
        raise LocalAPIObservationError from None
    if type(payload) is not dict or set(payload) != {
        "schemaVersion",
        "dataRevision",
        "generatedAt",
        "sources",
        "health",
    }:
        raise LocalAPIObservationError
    generated_at = payload["generatedAt"]
    try:
        generated_time = datetime.fromisoformat(
            generated_at.replace("Z", "+00:00")
        )
    except Exception:
        raise LocalAPIObservationError from None
    if (
        payload["schemaVersion"] != "1.0"
        or type(payload["dataRevision"]) is not int
        or payload["dataRevision"] < 0
        or type(generated_at) is not str
        or not generated_at
        or len(generated_at) > 40
        or _CONTROL.search(generated_at) is not None
        or generated_time.tzinfo is None
        or generated_time.utcoffset() is None
        or generated_time.utcoffset().total_seconds() != 0
        or type(payload["sources"]) is not list
        or payload["health"] != {"ok": True, "status": "ok"}
    ):
        raise LocalAPIObservationError

    def valid_utc_timestamp(value: object, *, optional: bool) -> bool:
        if value is None:
            return optional
        if (
            type(value) is not str
            or not value
            or len(value) > 40
            or _CONTROL.search(value) is not None
        ):
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            offset = parsed.utcoffset()
        except Exception:
            return False
        return (
            parsed.tzinfo is not None
            and offset is not None
            and offset.total_seconds() == 0
        )

    source_keys = {
        "providerId",
        "sourceId",
        "state",
        "lastAttemptAt",
        "lastSuccessAt",
        "staleAt",
        "errorCode",
    }
    for source in payload["sources"]:
        if type(source) is not dict or set(source) != source_keys:
            raise LocalAPIObservationError
        error_code = source["errorCode"]
        if (
            type(source["providerId"]) is not str
            or ID_PATTERN.fullmatch(source["providerId"]) is None
            or type(source["sourceId"]) is not str
            or ID_PATTERN.fullmatch(source["sourceId"]) is None
            or type(source["state"]) is not str
            or not source["state"]
            or len(source["state"]) > 64
            or _CONTROL.search(source["state"]) is not None
            or not valid_utc_timestamp(
                source["lastAttemptAt"], optional=False
            )
            or not valid_utc_timestamp(
                source["lastSuccessAt"], optional=True
            )
            or not valid_utc_timestamp(source["staleAt"], optional=True)
            or (
                error_code is not None
                and (
                    type(error_code) is not str
                    or not error_code
                    or len(error_code) > 128
                    or _CONTROL.search(error_code) is not None
                )
            )
        ):
            raise LocalAPIObservationError
    return 200, payload


class TokenBucket:
    """One bounded, thread-safe bucket for one local TCP server bearer."""

    def __init__(
        self,
        capacity: int,
        refill_per_second: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= 10_000:
            raise ValueError("rate limit capacity must be between 1 and 10000")
        if (
            isinstance(refill_per_second, bool)
            or not isinstance(refill_per_second, (int, float))
            or not math.isfinite(refill_per_second)
            or not 0 < refill_per_second <= 10_000
        ):
            raise ValueError("rate limit refill must be positive and bounded")
        self.capacity = capacity
        self.refill_per_second = float(refill_per_second)
        self.monotonic = monotonic
        self._tokens = float(capacity)
        self._updated = float(monotonic())
        self._lock = threading.Lock()

    def consume(self) -> tuple[bool, int]:
        with self._lock:
            current = float(self.monotonic())
            if not math.isfinite(current) or current < self._updated:
                current = self._updated
            elapsed = current - self._updated
            self._tokens = min(
                float(self.capacity),
                self._tokens + elapsed * self.refill_per_second,
            )
            self._updated = current
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True, 0
            wait = max(1, math.ceil((1.0 - self._tokens) / self.refill_per_second))
            return False, wait


def _compact(payload: Any) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _error(
    status: HTTPStatus,
    code: str,
    message: str,
    *,
    retry_after: int | None = None,
) -> APIProblem:
    return APIProblem(status, code, message, retry_after)


def _day(value: str, name: str) -> date:
    if len(value) != 10:
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Invalid {name}.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Invalid {name}.") from error
    if parsed.isoformat() != value:
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Invalid {name}.")
    return parsed


def _integer(value: str, name: str, *, minimum: int, maximum: int) -> int:
    if not value or len(value) > 19 or not value.isascii() or not value.isdecimal():
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Invalid {name}.")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Invalid {name}.")
    return parsed


def _ids(value: str, name: str) -> tuple[str, ...]:
    if not value:
        return ()
    values = tuple(value.split(","))
    if len(values) > MAX_ID_COUNT or any(
        not item
        or len(item) > MAX_ID_LENGTH
        or ID_PATTERN.fullmatch(item) is None
        for item in values
    ):
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Invalid {name}.")
    return values


def _timestamp(value: str, name: str) -> str:
    if len(value) > 40 or _CONTROL.search(value):
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Invalid {name}.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Invalid {name}.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Invalid {name}.")
    return value


def _parameters(raw_query: str, allowed: Collection[str]) -> dict[str, str]:
    if len(raw_query.encode("utf-8")) > MAX_QUERY_BYTES:
        raise _error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_too_large", "Request is too large.")
    if _CONTROL.search(raw_query) or _BAD_PERCENT.search(raw_query):
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_query", "Invalid query string.")
    try:
        pairs = parse_qsl(
            raw_query, keep_blank_values=True, strict_parsing=True,
            encoding="utf-8", errors="strict", max_num_fields=MAX_QUERY_FIELDS,
        ) if raw_query else []
    except (UnicodeError, ValueError) as error:
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_query", "Invalid query string.") from error
    result: dict[str, str] = {}
    for name, value in pairs:
        if name not in allowed or name in result or _CONTROL.search(name) or _CONTROL.search(value):
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_query", "Invalid query string.")
        result[name] = value
    return result


def _entity_tags(value: str) -> tuple[str, ...] | None:
    """Parse If-None-Match using RFC entity-tag list grammar.

    ``None`` represents the wildcard. Returned values are opaque tags without
    their weak/strong marker so callers can perform the required weak compare.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_QUERY_BYTES:
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header.")
    stripped = value.strip(" \t")
    if stripped == "*":
        return None

    tags: list[str] = []
    index = 0
    length = len(value)
    while True:
        while index < length and value[index] in " \t":
            index += 1
        if index >= length:
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header.")
        if value.startswith("W/", index):
            index += 2
        if index >= length or value[index] != '"':
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header.")
        index += 1
        start = index
        while index < length and value[index] != '"':
            codepoint = ord(value[index])
            if not (
                codepoint == 0x21
                or 0x23 <= codepoint <= 0x7E
                or 0x80 <= codepoint <= 0xFF
            ):
                raise _error(
                    HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header."
                )
            index += 1
        if index >= length:
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header.")
        tags.append(value[start:index])
        index += 1
        while index < length and value[index] in " \t":
            index += 1
        if index == length:
            return tuple(tags)
        if value[index] != ",":
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header.")
        index += 1


def _if_none_match(value: str, current_etag: str) -> bool:
    candidates = _entity_tags(value)
    if candidates is None:
        return True
    current = _entity_tags(current_etag)
    if current is None or len(current) != 1:  # pragma: no cover - trusted server tag
        raise RuntimeError("server ETag is invalid")
    return any(hmac.compare_digest(candidate, current[0]) for candidate in candidates)


class LocalAPIRouter:
    """Pure request router with injected query, clock, registry, and verifier."""

    ROUTES = (
        "/v1/health", "/v1/schema", "/v1/schema.json", "/v1/summary",
        "/v1/snapshot",
        "/v1/capabilities",
        "/v1/providers", "/v1/capacity", "/v1/activity/daily",
        "/v1/balances",
        "/v1/costs/daily",
        "/v1/quotas/history",
        "/v1/sources/status", "/v1/changes",
        "/v1/quick-connect",
    )

    def __init__(
        self,
        query: QueryService,
        *,
        clock: Callable[[], datetime] | None = None,
        provider_registry: Any = default_registry,
        observer_platform: ObserverPlatformResolver | None = None,
        bearer_verifier: Callable[[str], bool] | None = None,
        rate_limiter: TokenBucket | None = None,
        allowed_origins: Collection[str] = (),
        tcp_port: int | None = None,
    ) -> None:
        self.query = query
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.provider_registry = provider_registry
        self.observer_platform = (
            observer_platform
            if observer_platform is not None
            else ObserverPlatformResolver(
                default_catalog, runtime_platform=sys.platform
            )
        )
        self.bearer_verifier = bearer_verifier
        self.rate_limiter = rate_limiter
        origins = frozenset(allowed_origins)
        if any(
            not isinstance(origin, str)
            or not origin
            or origin == "*"
            or len(origin) > 512
            or _CONTROL.search(origin)
            for origin in origins
        ):
            raise ValueError("allowed origins must be explicit safe values")
        self.allowed_origins = origins
        self.tcp_port = tcp_port

    def handle(self, handler: "ReadOnlyHandler", *, include_body: bool) -> None:
        try:
            self._validate_headers(handler)
            self._authorize(handler)
            origin = self._origin(handler)
            self._reject_body(handler)
            route, params = self._target(handler.path)
            payload = self._payload(route, params)
            etag = self._etag(payload)
            headers = self._headers(origin, etag)
            validators = handler.headers.get_all("If-None-Match", failobj=[])
            if len(validators) > 1:
                raise _error(HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header.")
            if validators and _if_none_match(validators[0], etag):
                handler._send(
                    HTTPStatus.NOT_MODIFIED, b"", headers,
                    include_body=False, include_length=False,
                )
                return
            body = _compact(payload)
            handler._send(HTTPStatus.OK, body, headers, include_body=include_body)
        except APIProblem as problem:
            handler._problem(problem, include_body=include_body)
        except Exception:
            handler._problem(
                _error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "Request could not be completed."),
                include_body=include_body,
            )

    @staticmethod
    def _validate_headers(handler: "ReadOnlyHandler") -> None:
        for name, value in handler.headers.raw_items():
            if (
                not name
                or _CONTROL.search(name)
                or _CONTROL.search(value)
                or "\r" in value
                or "\n" in value
            ):
                raise _error(HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header.")
        hosts = handler.headers.get_all("Host", failobj=[])
        authorizations = handler.headers.get_all("Authorization", failobj=[])
        lengths = handler.headers.get_all("Content-Length", failobj=[])
        if len(hosts) != 1 or not hosts[0].strip() or len(authorizations) > 1 or len(lengths) > 1:
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header.")
        if lengths and (not lengths[0].isascii() or not lengths[0].isdecimal()):
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header.")

    def _authorize(self, handler: "ReadOnlyHandler") -> None:
        if self.tcp_port is not None:
            hosts = handler.headers.get_all("Host", failobj=[])
            expected = f"127.0.0.1:{self.tcp_port}"
            if len(hosts) != 1 or not hmac.compare_digest(hosts[0].strip(), expected):
                raise _error(HTTPStatus.FORBIDDEN, "forbidden_host", "Host is not allowed.")
            values = handler.headers.get_all("Authorization", failobj=[])
            if len(values) != 1 or not values[0].startswith("Bearer "):
                raise _error(HTTPStatus.UNAUTHORIZED, "authentication_required", "Authentication is required.")
            token = values[0][7:]
            if self.bearer_verifier is None or not self.bearer_verifier(token):
                raise _error(HTTPStatus.UNAUTHORIZED, "authentication_required", "Authentication is required.")
            if self.rate_limiter is not None:
                allowed, retry_after = self.rate_limiter.consume()
                if not allowed:
                    raise _error(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        "rate_limited",
                        "Request rate limit exceeded.",
                        retry_after=retry_after,
                    )

    def _origin(self, handler: "ReadOnlyHandler") -> str | None:
        values = handler.headers.get_all("Origin", failobj=[])
        if len(values) > 1:
            raise _error(HTTPStatus.FORBIDDEN, "forbidden_origin", "Origin is not allowed.")
        if not values:
            return None
        origin = values[0]
        if origin not in self.allowed_origins:
            raise _error(HTTPStatus.FORBIDDEN, "forbidden_origin", "Origin is not allowed.")
        return origin

    @staticmethod
    def _reject_body(handler: "ReadOnlyHandler") -> None:
        if handler.headers.get("Transfer-Encoding") is not None:
            raise _error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_body_not_allowed", "Request bodies are not allowed.")
        lengths = handler.headers.get_all("Content-Length", failobj=[])
        if lengths:
            length = int(lengths[0])
            if length > MAX_BODY_BYTES:
                raise _error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_body_not_allowed", "Request bodies are not allowed.")

    @staticmethod
    def _target(target: str) -> tuple[str, dict[str, str]]:
        if _CONTROL.search(target) or _BAD_PERCENT.search(target):
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_target", "Invalid request target.")
        split = urlsplit(target)
        if split.scheme or split.netloc or split.fragment or "%" in split.path:
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_target", "Invalid request target.")
        route = "/v1/schema" if split.path == "/schema" else split.path
        allowed = {
            "/v1/health": (),
            "/v1/schema": (),
            "/v1/schema.json": (),
            "/v1/summary": ("today",),
            "/v1/snapshot": ("today",),
            "/v1/capabilities": (),
            "/v1/providers": ("providerIds",),
            "/v1/capacity": ("limit",),
            "/v1/balances": ("limit",),
            "/v1/activity/daily": ("from", "to", "providerIds", "modelIds"),
            "/v1/costs/daily": ("from", "to", "providerIds", "currencies"),
            "/v1/quotas/history": ("providerId", "accountRef", "from", "to", "limit"),
            "/v1/sources/status": (),
            "/v1/changes": ("after", "limit"),
            "/v1/quick-connect": (),
        }
        if route not in allowed:
            raise _error(HTTPStatus.NOT_FOUND, "not_found", "Route was not found.")
        parameters = _parameters(split.query, allowed[route])
        if route == "/v1/providers" and "providerIds" in parameters:
            identifiers = _ids(parameters["providerIds"], "providerIds")
            if identifiers:
                parameters["providerIds"] = ",".join(sorted(set(identifiers)))
            else:
                parameters.pop("providerIds")
        return route, parameters

    def _payload(self, route: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            if route == "/v1/summary":
                now = self.clock()
                selected = _day(params["today"], "today") if "today" in params else now.astimezone().date()
                return to_wire(self.query.summary(selected))
            if route == "/v1/snapshot":
                now = self.clock()
                selected = (
                    _day(params["today"], "today")
                    if "today" in params else now.astimezone().date()
                )
                return to_wire(self.query.resource_snapshot(selected))
            if route == "/v1/capacity":
                limit = _integer(params["limit"], "limit", minimum=1, maximum=MAX_LIMIT) if "limit" in params else None
                return to_wire(self.query.capacity(limit))
            if route == "/v1/balances":
                limit = _integer(
                    params["limit"], "limit", minimum=1, maximum=MAX_LIMIT
                ) if "limit" in params else None
                return to_wire(self.query.balances(limit))
            if route == "/v1/quick-connect":
                from .quick_connect import QUICK_CONNECT

                return {
                    "schemaVersion": SCHEMA_VERSION,
                    "providers": [
                        {
                            "familyId": item.family_id,
                            "consoleUrl": item.console_url,
                            "authModes": list(item.auth_modes),
                            "apiKeyUrl": item.api_key_url,
                        }
                        for item in sorted(
                            QUICK_CONNECT.values(),
                            key=lambda item: item.family_id,
                        )
                    ],
                }
            if route == "/v1/activity/daily":
                if "from" not in params or "to" not in params:
                    raise _error(HTTPStatus.BAD_REQUEST, "missing_parameter", "Required parameter is missing.")
                return to_wire(self.query.activity(
                    _day(params["from"], "from"), _day(params["to"], "to"),
                    _ids(params.get("providerIds", ""), "providerIds"),
                    _ids(params.get("modelIds", ""), "modelIds"),
                ))
            if route == "/v1/costs/daily":
                if "from" not in params or "to" not in params:
                    raise _error(
                        HTTPStatus.BAD_REQUEST, "missing_parameter",
                        "Required parameter is missing.",
                    )
                return to_wire(self.query.costs(
                    _day(params["from"], "from"),
                    _day(params["to"], "to"),
                    _ids(params.get("providerIds", ""), "providerIds"),
                    _ids(params.get("currencies", ""), "currencies"),
                ))
            if route == "/v1/quotas/history":
                provider_id = params.get("providerId")
                account_ref = params.get("accountRef")
                if provider_id is not None:
                    parsed_provider_ids = _ids(provider_id, "providerId")
                    if len(parsed_provider_ids) != 1:
                        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", "Invalid providerId.")
                    provider_id = parsed_provider_ids[0]
                if account_ref is not None:
                    parsed_account_refs = _ids(account_ref, "accountRef")
                    if len(parsed_account_refs) != 1:
                        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", "Invalid accountRef.")
                    account_ref = parsed_account_refs[0]
                start = _timestamp(params["from"], "from") if "from" in params else None
                end = _timestamp(params["to"], "to") if "to" in params else None
                if (start is None) != (end is None):
                    raise _error(HTTPStatus.BAD_REQUEST, "missing_parameter", "Both time bounds are required.")
                limit = _integer(params.get("limit", "1000"), "limit", minimum=1, maximum=MAX_LIMIT)
                return to_wire(self.query.quota_history(
                    provider_id=provider_id, account_ref=account_ref,
                    from_time=start, to_time=end, limit=limit,
                ))
            if route == "/v1/sources/status":
                return to_wire(self.query.source_status())
            if route == "/v1/providers":
                return to_wire(self.query.provider_instances(
                    _ids(params.get("providerIds", ""), "providerIds")
                ))
            if route == "/v1/changes":
                after = _integer(params.get("after", "0"), "after", minimum=0, maximum=MAX_CURSOR)
                limit = _integer(params.get("limit", "100"), "limit", minimum=1, maximum=MAX_LIMIT)
                return to_wire(self.query.changes(after, limit))
            status = to_wire(self.query.source_status())
            if route == "/v1/health":
                status.update({"health": {"ok": True, "status": "ok"}})
                return status
            if route == "/v1/schema":
                return {
                    "schemaVersion": SCHEMA_VERSION,
                    "dataRevision": status["dataRevision"],
                    "generatedAt": status["generatedAt"],
                    "routes": list(self.ROUTES),
                    "errorShape": {"error": {"code": "string", "message": "string"}},
                }
            if route == "/v1/schema.json":
                return {
                    "schemaVersion": SCHEMA_VERSION,
                    "dataRevision": status["dataRevision"],
                    "generatedAt": status["generatedAt"],
                    "schema": LOCAL_API_SCHEMA,
                }
            if route == "/v1/capabilities":
                descriptors = self.provider_registry.descriptors
                platform_summary = self.observer_platform.summary

                def platform_support(
                    family_id: str, source_id: str
                ) -> dict[str, str]:
                    record = self.observer_platform.source_capability(
                        family_id, source_id
                    )
                    if record is not None:
                        return {
                            "state": (
                                "unknown"
                                if platform_summary.support == "unknown"
                                else (
                                    "supported"
                                    if record.supported
                                    else "unsupported"
                                )
                            ),
                            "reasonCode": record.reason_code,
                        }
                    if platform_summary.support == "unknown":
                        return {
                            "state": "unknown",
                            "reasonCode": "runtime_platform_unknown",
                        }
                    return {
                        "state": "unsupported",
                        "reasonCode": "source_level_evidence_unverified",
                    }

                providers = [{
                    "providerId": item.provider_id,
                    "familyId": item.provider_id,
                    "displayName": item.display_name,
                    "category": item.category,
                    "metricFamilies": sorted(value.value for value in item.metric_families),
                    "regions": sorted(item.regions),
                    "supportsAccounts": item.supports_accounts,
                    "capabilities": {
                        "quotaWindows": {
                            "state": item.capabilities.quota_windows.state.value,
                            "values": [
                                value.value
                                for value in item.capabilities.quota_windows.values
                            ],
                        },
                        "tokenHistory": item.capabilities.token_history.value,
                        "modelBreakdown": item.capabilities.model_breakdown.value,
                        "resetTimestamps": item.capabilities.reset_timestamps.value,
                        "billing": item.capabilities.billing.value,
                        "credits": item.capabilities.credits.value,
                        "balance": item.capabilities.balance.value,
                        "cost": item.capabilities.cost.value,
                        "rateLimits": item.capabilities.rate_limits.value,
                        "serviceStatus": item.capabilities.service_status.value,
                    },
                    "sources": [{
                        "sourceId": source.source_id,
                        "kind": source.kind.value,
                        "timeoutSeconds": source.timeout_seconds,
                        "freshnessSeconds": source.freshness_seconds,
                        "credentialType": source.credential_type.value,
                        "requiresCredential": source.credential_scope is not None,
                        "operatingSystems": sorted(
                            value.value for value in source.operating_systems
                        ),
                        "stability": source.stability.value,
                        "provenance": source.provenance.value,
                        "factFamilies": sorted(
                            value.value for value in source.fact_families
                        ),
                        "authority": source.authority.value,
                        "accountScope": source.account_scope.value,
                        "modelScope": source.model_scope.value,
                        "verification": source.verification.value,
                        "platformSupport": platform_support(
                            item.provider_id, source.source_id
                        ),
                    } for source in item.sources],
                } for item in descriptors]
                return {
                    "schemaVersion": SCHEMA_VERSION,
                    "dataRevision": status["dataRevision"],
                    "generatedAt": status["generatedAt"],
                    "upstream": {
                        "name": "openusage",
                        "version": default_catalog.upstream_version,
                        "revision": default_catalog.upstream_revision,
                        "familyCount": len(default_catalog.upstream_family_ids),
                    },
                    "observerPlatform": {
                        "operatingSystem": platform_summary.operating_system,
                        "support": platform_summary.support,
                        "supportedSourceCount": (
                            platform_summary.supported_source_count
                        ),
                        "totalSourceCount": (
                            platform_summary.total_source_count
                        ),
                        "reasonCode": platform_summary.reason_code,
                    },
                    "providers": providers,
                }
        except APIProblem:
            raise
        except ValueError as error:
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", "Invalid request parameter.") from error
        raise _error(HTTPStatus.NOT_FOUND, "not_found", "Route was not found.")

    @staticmethod
    def _etag(payload: dict[str, Any]) -> str:
        # generatedAt is representation metadata, not a semantic resource
        # change. A weak validator over every other public field remains valid
        # across clock-only renders while changing for catalog or ledger facts.
        semantic_payload = {
            key: value for key, value in payload.items() if key != "generatedAt"
        }
        material = _compact(semantic_payload)
        return 'W/"' + hashlib.sha256(material).hexdigest() + '"'

    @staticmethod
    def _headers(origin: str | None, etag: str) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "private, no-cache",
            "X-Content-Type-Options": "nosniff",
            "ETag": etag,
        }
        if origin is not None:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"
        return headers


class ReadOnlyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OpenUsageLocalAPI/1"
    sys_version = ""

    def handle_one_request(self) -> None:
        try:
            self.raw_requestline = self.rfile.readline(MAX_REQUEST_LINE + 1)
            if len(self.raw_requestline) > MAX_REQUEST_LINE:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self._problem(
                    _error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_too_large", "Request is too large."),
                    include_body=not self._request_is_head(),
                )
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            if self.request_version != "HTTP/1.1":
                self.request_version = "HTTP/1.1"
                self.close_connection = True
                self._problem(
                    _error(HTTPStatus.BAD_REQUEST, "invalid_request", "HTTP/1.1 is required."),
                    include_body=not self._request_is_head(),
                )
                return
            method = getattr(self, "do_" + self.command, None)
            if method is None:
                self._method_not_allowed(include_body=not self._request_is_head())
                return
            method()
            self.wfile.flush()
        except TimeoutError:
            self.close_connection = True

    def do_GET(self) -> None:
        if self._handle_unix_internal_shared_client_boundary():
            return
        self.server.router.handle(self, include_body=True)

    def do_HEAD(self) -> None:
        self.server.router.handle(self, include_body=False)

    def _handle_unix_internal_shared_client_boundary(self) -> bool:
        if not getattr(self.server, "_shared_client_boundary_enabled", False):
            return False
        if self.path != "/_internal/v1/shared-client-boundary-attempts":
            return False
        try:
            self.server.router._validate_headers(self)
            self.server.router._reject_body(self)
            observed = shared_client_boundary_attempt_counters()
            body = _compact(
                {
                    "apiVersion": "local-api-internal-diagnostics/v1",
                    "object": "sharedClientBoundaryAttempts",
                    "processEpochSha256": observed.process_epoch_sha256,
                    "boundedHttpOpenAttempts": observed.bounded_http_open_attempts,
                    "headlessKeychainGetAttempts": observed.headless_keychain_get_attempts,
                }
            )
            self._send(
                HTTPStatus.OK,
                body,
                {
                    "Content-Type": "application/json; charset=utf-8",
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
                include_body=True,
            )
        except APIProblem as problem:
            self._problem(problem, include_body=True)
        except Exception:
            self._problem(
                _error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "internal_error",
                    "Request could not be completed.",
                ),
                include_body=True,
            )
        return True

    def _method_not_allowed(self, *, include_body: bool = True) -> None:
        # Never drain a mutation body; close after the 405 so its bytes cannot
        # be interpreted as a second request on the persistent connection.
        self.close_connection = True
        problem = _error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Only GET and HEAD are allowed.")
        self._problem(problem, include_body=include_body, extra_headers={"Allow": "GET, HEAD"})

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_OPTIONS = _method_not_allowed

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Replace stdlib HTML/parser-detail errors with the stable JSON shape."""
        del message, explain
        status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE if code in {414, 431} else HTTPStatus.BAD_REQUEST
        problem = _error(
            status,
            "request_too_large" if status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE else "invalid_request",
            "Request is too large." if status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE else "Invalid HTTP request.",
        )
        self.close_connection = True
        # ``parse_request`` temporarily uses HTTP/0.9 for malformed versions;
        # force a framed response so the stable JSON error remains parseable.
        self.request_version = "HTTP/1.1"
        self._problem(problem, include_body=not self._request_is_head())

    def _request_is_head(self) -> bool:
        if getattr(self, "command", None) == "HEAD":
            return True
        raw_requestline = getattr(self, "raw_requestline", b"")
        raw_method = raw_requestline.split(b" ", 1)[0] if raw_requestline else b""
        return raw_method == b"HEAD"

    def _problem(
        self,
        problem: APIProblem,
        *,
        include_body: bool,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = _compact({"error": {"code": problem.code, "message": problem.message}})
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            **(extra_headers or {}),
        }
        if problem.status == HTTPStatus.UNAUTHORIZED:
            headers["WWW-Authenticate"] = "Bearer"
        if problem.retry_after is not None:
            headers["Retry-After"] = str(problem.retry_after)
        self._send(problem.status, body, headers, include_body=include_body)

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        headers: dict[str, str],
        *,
        include_body: bool,
        include_length: bool = True,
    ) -> None:
        self.close_connection = True
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Connection", "close")
        if include_length:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body and body:
            self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


class _BoundedThreads:
    daemon_threads = True
    block_on_close = False

    def _configure_threads(self, maximum: int, timeout: float, deadline: float) -> None:
        self._thread_slots = threading.BoundedSemaphore(maximum)
        self._client_timeout = timeout
        self._request_deadline = deadline
        self._deadline_lock = threading.Lock()
        self._deadline_timers: dict[socket.socket, threading.Timer] = {}
        self._closing = False

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(self._client_timeout)
        return request, address

    def handle_error(self, request: socket.socket, client_address: Any) -> None:
        # Deadline disconnects and malformed peers must never emit request,
        # authorization, path, or traceback material to process logs.
        return

    def process_request(self, request, client_address) -> None:
        if not self._thread_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._thread_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        timer = threading.Timer(
            self._request_deadline,
            self._expire_request,
            args=(request,),
        )
        timer.daemon = True
        with self._deadline_lock:
            if self._closing:
                reject = True
            else:
                self._deadline_timers[request] = timer
                timer.start()
                reject = False
        if reject:
            self.shutdown_request(request)
            self._thread_slots.release()
            return
        try:
            super().process_request_thread(request, client_address)
        finally:
            timer.cancel()
            if timer is not threading.current_thread():
                timer.join()
            with self._deadline_lock:
                self._deadline_timers.pop(request, None)
            self._thread_slots.release()

    @staticmethod
    def _expire_request(request: socket.socket) -> None:
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    @property
    def active_deadline_count(self) -> int:
        with self._deadline_lock:
            return len(self._deadline_timers)

    def _abort_active_requests(self) -> None:
        with self._deadline_lock:
            self._closing = True
            active = tuple(self._deadline_timers.items())
        for request, timer in active:
            timer.cancel()
            self._expire_request(request)
        for _, timer in active:
            if timer is not threading.current_thread():
                timer.join()
        with self._deadline_lock:
            for request, timer in active:
                if self._deadline_timers.get(request) is timer:
                    self._deadline_timers.pop(request, None)

    def server_close(self) -> None:
        self._abort_active_requests()
        super().server_close()


if hasattr(socketserver, "UnixStreamServer"):

    class UnixHTTPServer(
        _BoundedThreads,
        socketserver.ThreadingMixIn,
        socketserver.UnixStreamServer,
    ):
        allow_reuse_address = False
        request_queue_size = DEFAULT_MAX_THREADS

        def __init__(
            self,
            path: Path,
            router: LocalAPIRouter,
            *,
            max_threads: int,
            client_timeout: float,
            request_deadline: float,
            cleanup_hook: Callable[[Path], None] | None,
        ) -> None:
            self.path = path
            self.router = router
            self._shared_client_boundary_enabled = True
            self._created_identity: tuple[int, int] | None = None
            self._cleanup_hook = cleanup_hook
            _prepare_socket_path(path)
            try:
                super().__init__(str(path), ReadOnlyHandler)
                current = path.lstat()
                self._created_identity = (current.st_dev, current.st_ino)
                os.chmod(path, 0o600, follow_symlinks=False)
                self._configure_threads(max_threads, client_timeout, request_deadline)
            except Exception:
                if self._created_identity is not None:
                    _unlink_socket_if(path, self._created_identity)
                raise

        def verify_request(self, request: socket.socket, client_address: Any) -> bool:
            return _peer_is_current_user(request)

        def server_close(self) -> None:
            try:
                super().server_close()
            finally:
                _unlink_socket_if(
                    self.path,
                    self._created_identity,
                    after_quarantine=self._cleanup_hook,
                )

else:

    class UnixHTTPServer:  # pragma: no cover - Windows uses loopback TCP.
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise OSError(
                "Unix-domain HTTP is unavailable on this platform; use TCP"
            )


class LoopbackHTTPServer(_BoundedThreads, ThreadingHTTPServer):
    allow_reuse_address = False
    request_queue_size = DEFAULT_MAX_THREADS

    def __init__(
        self,
        router: LocalAPIRouter,
        port: int,
        *,
        max_threads: int,
        client_timeout: float,
        request_deadline: float,
    ) -> None:
        super().__init__(("127.0.0.1", port), ReadOnlyHandler)
        self.router = router
        self._configure_threads(max_threads, client_timeout, request_deadline)


def _prepare_socket_path(path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise OSError("socket parent must be a directory")
    if path.parent.stat().st_uid != os.getuid():
        raise OSError("socket parent must be owned by the current user")
    os.chmod(path.parent, 0o700, follow_symlinks=False)
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(current.st_mode):
        raise OSError("socket path already exists and is not a socket")
    stale_identity = (current.st_dev, current.st_ino)
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.1)
    try:
        probe.connect(str(path))
    except OSError as error:
        if error.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
            raise OSError("socket path cannot be safely replaced") from error
    else:
        raise OSError("socket path is already in use")
    finally:
        probe.close()
    current = path.lstat()
    if not stat.S_ISSOCK(current.st_mode) or (current.st_dev, current.st_ino) != stale_identity:
        raise OSError("socket path changed during stale-socket check")
    _unlink_socket_if(path, stale_identity)


def _rename_exclusive(parent_fd: int, source: str, destination: str) -> None:
    """Atomically rename within one open directory without replacing a name."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx = getattr(libc, "renameatx_np", None)
    if renameatx is None:
        # Portable fallback for Linux/Windows: rename relative to the same
        # open directory. The caller re-verifies the quarantined node identity
        # before unlinking, so a non-exclusive rename stays safe for the
        # local socket lifecycle.
        try:
            os.rename(
                source,
                destination,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        except (NotImplementedError, OSError, TypeError) as error:
            if not isinstance(error, OSError) or error.errno == errno.ENOTSUP:
                raise OSError(errno.ENOTSUP, "exclusive rename is unavailable") from error
            raise
        return
    renameatx.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameatx.restype = ctypes.c_int
    result = renameatx(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(destination),
        0x00000004,  # RENAME_EXCL from sys/stdio.h.
    )
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), destination)


def _unlink_socket_if(
    path: Path,
    identity: tuple[int, int] | None,
    *,
    after_quarantine: Callable[[Path], None] | None = None,
) -> None:
    if identity is None:
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    parent_fd = os.open(path.parent, flags)
    quarantine = f".{path.name}.quarantine-{secrets.token_hex(16)}"
    try:
        parent = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid():
            raise OSError("socket parent changed or is not user-owned")
        try:
            _rename_exclusive(parent_fd, path.name, quarantine)
        except FileNotFoundError:
            return
        if after_quarantine is not None:
            after_quarantine(path)
        current = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISSOCK(current.st_mode) and (current.st_dev, current.st_ino) == identity:
            os.unlink(quarantine, dir_fd=parent_fd)
            return
        try:
            _rename_exclusive(parent_fd, quarantine, path.name)
        except OSError as error:
            raise OSError(
                f"socket cleanup preserved an unexpected node at {path.parent / quarantine}"
            ) from error
        raise OSError("socket cleanup restored an unexpected replacement and aborted")
    finally:
        os.close(parent_fd)


def _peer_is_current_user(peer: socket.socket) -> bool:
    if hasattr(peer, "getpeereid"):
        uid, _ = peer.getpeereid()
        return uid == os.getuid()
    if hasattr(socket, "SO_PEERCRED"):
        raw = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _, uid, _ = struct.unpack("3i", raw)
        return uid == os.getuid()
    return True  # Socket mode 0600 remains the platform capability boundary.


def _validate_token(token: str) -> str:
    if (
        not isinstance(token, str)
        or len(token) < 43
        or len(token) > 256
        or not token.isascii()
        or any(character.isspace() or ord(character) < 33 for character in token)
    ):
        raise ValueError("bearer token must be a high-entropy ASCII value")
    return token


def _validated_token_path(value: object) -> Path:
    invalid = "token path must be absolute"
    try:
        raw = os.fspath(value)
    except Exception:
        raise ValueError(invalid) from None
    if (
        not isinstance(raw, str)
        or not raw
        or len(raw) > _MAX_TOKEN_PATH_LENGTH
        or any(
            ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
            for character in raw
        )
    ):
        raise ValueError(invalid)
    try:
        path = Path(raw)
    except Exception:
        raise ValueError(invalid) from None
    if (
        not path.is_absolute()
        or not path.name
        or ".." in path.parts
        or (
            os.name == "nt"
            and (
                re.fullmatch(r"[A-Za-z]:", path.drive) is None
                or ":" in raw[len(path.drive):]
            )
        )
    ):
        raise ValueError(invalid)
    return path


def _prepare_private_parent(path: Path, purpose: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise OSError(f"{purpose} parent must be a directory")
    if hasattr(os, "getuid") and path.parent.stat().st_uid != os.getuid():
        raise OSError(f"{purpose} parent must be owned by the current user")
    if _WINDOWS_FILE_SECURITY is not None:
        try:
            _WINDOWS_FILE_SECURITY.harden_directory(path.parent)
        except Exception:
            raise OSError(f"{purpose} parent is unsafe") from None
        return
    try:
        os.chmod(path.parent, 0o700, follow_symlinks=False)
    except (NotImplementedError, OSError, TypeError):
        os.chmod(path.parent, 0o700)


def _read_token(path: Path) -> str:
    unsafe = "existing token file is unsafe"
    try:
        existing = path.lstat()
    except FileNotFoundError:
        raise
    except OSError:
        raise OSError(unsafe) from None
    if (
        stat.S_ISLNK(existing.st_mode)
        or not stat.S_ISREG(existing.st_mode)
        or existing.st_nlink != 1
    ):
        raise OSError(unsafe)
    read_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        read_flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, read_flags)
    except FileNotFoundError:
        raise
    except OSError:
        raise OSError(unsafe) from None
    raw = b""
    read_error: OSError | None = None
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino)
            != (existing.st_dev, existing.st_ino)
            or (
                hasattr(os, "getuid")
                and (
                    current.st_uid != os.getuid()
                    or stat.S_IMODE(current.st_mode) != 0o600
                )
            )
        ):
            raise OSError(unsafe)
        if _WINDOWS_FILE_SECURITY is not None:
            try:
                _WINDOWS_FILE_SECURITY.verify_file(descriptor)
            except Exception:
                raise OSError(unsafe) from None
        raw = os.read(descriptor, 257)
    except OSError:
        read_error = OSError(unsafe)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            if read_error is None:
                read_error = OSError(unsafe)
    if read_error is not None:
        raise read_error from None
    try:
        return _validate_token(raw.decode("ascii"))
    except (UnicodeError, ValueError) as error:
        raise OSError(unsafe) from error


def _unlink_owned_token_node(
    path: Path,
    identity: tuple[int, int] | None,
) -> None:
    if identity is None:
        return
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISREG(current.st_mode)
        and (current.st_dev, current.st_ino) == identity
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _link_token_without_replacement(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination, follow_symlinks=False)
    except (NotImplementedError, TypeError):
        # Windows and older Python/filesystem combinations may not expose the
        # keyword even though same-directory hard-link publication is present.
        os.link(source, destination)


def _fsync_token_parent(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path.parent, flags)
    sync_error: OSError | None = None
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISDIR(current.st_mode):
            raise OSError("token parent is unsafe")
        os.fsync(descriptor)
    except OSError as error:
        sync_error = error
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            if sync_error is None:
                sync_error = error
    if sync_error is not None:
        raise sync_error


def _create_token(path: Path, token: str) -> bool:
    """Publish a complete private token atomically without replacing a node."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY

    descriptor: int | None = None
    temporary: Path | None = None
    identity: tuple[int, int] | None = None
    published = False
    primary_error_active = False
    try:
        for _ in range(16):
            candidate = path.parent / (
                f".{path.name}.tmp-{secrets.token_hex(16)}"
            )
            try:
                descriptor = os.open(candidate, flags, 0o600)
            except FileExistsError:
                continue
            temporary = candidate
            if hasattr(os, "fchmod"):
                try:
                    os.fchmod(descriptor, 0o600)
                except (NotImplementedError, OSError):
                    # The O_CREAT mode is already no broader than 0600.  Do
                    # not fall back to a path chmod before fstat establishes
                    # which inode we own; fail closed on the check below.
                    pass
            opened = os.fstat(descriptor)
            identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (
                    hasattr(os, "getuid")
                    and (
                        opened.st_uid != os.getuid()
                        or stat.S_IMODE(opened.st_mode) != 0o600
                    )
                )
            ):
                raise OSError("temporary token file is unsafe")
            if _WINDOWS_FILE_SECURITY is not None:
                try:
                    _WINDOWS_FILE_SECURITY.harden_file(descriptor)
                except Exception:
                    raise OSError("temporary token file is unsafe") from None
            break
        else:
            raise OSError("temporary token file could not be created")

        assert descriptor is not None and temporary is not None
        content = memoryview(token.encode("ascii"))
        while content:
            written = os.write(descriptor, content)
            if written <= 0:
                raise OSError("bearer token could not be persisted")
            content = content[written:]
        os.fsync(descriptor)
        completed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(completed.st_mode)
            or completed.st_nlink != 1
            or (
                hasattr(os, "getuid")
                and (
                    completed.st_uid != os.getuid()
                    or stat.S_IMODE(completed.st_mode) != 0o600
                )
            )
        ):
            raise OSError("temporary token file is unsafe")
        closing_descriptor = descriptor
        descriptor = None
        os.close(closing_descriptor)

        try:
            _link_token_without_replacement(temporary, path)
        except FileExistsError:
            return False
        published = True
        temporary.unlink()
        temporary = None

        final = path.lstat()
        if (
            not stat.S_ISREG(final.st_mode)
            or (final.st_dev, final.st_ino) != identity
            or final.st_nlink != 1
            or (
                hasattr(os, "getuid")
                and (
                    final.st_uid != os.getuid()
                    or stat.S_IMODE(final.st_mode) != 0o600
                )
            )
        ):
            raise OSError("published token file is unsafe")
        _fsync_token_parent(path)
        return True
    except BaseException:
        primary_error_active = True
        if published:
            try:
                _unlink_owned_token_node(path, identity)
            except BaseException:
                pass
        raise
    finally:
        close_error: BaseException | None = None
        if descriptor is not None:
            closing_descriptor = descriptor
            descriptor = None
            try:
                os.close(closing_descriptor)
            except BaseException as error:
                close_error = error
        cleanup_error: BaseException | None = None
        if temporary is not None:
            try:
                _unlink_owned_token_node(temporary, identity)
            except BaseException as error:
                cleanup_error = error
        if not primary_error_active:
            if close_error is not None:
                raise close_error
            if cleanup_error is not None:
                raise cleanup_error


def _load_or_create_token(path: Path, supplied: str | None) -> str:
    path = _validated_token_path(path)
    _prepare_private_parent(path, "token")
    if supplied is not None:
        token = _validate_token(supplied)
        if _create_token(path, token):
            return token
        if not hmac.compare_digest(_read_token(path), token):
            raise OSError("existing token file is unsafe or does not match")
        return token
    try:
        return _read_token(path)
    except FileNotFoundError:
        generated = _validate_token(secrets.token_urlsafe(32))
        if _create_token(path, generated):
            return generated
        return _read_token(path)


def create_unix_server(
    socket_path: str | Path,
    query: QueryService,
    *,
    allowed_origins: Collection[str] = (),
    clock: Callable[[], datetime] | None = None,
    provider_registry: Any = default_registry,
    observer_platform: ObserverPlatformResolver | None = None,
    max_threads: int = DEFAULT_MAX_THREADS,
    client_timeout: float = DEFAULT_CLIENT_TIMEOUT,
    request_deadline: float = DEFAULT_REQUEST_DEADLINE,
    cleanup_hook: Callable[[Path], None] | None = None,
) -> UnixHTTPServer:
    if isinstance(max_threads, bool) or not isinstance(max_threads, int) or not 1 <= max_threads <= 256:
        raise ValueError("max_threads must be between 1 and 256")
    if isinstance(client_timeout, bool) or not isinstance(client_timeout, (int, float)) or not 0.1 <= client_timeout <= 60:
        raise ValueError("client_timeout must be between 0.1 and 60 seconds")
    if isinstance(request_deadline, bool) or not isinstance(request_deadline, (int, float)) or not 0.05 <= request_deadline <= 300:
        raise ValueError("request_deadline must be between 0.05 and 300 seconds")
    router = LocalAPIRouter(
        query, clock=clock, provider_registry=provider_registry,
        observer_platform=observer_platform,
        allowed_origins=allowed_origins,
    )
    return UnixHTTPServer(
        Path(socket_path), router, max_threads=max_threads,
        client_timeout=client_timeout,
        request_deadline=float(request_deadline), cleanup_hook=cleanup_hook,
    )


def create_tcp_server(
    query: QueryService,
    *,
    port: int = 0,
    bearer_token: str | None = None,
    token_path: str | Path | None = None,
    allowed_origins: Collection[str] = (),
    clock: Callable[[], datetime] | None = None,
    provider_registry: Any = default_registry,
    observer_platform: ObserverPlatformResolver | None = None,
    max_threads: int = DEFAULT_MAX_THREADS,
    client_timeout: float = DEFAULT_CLIENT_TIMEOUT,
    request_deadline: float = DEFAULT_REQUEST_DEADLINE,
    rate_limit_capacity: int = DEFAULT_RATE_LIMIT_CAPACITY,
    rate_limit_refill_per_second: float = DEFAULT_RATE_LIMIT_REFILL_PER_SECOND,
    monotonic: Callable[[], float] = time.monotonic,
) -> LoopbackHTTPServer:
    """Create an explicitly opted-in IPv4 loopback server.

    The returned ``bearer_token`` attribute is for the owning process only; it
    is never logged or included in an HTTP response.
    """
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    if bearer_token is None and token_path is None:
        raise ValueError("token_path is required when generating a bearer token")
    if isinstance(max_threads, bool) or not isinstance(max_threads, int) or not 1 <= max_threads <= 256:
        raise ValueError("max_threads must be between 1 and 256")
    if isinstance(client_timeout, bool) or not isinstance(client_timeout, (int, float)) or not 0.1 <= client_timeout <= 60:
        raise ValueError("client_timeout must be between 0.1 and 60 seconds")
    if isinstance(request_deadline, bool) or not isinstance(request_deadline, (int, float)) or not 0.05 <= request_deadline <= 300:
        raise ValueError("request_deadline must be between 0.05 and 300 seconds")
    if token_path is None:
        token = _validate_token(bearer_token or "")
    else:
        validated_token_path = _validated_token_path(token_path)
        token = _load_or_create_token(validated_token_path, bearer_token)
    verifier = lambda candidate: hmac.compare_digest(token, candidate)
    rate_limiter = TokenBucket(
        rate_limit_capacity,
        rate_limit_refill_per_second,
        monotonic=monotonic,
    )
    router = LocalAPIRouter(
        query, clock=clock, provider_registry=provider_registry,
        observer_platform=observer_platform,
        bearer_verifier=verifier, rate_limiter=rate_limiter,
        allowed_origins=allowed_origins,
    )
    server = LoopbackHTTPServer(
        router, port, max_threads=max_threads, client_timeout=client_timeout,
        request_deadline=float(request_deadline),
    )
    router.tcp_port = server.server_address[1]
    server.bearer_token = token
    return server
