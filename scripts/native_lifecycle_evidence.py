#!/usr/bin/env python3
"""Generate and verify closed native final-container lifecycle evidence.

The evidence is deliberately a release non-claim.  ``generate`` executes an
independent platform driver; it never accepts a JSON assertion or trusts output
from the NSIS/AppImage under test as the lifecycle result.  An injected
executor exists only for unit tests of the assembler boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform as host_platform_module
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, fields as dataclass_fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

if os.name != "nt":
    import pwd


SCHEMA_VERSION = "native-lifecycle-evidence/v1"
OBJECT = "native.lifecycle"
MAX_REPORT_BYTES = 64 * 1024
SHA1 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
OBSERVED_AT = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z"
)
ARTIFACT_NAMES = {
    "win": re.compile(
        r"UsageHub-[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?-win-x64\.exe\Z"
    ),
    "linux": re.compile(
        r"UsageHub-[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?-linux-x86_64\.AppImage\Z"
    ),
}
SERVICE_MANAGERS = {"win": "task_scheduler", "linux": "systemd_user"}
_LINUX_GATEWAY_DEFAULT_PORT = 17823
_LINUX_NETNS_PATH = "/proc/thread-self/ns/net"
_LINUX_AF_INET = 2
_LINUX_AF_INET6 = 10
_LINUX_NETLINK_FAMILIES = (
    _LINUX_AF_INET,
    _LINUX_AF_INET6,
    _LINUX_AF_INET6,
    _LINUX_AF_INET,
)
_LINUX_NETLINK_SOCK_DIAG = 4
_LINUX_SOCK_DIAG_BY_FAMILY = 20
_LINUX_NLM_F_REQUEST_DUMP = 0x301
_LINUX_NLM_F_MULTI = 0x2
_LINUX_NLMSG_DONE = 3
_LINUX_IPPROTO_TCP = 6
_LINUX_TCP_LISTEN = 10
_LINUX_TCPF_LISTEN = 1 << _LINUX_TCP_LISTEN
_LINUX_NLMSG_HEADER_BYTES = 16
_LINUX_INET_DIAG_MSG_BYTES = 72
_LINUX_DIAG_RESPONSE_LIMIT = 4 * 1024 * 1024
_LINUX_DIAG_MESSAGE_LIMIT = 4096
_LINUX_DIAG_RECV_BYTES = 64 * 1024
_LINUX_DIAG_DEADLINE_SECONDS = 2.0
_LINUX_DIAG_OPERATION_TIMEOUT_SECONDS = 1.0
CHECK_NAMES = (
    "install",
    "firstRun",
    "observeDefault",
    "serviceRegistered",
    "uninstallPreserve",
    "serviceRemoved",
    "reinstall",
    "uninstallDelete",
    "finalStateRemoved",
)
ROOT_KEYS = frozenset(
    {
        "schemaVersion",
        "object",
        "synthetic",
        "releaseEligible",
        "observedAt",
        "sourceCommit",
        "target",
        "artifact",
        "checks",
        "gateway",
        "privacy",
        "persistence",
    }
)
OBSERVATION_KEYS = frozenset(
    {"checks", "gateway", "privacy", "persistence"}
)
ALLOWED_ERRORS = frozenset(
    {
        "arguments_invalid",
        "artifact_changed",
        "artifact_invalid",
        "artifact_unavailable",
        "binding_invalid",
        "driver_failed",
        "driver_unavailable",
        "execution_not_real",
        "host_invalid",
        "operation_failed",
        "output_exists",
        "output_write_failed",
        "record_invalid",
        "report_invalid",
        "report_not_canonical",
        "report_unavailable",
    }
)


class LifecycleEvidenceError(ValueError):
    """A path-free, allowlisted lifecycle evidence error."""


class SafeArgumentParser(argparse.ArgumentParser):
    """Collapse parser diagnostics so hostile values never reach stderr."""

    def error(self, message: str) -> None:
        del message
        _fail("arguments_invalid")


def _fail(reason: str) -> None:
    if reason not in ALLOWED_ERRORS:
        reason = "operation_failed"
    raise LifecycleEvidenceError(reason)


def _exact_keys(value: object, keys: frozenset[str] | tuple[str, ...]) -> bool:
    return type(value) is dict and set(value) == set(keys)


def _valid_observed_at(value: object) -> bool:
    if type(value) is not str or OBSERVED_AT.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return False
    return parsed.tzinfo is None


def validate_lifecycle_record(record: object) -> dict[str, Any]:
    """Validate and return one closed Windows/Linux x64 lifecycle record."""

    if not _exact_keys(record, ROOT_KEYS):
        _fail("record_invalid")
    assert isinstance(record, dict)
    if (
        type(record["schemaVersion"]) is not str
        or record["schemaVersion"] != SCHEMA_VERSION
        or type(record["object"]) is not str
        or record["object"] != OBJECT
        or record["synthetic"] is not False
        or record["releaseEligible"] is not False
        or not _valid_observed_at(record["observedAt"])
        or type(record["sourceCommit"]) is not str
        or SHA1.fullmatch(record["sourceCommit"]) is None
    ):
        _fail("record_invalid")

    target = record["target"]
    if not _exact_keys(target, ("platform", "arch", "serviceManager")):
        _fail("record_invalid")
    platform = target["platform"]
    if (
        type(platform) is not str
        or platform not in SERVICE_MANAGERS
        or type(target["arch"]) is not str
        or target["arch"] != "x64"
        or type(target["serviceManager"]) is not str
        or target["serviceManager"] != SERVICE_MANAGERS[platform]
    ):
        _fail("record_invalid")

    artifact = record["artifact"]
    if not _exact_keys(artifact, ("name", "sha256", "sizeBytes")):
        _fail("record_invalid")
    if (
        type(artifact["name"]) is not str
        or ARTIFACT_NAMES[platform].fullmatch(artifact["name"]) is None
        or type(artifact["sha256"]) is not str
        or SHA256.fullmatch(artifact["sha256"]) is None
        or type(artifact["sizeBytes"]) is not int
        or isinstance(artifact["sizeBytes"], bool)
        or artifact["sizeBytes"] <= 0
    ):
        _fail("record_invalid")

    checks = record["checks"]
    if not _exact_keys(checks, CHECK_NAMES) or any(
        type(checks[name]) is not str or checks[name] != "passed"
        for name in CHECK_NAMES
    ):
        _fail("record_invalid")

    gateway = record["gateway"]
    if not _exact_keys(
        gateway,
        ("defaultMode", "listenerActive", "cacheCreated", "telemetryCreated"),
    ) or (
        type(gateway["defaultMode"]) is not str
        or gateway["defaultMode"] != "observe"
        or gateway["listenerActive"] is not False
        or gateway["cacheCreated"] is not False
        or gateway["telemetryCreated"] is not False
    ):
        _fail("record_invalid")

    privacy = record["privacy"]
    if not _exact_keys(
        privacy, ("providerCredentialReads", "providerNetworkCalls")
    ) or any(
        type(privacy[name]) is not int or privacy[name] != 0
        for name in ("providerCredentialReads", "providerNetworkCalls")
    ):
        _fail("record_invalid")

    persistence = record["persistence"]
    if not _exact_keys(
        persistence,
        (
            "ledgerOnPreserve",
            "credentialsOnPreserve",
            "gatewayCacheOnPreserve",
            "gatewayTelemetryOnPreserve",
            "stateAfterDelete",
        ),
    ) or any(type(persistence[name]) is not str for name in persistence) or persistence != {
        "ledgerOnPreserve": "preserved",
        "credentialsOnPreserve": "not_created",
        "gatewayCacheOnPreserve": "not_created",
        "gatewayTelemetryOnPreserve": "not_created",
        "stateAfterDelete": "removed",
    }:
        _fail("record_invalid")
    return record


def _canonical(record: dict[str, Any]) -> str:
    try:
        return json.dumps(
            record,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ) + "\n"
    except (TypeError, ValueError):
        _fail("record_invalid")


def _stable_metadata_signature(
    metadata: Any,
    *,
    platform_name: str | None = None,
) -> tuple[int, ...]:
    """Return the mutation-sensitive fields that are stable on this host.

    Windows may update ``st_ctime_ns`` when a freshly written file is first
    opened.  File identity, size, modification time, and the content digest
    remain authoritative there; POSIX hosts retain the stricter ctime check.
    """

    active_platform = os.name if platform_name is None else platform_name
    signature = (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )
    if active_platform != "nt":
        signature += (metadata.st_ctime_ns,)
    return signature


def _inspect_artifact(
    path: Path, *, expected_platform: str
) -> tuple[dict[str, Any], tuple[int, ...]]:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or ARTIFACT_NAMES[expected_platform].fullmatch(path.name) is None
        ):
            _fail("artifact_invalid")
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        digest = hashlib.sha256()
        total = 0
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or _stable_metadata_signature(opened)
                != _stable_metadata_signature(metadata)
            ):
                _fail("artifact_invalid")
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
            finished = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        final_link = path.lstat()
    except LifecycleEvidenceError:
        raise
    except OSError:
        _fail("artifact_unavailable")
    if (
        total != metadata.st_size
        or _stable_metadata_signature(finished)
        != _stable_metadata_signature(opened)
        or stat.S_ISLNK(final_link.st_mode)
        or not stat.S_ISREG(final_link.st_mode)
        or _stable_metadata_signature(final_link)
        != _stable_metadata_signature(finished)
    ):
        _fail("artifact_changed")
    return (
        {
            "name": path.name,
            "sha256": digest.hexdigest(),
            "sizeBytes": total,
        },
        _stable_metadata_signature(finished),
    )


def _artifact_record(path: Path, *, expected_platform: str) -> dict[str, Any]:
    return _inspect_artifact(path, expected_platform=expected_platform)[0]


def _write_exclusive(path: Path, content: str) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        raw = content.encode("ascii")
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail("output_write_failed")
            view = view[written:]
        os.fsync(descriptor)
    except FileExistsError:
        _fail("output_exists")
    except LifecycleEvidenceError:
        raise
    except (OSError, UnicodeError):
        _fail("output_write_failed")
    finally:
        if descriptor is not None:
            os.close(descriptor)


class LifecycleExecutor(Protocol):
    execution_class: str

    def execute(
        self,
        *,
        platform: str,
        arch: str,
        artifact: Path,
        artifact_sha256: str,
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class NativePathState:
    exists: bool
    kind: str
    size_bytes: int
    sha256: str | None
    mode: int
    file_id: str | None

    def __post_init__(self) -> None:
        valid = (
            type(self.exists) is bool
            and self.kind in {"missing", "file", "directory"}
            and type(self.size_bytes) is int
            and self.size_bytes >= 0
            and type(self.mode) is int
            and 0 <= self.mode <= 0o7777
            and (self.file_id is None or type(self.file_id) is str)
            and (self.sha256 is None or SHA256.fullmatch(self.sha256) is not None)
        )
        if not valid:
            raise ValueError("native path state invalid")
        if self.exists != (self.kind != "missing"):
            raise ValueError("native path state invalid")
        if self.kind == "file" and (
            self.sha256 is None or not self.file_id
        ):
            raise ValueError("native path state invalid")
        if self.kind != "file" and self.sha256 is not None:
            raise ValueError("native path state invalid")
        if self.kind == "missing" and (
            self.size_bytes != 0 or self.mode != 0 or self.file_id is not None
        ):
            raise ValueError("native path state invalid")


@dataclass(frozen=True)
class NativeProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool

    def __post_init__(self) -> None:
        if (
            type(self.returncode) is not int
            or type(self.stdout) is not bytes
            or type(self.stderr) is not bytes
            or type(self.timed_out) is not bool
        ):
            raise ValueError("native process result invalid")


@dataclass(frozen=True)
class NativeServiceState:
    registered: bool
    active: bool
    command: tuple[str, ...] | None

    def __post_init__(self) -> None:
        if (
            type(self.registered) is not bool
            or type(self.active) is not bool
            or (
                self.command is not None
                and (
                    type(self.command) is not tuple
                    or not self.command
                    or any(type(value) is not str or not value for value in self.command)
                )
            )
            or (not self.registered and (self.active or self.command is not None))
        ):
            raise ValueError("native service state invalid")


@dataclass(frozen=True)
class NativeLedgerState:
    exists: bool
    file_id: str | None
    size_bytes: int
    sha256: str | None
    integrity_ok: bool
    revision: int

    def __post_init__(self) -> None:
        valid = (
            type(self.exists) is bool
            and (self.file_id is None or type(self.file_id) is str)
            and type(self.size_bytes) is int
            and self.size_bytes >= 0
            and (self.sha256 is None or SHA256.fullmatch(self.sha256) is not None)
            and type(self.integrity_ok) is bool
            and type(self.revision) is int
            and self.revision >= 0
        )
        if not valid:
            raise ValueError("native ledger state invalid")
        if self.exists and (
            not self.file_id or self.sha256 is None or not self.integrity_ok
        ):
            raise ValueError("native ledger state invalid")
        if not self.exists and (
            self.file_id is not None
            or self.size_bytes != 0
            or self.sha256 is not None
            or self.integrity_ok
            or self.revision != 0
        ):
            raise ValueError("native ledger state invalid")


@dataclass(frozen=True)
class NativeListenerState:
    active: bool
    authenticated_ready: bool

    def __post_init__(self) -> None:
        if (
            type(self.active) is not bool
            or type(self.authenticated_ready) is not bool
            or (self.authenticated_ready and not self.active)
        ):
            raise ValueError("native listener state invalid")


@dataclass(frozen=True, repr=False)
class LinuxManagedObserverGenerationFact:
    """Closed continuity fact for one managed Linux Observer generation."""

    generation: int
    runtime_identity_sha256: str
    process_epoch_sha256: str
    bounded_http_open_attempts_zero: bool
    headless_keychain_get_attempts_zero: bool

    def __post_init__(self) -> None:
        if _closed_linux_managed_generation_values(self) is None:
            raise ValueError("Linux managed Observer generation fact invalid")

    def __repr__(self) -> str:
        return "<LinuxManagedObserverGenerationFact closed>"


def _closed_linux_managed_generation_values(
    value: object,
) -> tuple[int, str, str, bool, bool] | None:
    if type(value) is not LinuxManagedObserverGenerationFact:
        return None
    selected = (
        value.generation,
        value.runtime_identity_sha256,
        value.process_epoch_sha256,
        value.bounded_http_open_attempts_zero,
        value.headless_keychain_get_attempts_zero,
    )
    if (
        type(selected[0]) is not int
        or selected[0] not in {1, 2}
        or type(selected[1]) is not str
        or SHA256.fullmatch(selected[1]) is None
        or type(selected[2]) is not str
        or SHA256.fullmatch(selected[2]) is None
        or selected[3] is not True
        or selected[4] is not True
    ):
        return None
    return selected


def _valid_native_path(value: object) -> bool:
    if not isinstance(value, Path) or not value.is_absolute():
        return False
    rendered = str(value)
    return (
        value != Path(value.anchor)
        and len(rendered) <= 4096
        and rendered.isprintable()
        and ".." not in value.parts
    )


@dataclass(frozen=True)
class NativeProfilePaths:
    """Authoritative current-user product paths observed outside the app."""

    state_root: Path
    config_root: Path
    runtime_root: Path
    task_definition: Path

    def __post_init__(self) -> None:
        if any(
            not _valid_native_path(value)
            for value in (
                self.state_root,
                self.config_root,
                self.runtime_root,
                self.task_definition,
            )
        ):
            raise ValueError("native profile paths invalid")


@dataclass(frozen=True)
class NativePackagePaths:
    """Authoritative installed product paths for one target platform."""

    install_root: Path
    app: Path | None
    uninstaller: Path | None
    collector: Path

    def __post_init__(self) -> None:
        if (
            not _valid_native_path(self.install_root)
            or not _valid_native_path(self.collector)
            or (self.app is None) != (self.uninstaller is None)
            or (
                self.app is not None
                and (
                    not _valid_native_path(self.app)
                    or not _valid_native_path(self.uninstaller)
                )
            )
        ):
            raise ValueError("native package paths invalid")
        descendants = [self.collector]
        if self.app is not None:
            descendants.extend((self.app, self.uninstaller))
        for value in descendants:
            assert isinstance(value, Path)
            try:
                relative = value.relative_to(self.install_root)
            except ValueError:
                raise ValueError("native package paths invalid") from None
            if not relative.parts:
                raise ValueError("native package paths invalid")


@dataclass(frozen=True)
class NativeLifecycleDependencies:
    """Closed low-level effects available to the external lifecycle driver."""

    make_run_directory: Callable[..., object]
    inspect_path: Callable[..., object]
    copy_file: Callable[..., object]
    set_file_mode: Callable[..., object]
    remove_path: Callable[..., object]
    start_process: Callable[..., object]
    run_process: Callable[..., object]
    stop_process: Callable[..., object]
    read_registry_value: Callable[..., object]
    profile_paths: Callable[..., object]
    package_paths: Callable[..., object]
    inspect_service: Callable[..., object]
    inspect_listener: Callable[..., object]
    inspect_ledger: Callable[..., object]
    network_events: Callable[..., object]
    credential_events: Callable[..., object]
    monotonic: Callable[..., object]
    wait: Callable[..., object]


def _native_file_signature(metadata: Any) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mode,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_nlink,
    )


def _hash_descriptor(descriptor: int) -> tuple[int, str]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    return total, digest.hexdigest()


def _snapshot_external_file(path: Path) -> tuple[tuple[int, ...], str]:
    descriptor: int | None = None
    try:
        if not _valid_native_path(path):
            _driver_fail()
        entry = path.lstat()
        if (
            stat.S_ISLNK(entry.st_mode)
            or not stat.S_ISREG(entry.st_mode)
            or entry.st_size <= 0
        ):
            _driver_fail()
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _native_file_signature(opened)
            != _native_file_signature(entry)
        ):
            _driver_fail()
        total, digest = _hash_descriptor(descriptor)
        finished = os.fstat(descriptor)
        final_entry = path.lstat()
        if (
            total != opened.st_size
            or _native_file_signature(finished)
            != _native_file_signature(opened)
            or _native_file_signature(final_entry)
            != _native_file_signature(opened)
        ):
            _driver_fail()
        return _native_file_signature(opened), digest
    except LifecycleEvidenceError:
        raise
    except Exception:
        _driver_fail()
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except Exception:
                _driver_fail()


def _restore_regular_entry_no_clobber(
    parent_fd: int,
    quarantine: str,
    public_name: str,
) -> None:
    os.link(
        quarantine,
        public_name,
        src_dir_fd=parent_fd,
        dst_dir_fd=parent_fd,
        follow_symlinks=False,
    )
    os.unlink(quarantine, dir_fd=parent_fd)


@dataclass
class _BoundRunFile:
    name: str
    descriptor: int
    identity: tuple[int, int]
    size_bytes: int
    sha256: str
    mode: int


@dataclass
class _BoundRunDirectory:
    path: Path
    parent_fd: int
    directory_fd: int
    identity: tuple[int, int]
    execution_copy: _BoundRunFile | None = None
    sentinel: _BoundRunFile | None = None
    execution_generation: int = 0
    authoritative_source: Path | None = None
    authoritative_source_signature: tuple[int, ...] | None = None
    authoritative_source_sha256: str | None = None

    def preserve_uninstall_token(self) -> tuple[object, ...]:
        execution = self.execution_copy
        sentinel = self.sentinel
        if (
            execution is None
            or execution.mode != 0o700
            or sentinel is None
            or sentinel.mode != 0o600
        ):
            _driver_fail()
        self.inspect_path("execution_copy", self.path / execution.name)
        self.inspect_path("sentinel", self.path / sentinel.name)
        try:
            public_root = os.stat(
                self.path.name,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
            opened_root = os.fstat(self.directory_fd)
        except Exception:
            _driver_fail()
        if (
            not stat.S_ISDIR(public_root.st_mode)
            or not stat.S_ISDIR(opened_root.st_mode)
            or (public_root.st_dev, public_root.st_ino) != self.identity
            or (opened_root.st_dev, opened_root.st_ino) != self.identity
            or stat.S_IMODE(public_root.st_mode) != 0o700
            or stat.S_IMODE(opened_root.st_mode) != 0o700
        ):
            _driver_fail()
        return (
            self.execution_generation,
            execution.name,
            execution.identity,
            execution.size_bytes,
            execution.sha256,
            execution.mode,
            sentinel.identity,
            sentinel.size_bytes,
            sentinel.sha256,
            sentinel.mode,
        )

    def prepare_preserve_uninstall(
        self,
        token: tuple[object, ...],
    ) -> tuple[int, str]:
        if type(token) is not tuple or token != self.preserve_uninstall_token():
            _driver_fail()
        execution = self.execution_copy
        assert execution is not None
        return execution.descriptor, f"/proc/self/fd/{execution.descriptor}"

    def prepare_execution_lease(self) -> tuple[tuple[object, ...], int, str]:
        token = self.preserve_uninstall_token()
        execution = self.execution_copy
        assert execution is not None
        return token, execution.descriptor, f"/proc/self/fd/{execution.descriptor}"

    def _validate_child_path(self, path: object) -> Path:
        if (
            not isinstance(path, Path)
            or not _valid_native_path(path)
            or path.parent != self.path
            or path.name != path.parts[-1]
        ):
            _driver_fail()
        return path

    def inspect_path(self, purpose: object, path: object) -> NativePathState:
        if (
            type(purpose) is not str
            or not isinstance(path, Path)
            or not _valid_native_path(path)
        ):
            _driver_fail()
        if purpose not in {
            "fresh_execution_copy",
            "execution_copy",
            "preserve_execution_copy",
            "delete_execution_copy",
            "fresh_sentinel",
            "sentinel",
        }:
            _fail("driver_unavailable")
        child = self._validate_child_path(path)
        if purpose in {
            "fresh_execution_copy",
            "execution_copy",
            "preserve_execution_copy",
            "delete_execution_copy",
        }:
            if ARTIFACT_NAMES["linux"].fullmatch(child.name) is None:
                _driver_fail()
            bound = self.execution_copy
        else:
            if child.name != "outside-product-sentinel.bin":
                _driver_fail()
            bound = self.sentinel
        if purpose in {
            "fresh_execution_copy",
            "preserve_execution_copy",
            "delete_execution_copy",
            "fresh_sentinel",
        }:
            if bound is not None:
                _driver_fail()
            if (
                purpose == "fresh_execution_copy"
                and self.execution_generation != 0
            ) or (
                purpose == "preserve_execution_copy"
                and self.execution_generation != 1
            ) or (
                purpose == "delete_execution_copy"
                and self.execution_generation != 2
            ):
                _driver_fail()
            try:
                os.stat(
                    child.name,
                    dir_fd=self.directory_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return NativePathState(False, "missing", 0, None, 0, None)
            except Exception:
                _driver_fail()
            _driver_fail()
        if bound is None or child.name != bound.name:
            _driver_fail()
        try:
            entry = os.stat(
                bound.name,
                dir_fd=self.directory_fd,
                follow_symlinks=False,
            )
            opened = os.fstat(bound.descriptor)
            total, digest = _hash_descriptor(bound.descriptor)
            finished = os.fstat(bound.descriptor)
            final_entry = os.stat(
                bound.name,
                dir_fd=self.directory_fd,
                follow_symlinks=False,
            )
        except LifecycleEvidenceError:
            raise
        except Exception:
            _driver_fail()
        if (
            not stat.S_ISREG(entry.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or (entry.st_dev, entry.st_ino) != bound.identity
            or (opened.st_dev, opened.st_ino) != bound.identity
            or _native_file_signature(finished)
            != _native_file_signature(opened)
            or not stat.S_ISREG(final_entry.st_mode)
            or (final_entry.st_dev, final_entry.st_ino) != bound.identity
            or _native_file_signature(final_entry)
            != _native_file_signature(finished)
            or final_entry.st_nlink != 1
            or final_entry.st_size != bound.size_bytes
            or stat.S_IMODE(final_entry.st_mode) != bound.mode
            or opened.st_nlink != 1
            or total != bound.size_bytes
            or opened.st_size != bound.size_bytes
            or digest != bound.sha256
            or stat.S_IMODE(opened.st_mode) != bound.mode
        ):
            _driver_fail()
        return NativePathState(
            True,
            "file",
            bound.size_bytes,
            bound.sha256,
            bound.mode,
            f"{bound.identity[0]}:{bound.identity[1]}",
        )

    def copy_file(self, source: object, destination: object) -> None:
        if not isinstance(source, Path) or not _valid_native_path(source):
            _driver_fail()
        child = self._validate_child_path(destination)
        if (
            child.name == source.name
            and ARTIFACT_NAMES["linux"].fullmatch(child.name) is not None
        ):
            if (
                self.execution_copy is not None
                or self.execution_generation >= 2
            ):
                _driver_fail()
            role = "execution_copy"
        elif child.name == "outside-product-sentinel.bin":
            if (
                self.execution_copy is None
                or self.execution_copy.mode != 0o700
                or self.sentinel is not None
            ):
                _driver_fail()
            self.inspect_path("execution_copy", self.path / self.execution_copy.name)
            role = "sentinel"
        else:
            _driver_fail()
        try:
            public_run_directory = os.stat(
                self.path.name,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
            resolved_source = source.resolve(strict=True)
            resolved_run_directory = self.path.resolve(strict=True)
            source_is_internal = (
                resolved_source == resolved_run_directory
                or resolved_run_directory in resolved_source.parents
            )
        except Exception:
            _driver_fail()
        if (
            not stat.S_ISDIR(public_run_directory.st_mode)
            or (public_run_directory.st_dev, public_run_directory.st_ino)
            != self.identity
            or source_is_internal
        ):
            _driver_fail()
        before_signature, before_sha256 = _snapshot_external_file(source)
        if role == "execution_copy" and self.execution_generation == 1:
            if (
                self.authoritative_source is None
                or resolved_source != self.authoritative_source
                or before_signature != self.authoritative_source_signature
                or before_sha256 != self.authoritative_source_sha256
            ):
                _driver_fail()
        elif role == "sentinel":
            execution = self.execution_copy
            if (
                execution is None
                or before_signature[2] != execution.size_bytes
                or before_sha256 != execution.sha256
                or self.authoritative_source is None
                or resolved_source != self.authoritative_source
                or before_signature != self.authoritative_source_signature
                or before_sha256 != self.authoritative_source_sha256
            ):
                _driver_fail()
        source_fd: int | None = None
        destination_fd: int | None = None
        try:
            source_fd = os.open(
                source,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            source_opened = os.fstat(source_fd)
            if _native_file_signature(source_opened) != before_signature:
                _driver_fail()
            destination_fd = os.open(
                child.name,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=self.directory_fd,
            )
            destination_opened = os.fstat(destination_fd)
            identity = (
                destination_opened.st_dev,
                destination_opened.st_ino,
            )
            bound = _BoundRunFile(
                name=child.name,
                descriptor=destination_fd,
                identity=identity,
                size_bytes=0,
                sha256=hashlib.sha256(b"").hexdigest(),
                mode=0o600,
            )
            if role == "execution_copy":
                self.execution_copy = bound
            else:
                self.sentinel = bound
            destination_fd = None
            if (
                not stat.S_ISREG(destination_opened.st_mode)
                or destination_opened.st_nlink != 1
                or identity == before_signature[:2]
                or (
                    role == "sentinel"
                    and self.execution_copy is not None
                    and identity == self.execution_copy.identity
                )
            ):
                _driver_fail()
            os.fchmod(bound.descriptor, 0o600)
            copied_digest = hashlib.sha256()
            copied_size = 0
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                copied_digest.update(chunk)
                copied_size += len(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(bound.descriptor, view)
                    if written <= 0:
                        _driver_fail()
                    view = view[written:]
            os.fsync(bound.descriptor)
            source_finished = os.fstat(source_fd)
            destination_opened = os.fstat(bound.descriptor)
            destination_size, destination_sha256 = _hash_descriptor(
                bound.descriptor
            )
            destination_finished = os.fstat(bound.descriptor)
            destination_entry = os.stat(
                child.name,
                dir_fd=self.directory_fd,
                follow_symlinks=False,
            )
            after_signature, after_sha256 = _snapshot_external_file(source)
            if (
                _native_file_signature(source_finished) != before_signature
                or after_signature != before_signature
                or copied_size != before_signature[2]
                or copied_digest.hexdigest() != before_sha256
                or after_sha256 != before_sha256
                or not stat.S_ISREG(destination_opened.st_mode)
                or destination_opened.st_nlink != 1
                or identity == before_signature[:2]
                or (destination_entry.st_dev, destination_entry.st_ino)
                != identity
                or _native_file_signature(destination_finished)
                != _native_file_signature(destination_opened)
                or destination_size != copied_size
                or destination_sha256 != before_sha256
                or stat.S_IMODE(destination_opened.st_mode) != 0o600
            ):
                _driver_fail()
            bound.size_bytes = destination_size
            bound.sha256 = destination_sha256
            if role == "execution_copy":
                if self.execution_generation == 0:
                    self.authoritative_source = resolved_source
                    self.authoritative_source_signature = before_signature
                    self.authoritative_source_sha256 = before_sha256
                self.execution_generation += 1
        except LifecycleEvidenceError:
            raise
        except Exception:
            _driver_fail()
        finally:
            if source_fd is not None:
                try:
                    os.close(source_fd)
                except Exception:
                    _driver_fail()
            if destination_fd is not None:
                try:
                    os.close(destination_fd)
                except Exception:
                    _driver_fail()

    def set_file_mode(self, path: object, mode: object) -> None:
        if type(mode) is not int or mode != 0o700:
            _driver_fail()
        child = self._validate_child_path(path)
        bound = self.execution_copy
        if (
            bound is None
            or child.name != bound.name
            or bound.mode != 0o600
        ):
            _driver_fail()
        self.inspect_path("execution_copy", child)
        try:
            before = os.fstat(bound.descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or (before.st_dev, before.st_ino) != bound.identity
                or before.st_nlink != 1
                or before.st_size != bound.size_bytes
                or stat.S_IMODE(before.st_mode) != 0o600
            ):
                _driver_fail()
            os.fchmod(bound.descriptor, 0o700)
            os.fsync(bound.descriptor)
            opened = os.fstat(bound.descriptor)
            total, digest = _hash_descriptor(bound.descriptor)
            finished = os.fstat(bound.descriptor)
            final_entry = os.stat(
                bound.name,
                dir_fd=self.directory_fd,
                follow_symlinks=False,
            )
        except LifecycleEvidenceError:
            raise
        except Exception:
            _driver_fail()
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != bound.identity
            or opened.st_nlink != 1
            or opened.st_size != bound.size_bytes
            or stat.S_IMODE(opened.st_mode) != 0o700
            or total != bound.size_bytes
            or digest != bound.sha256
            or _native_file_signature(finished)
            != _native_file_signature(opened)
            or not stat.S_ISREG(final_entry.st_mode)
            or (final_entry.st_dev, final_entry.st_ino) != bound.identity
            or final_entry.st_nlink != 1
            or final_entry.st_size != bound.size_bytes
            or stat.S_IMODE(final_entry.st_mode) != 0o700
            or _native_file_signature(final_entry)
            != _native_file_signature(finished)
        ):
            _driver_fail()
        bound.mode = 0o700

    def remove_path(self, path: object) -> None:
        child = self._validate_child_path(path)
        bound = self.execution_copy
        if (
            bound is None
            or child.name != bound.name
            or bound.mode != 0o700
            or self.execution_generation not in {1, 2}
        ):
            _driver_fail()
        self.inspect_path("execution_copy", child)
        self._cleanup_bound_file(bound)
        self.execution_copy = None

    def _cleanup_bound_file(self, bound: _BoundRunFile) -> None:
        quarantine = f".{bound.name}.quarantine-{secrets.token_hex(16)}"
        cleanup_failed = False
        quarantined = False
        try:
            os.rename(
                bound.name,
                quarantine,
                src_dir_fd=self.directory_fd,
                dst_dir_fd=self.directory_fd,
            )
            quarantined = True
            current = os.stat(
                quarantine,
                dir_fd=self.directory_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != bound.identity
            ):
                _restore_regular_entry_no_clobber(
                    self.directory_fd,
                    quarantine,
                    bound.name,
                )
                quarantined = False
                cleanup_failed = True
            else:
                os.unlink(quarantine, dir_fd=self.directory_fd)
                quarantined = False
        except Exception:
            cleanup_failed = True
            if quarantined:
                try:
                    _restore_regular_entry_no_clobber(
                        self.directory_fd,
                        quarantine,
                        bound.name,
                    )
                    quarantined = False
                except Exception:
                    pass
        finally:
            try:
                os.close(bound.descriptor)
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            _driver_fail()

    def cleanup(self) -> None:
        quarantine = f".{self.path.name}.quarantine-{secrets.token_hex(16)}"
        quarantined = False
        cleanup_failed = False
        try:
            if self.sentinel is not None:
                self._cleanup_bound_file(self.sentinel)
                self.sentinel = None
            if self.execution_copy is not None:
                self._cleanup_bound_file(self.execution_copy)
                self.execution_copy = None
            os.rename(
                self.path.name,
                quarantine,
                src_dir_fd=self.parent_fd,
                dst_dir_fd=self.parent_fd,
            )
            quarantined = True
            current = os.stat(
                quarantine,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != self.identity
            ):
                os.rename(
                    quarantine,
                    self.path.name,
                    src_dir_fd=self.parent_fd,
                    dst_dir_fd=self.parent_fd,
                )
                quarantined = False
                cleanup_failed = True
            else:
                os.rmdir(quarantine, dir_fd=self.parent_fd)
                quarantined = False
        except Exception:
            cleanup_failed = True
            if quarantined:
                try:
                    os.stat(
                        self.path.name,
                        dir_fd=self.parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    try:
                        os.rename(
                            quarantine,
                            self.path.name,
                            src_dir_fd=self.parent_fd,
                            dst_dir_fd=self.parent_fd,
                        )
                        quarantined = False
                    except Exception:
                        pass
                except Exception:
                    pass
        finally:
            try:
                os.close(self.directory_fd)
            except Exception:
                cleanup_failed = True
            try:
                os.close(self.parent_fd)
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            _driver_fail()

    def abandon(self) -> None:
        """Close held descriptors without mutating an unproven live root."""

        cleanup_failed = False
        for bound in (self.sentinel, self.execution_copy):
            if bound is not None:
                try:
                    os.close(bound.descriptor)
                except Exception:
                    cleanup_failed = True
        self.sentinel = None
        self.execution_copy = None
        for descriptor in (self.directory_fd, self.parent_fd):
            try:
                os.close(descriptor)
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            _driver_fail()


@dataclass(repr=False)
class _LinuxExecutionProcessLease:
    token: tuple[object, ...]
    process: subprocess.Popen[bytes]
    pid: int
    start_time_ticks: int
    process_group_id: int
    leader_reaped: bool = False

    def __repr__(self) -> str:
        return "<_LinuxExecutionProcessLease closed>"


def _force_stop_linux_execution_lease(
    lease: _LinuxExecutionProcessLease,
) -> None:
    cleanup_failed = False
    try:
        os.killpg(lease.process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        cleanup_failed = True
    try:
        if not _wait_for_linux_reserved_leader(lease.pid, 5.0):
            cleanup_failed = True
    except Exception:
        cleanup_failed = True
    try:
        _wait_for_linux_process_group_exit(lease.process_group_id)
    except Exception:
        cleanup_failed = True
    try:
        result = lease.process.wait(timeout=5.0)
        if type(result) is not int:
            cleanup_failed = True
        else:
            lease.leader_reaped = True
    except Exception:
        cleanup_failed = True
    if cleanup_failed:
        _driver_fail()


def _read_linux_process_identity(process_id: int) -> tuple[int, int, int]:
    try:
        from openusage_bar.platform_services import (
            _read_linux_process_identity as read_identity,
        )

        observed = read_identity(process_id)
    except Exception:
        _driver_fail()
    if (
        type(observed) is not tuple
        or len(observed) != 3
        or any(type(value) is not int for value in observed)
    ):
        _driver_fail()
    return observed


def _linux_process_group_has_live_member(process_group_id: int) -> bool:
    try:
        entries = os.listdir("/proc")
    except Exception:
        _driver_fail()
    for entry in entries:
        if not entry.isascii() or not entry.isdecimal():
            continue
        try:
            payload = Path("/proc", entry, "stat").read_bytes()
            if len(payload) > 4096:
                _driver_fail()
            closing = payload.rfind(b")")
            fields = payload[closing + 2 :].split()
            if closing <= 0 or len(fields) < 3:
                _driver_fail()
            state = fields[0]
            observed_group = int(fields[2])
        except (FileNotFoundError, ProcessLookupError):
            continue
        except LifecycleEvidenceError:
            raise
        except Exception:
            _driver_fail()
        if observed_group == process_group_id and state != b"Z":
            return True
    return False


def _wait_for_linux_reserved_leader(process_id: int, timeout: float) -> bool:
    started = time.monotonic()
    if type(started) is not float or not math.isfinite(started):
        _driver_fail()
    deadline = started + timeout
    previous = started
    while True:
        try:
            status = os.waitid(
                os.P_PID,
                process_id,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except Exception:
            _driver_fail()
        if status is not None and status.si_pid == process_id:
            return True
        current = time.monotonic()
        if (
            type(current) is not float
            or not math.isfinite(current)
            or current < previous
            or current >= deadline
        ):
            return False
        previous = current
        time.sleep(min(0.05, deadline - current))


def _wait_for_linux_process_group_exit(process_group_id: int) -> None:
    started = time.monotonic()
    if type(started) is not float or not math.isfinite(started):
        _driver_fail()
    deadline = started + 5.0
    previous = started
    while _linux_process_group_has_live_member(process_group_id):
        current = time.monotonic()
        if (
            type(current) is not float
            or not math.isfinite(current)
            or current < previous
            or current >= deadline
        ):
            _driver_fail()
        previous = current
        time.sleep(min(0.05, deadline - current))


def _prove_linux_default_gateway_endpoint_absent() -> None:
    """Prove no TCP 17823 listener in the calling thread's current netns."""

    try:
        import math
        import socket
        import struct
        import time

        def current_namespace_identity() -> tuple[int, int, int]:
            metadata = os.stat(_LINUX_NETNS_PATH)
            if (
                type(metadata.st_mode) is not int
                or not stat.S_ISREG(metadata.st_mode)
                or type(metadata.st_dev) is not int
                or metadata.st_dev < 0
                or type(metadata.st_ino) is not int
                or metadata.st_ino <= 0
            ):
                _fail("driver_unavailable")
            return (
                metadata.st_mode,
                metadata.st_dev,
                metadata.st_ino,
            )

        started = time.monotonic()
        if (
            type(started) not in {int, float}
            or not math.isfinite(started)
        ):
            _fail("driver_unavailable")
        deadline = started + _LINUX_DIAG_DEADLINE_SECONDS
        if not math.isfinite(deadline):
            _fail("driver_unavailable")
        last_clock = started

        def deadline_remaining() -> float:
            nonlocal last_clock
            current = time.monotonic()
            if (
                type(current) not in {int, float}
                or not math.isfinite(current)
                or current < last_clock
            ):
                _fail("driver_unavailable")
            last_clock = current
            remaining = deadline - current
            if not math.isfinite(remaining) or remaining <= 0:
                _fail("driver_unavailable")
            return remaining

        namespace_before = current_namespace_identity()
        response_bytes = 0
        response_messages = 0
        with socket.socket(
            socket.AF_NETLINK,
            socket.SOCK_RAW,
            _LINUX_NETLINK_SOCK_DIAG,
        ) as diagnostic:
            diagnostic.bind((0, 0))
            local_address = diagnostic.getsockname()
            if (
                type(local_address) is not tuple
                or len(local_address) != 2
                or type(local_address[0]) is not int
                or not 0 <= local_address[0] <= 0xFFFFFFFF
                or type(local_address[1]) is not int
                or local_address[1] != 0
            ):
                _fail("driver_unavailable")
            port_id = local_address[0]
            for sequence, family in enumerate(
                _LINUX_NETLINK_FAMILIES,
                start=1,
            ):
                request_body = struct.pack(
                    "=BBBBI48x",
                    family,
                    _LINUX_IPPROTO_TCP,
                    0,
                    0,
                    _LINUX_TCPF_LISTEN,
                )
                request = struct.pack(
                    "=IHHII",
                    _LINUX_NLMSG_HEADER_BYTES + len(request_body),
                    _LINUX_SOCK_DIAG_BY_FAMILY,
                    _LINUX_NLM_F_REQUEST_DUMP,
                    sequence,
                    port_id,
                ) + request_body
                diagnostic.settimeout(
                    min(
                        _LINUX_DIAG_OPERATION_TIMEOUT_SECONDS,
                        deadline_remaining(),
                    )
                )
                if diagnostic.sendto(request, (0, 0)) != len(request):
                    _fail("driver_unavailable")
                done = False
                while not done:
                    diagnostic.settimeout(
                        min(
                            _LINUX_DIAG_OPERATION_TIMEOUT_SECONDS,
                            deadline_remaining(),
                        )
                    )
                    received = diagnostic.recvmsg(_LINUX_DIAG_RECV_BYTES)
                    if type(received) is not tuple or len(received) != 4:
                        _fail("driver_unavailable")
                    payload, ancillary, message_flags, source = received
                    if (
                        type(payload) is not bytes
                        or type(ancillary) is not list
                        or ancillary
                        or type(source) is not tuple
                        or len(source) != 2
                        or type(source[0]) is not int
                        or source[0] != 0
                        or type(source[1]) is not int
                        or source[1] != 0
                    ):
                        _fail("driver_unavailable")
                    response_bytes += len(payload)
                    if response_bytes > _LINUX_DIAG_RESPONSE_LIMIT:
                        _fail("driver_unavailable")
                    if (
                        type(message_flags) is not int
                        or message_flags != 0
                        or not payload
                    ):
                        _fail("driver_unavailable")
                    offset = 0
                    while offset < len(payload):
                        response_messages += 1
                        if response_messages > _LINUX_DIAG_MESSAGE_LIMIT:
                            _fail("driver_unavailable")
                        if len(payload) - offset < _LINUX_NLMSG_HEADER_BYTES:
                            _fail("driver_unavailable")
                        (
                            message_length,
                            message_type,
                            response_flags,
                            response_sequence,
                            response_port_id,
                        ) = struct.unpack_from("=IHHII", payload, offset)
                        if (
                            message_length < _LINUX_NLMSG_HEADER_BYTES
                            or message_length > len(payload) - offset
                            or response_flags != _LINUX_NLM_F_MULTI
                            or response_sequence != sequence
                            or response_port_id != port_id
                        ):
                            _fail("driver_unavailable")
                        aligned_length = (message_length + 3) & ~3
                        if aligned_length > len(payload) - offset:
                            _fail("driver_unavailable")
                        if message_type == _LINUX_NLMSG_DONE:
                            done_status_valid = (
                                message_length == _LINUX_NLMSG_HEADER_BYTES
                                or (
                                    message_length
                                    == _LINUX_NLMSG_HEADER_BYTES + 4
                                    and struct.unpack_from(
                                        "=i",
                                        payload,
                                        offset + _LINUX_NLMSG_HEADER_BYTES,
                                    )[0]
                                    == 0
                                )
                            )
                            if (
                                not done_status_valid
                                or offset + aligned_length != len(payload)
                            ):
                                _fail("driver_unavailable")
                            done = True
                        elif message_type == _LINUX_SOCK_DIAG_BY_FAMILY:
                            if message_length < (
                                _LINUX_NLMSG_HEADER_BYTES
                                + _LINUX_INET_DIAG_MSG_BYTES
                            ):
                                _fail("driver_unavailable")
                            message_family = payload[
                                offset + _LINUX_NLMSG_HEADER_BYTES
                            ]
                            connection_state = payload[
                                offset + _LINUX_NLMSG_HEADER_BYTES + 1
                            ]
                            source_port = struct.unpack_from(
                                "!H",
                                payload,
                                offset + _LINUX_NLMSG_HEADER_BYTES + 4,
                            )[0]
                            if (
                                message_family != family
                                or connection_state != _LINUX_TCP_LISTEN
                            ):
                                _fail("driver_unavailable")
                            if source_port == _LINUX_GATEWAY_DEFAULT_PORT:
                                _fail("driver_unavailable")
                        else:
                            _fail("driver_unavailable")
                        offset += aligned_length
        if current_namespace_identity() != namespace_before:
            _fail("driver_unavailable")
        deadline_remaining()
    except LifecycleEvidenceError:
        raise
    except Exception:
        _fail("driver_unavailable")


@contextmanager
def native_lifecycle_dependencies_for_host() -> Iterator[NativeLifecycleDependencies]:
    """Own the low-level dependency session for the current native host.

    The partial Linux x86_64 adapter owns a private run directory; audited
    execution-copy, sentinel, mode, removal, and one recopy; authoritative
    profile/package projections; absence-only service, local-listener, and
    ledger facts; and an instantaneous current-netns TCP 17823 absence fact.
    The bound start/stop process lease, preserve and second-generation
    delete-data ``run_process`` transactions, and a closed monotonic/wait clock
    are enabled.  Each managed generation must retain one exact service,
    Local API, and shared-client process epoch fact across the Desktop process
    group stop before preserve or delete rollback is authorized.  This covers
    only the two instrumented process-local shared-client attempt boundaries;
    it is not a whole-process network or credential observation.  Generic
    Gateway state and privacy event callbacks remain unavailable, so real
    product lifecycle mutation is reachable but the default executor still
    fails closed before producing lifecycle evidence.
    Random
    quarantines and identity rechecks detect observed replacements, but are
    not isolation from a continuously malicious same-UID process after the
    final check.
    """

    try:
        active_platform = sys.platform
        active_machine = host_platform_module.machine()
    except Exception:
        _fail("driver_unavailable")
    if (
        type(active_platform) is not str
        or not active_platform.startswith("linux")
        or type(active_machine) is not str
        or active_machine.casefold() not in {"x86_64", "amd64"}
    ):
        _fail("driver_unavailable")

    run_directory: _BoundRunDirectory | None = None
    profile_home: Path | None = None
    profile_projection: NativeProfilePaths | None = None
    profile_xdg_binding: str | None = None
    profile_xdg_trusted_root: Path | None = None
    profile_xdg_runtime_parts: tuple[str, ...] | None = None
    package_projection: NativePackagePaths | None = None
    runtime_install_absence_fact: NativePathState | None = None
    service_absence_confirmed = False
    service_presence_observation: object | None = None
    local_listener_absence_confirmed = False
    process_rollback_unproven = False
    product_rollback_unproven = False
    preserve_uninstall_token: tuple[object, ...] | None = None
    last_monotonic: float | None = None
    active_process_lease: _LinuxExecutionProcessLease | None = None
    managed_generation_observation: LinuxManagedObserverGenerationFact | None = None
    managed_generation_before_values: tuple[int, str, str, bool, bool] | None = None
    stopped_execution_token: tuple[object, ...] | None = None
    managed_generation_continuity_proven = False

    def revoke_runtime_install_absence_fact() -> None:
        nonlocal runtime_install_absence_fact
        runtime_install_absence_fact = None

    def revoke_service_presence_observation() -> None:
        nonlocal service_presence_observation
        service_presence_observation = None

    def revoke_managed_generation_observation() -> None:
        nonlocal managed_generation_observation
        nonlocal managed_generation_before_values, stopped_execution_token
        nonlocal managed_generation_continuity_proven
        managed_generation_observation = None
        managed_generation_before_values = None
        stopped_execution_token = None
        managed_generation_continuity_proven = False

    def revoke_transient_observation_facts() -> None:
        nonlocal preserve_uninstall_token
        revoke_runtime_install_absence_fact()
        revoke_service_presence_observation()
        preserve_uninstall_token = None

    def revoke_all_observation_facts() -> None:
        nonlocal service_absence_confirmed, local_listener_absence_confirmed
        revoke_transient_observation_facts()
        revoke_managed_generation_observation()
        service_absence_confirmed = False
        local_listener_absence_confirmed = False

    def unavailable(*args: object, **kwargs: object) -> object:
        revoke_all_observation_facts()
        del args, kwargs
        _fail("driver_unavailable")

    def monotonic() -> float:
        nonlocal last_monotonic
        revoke_all_observation_facts()
        try:
            observed = time.monotonic()
        except Exception:
            _fail("driver_unavailable")
        if (
            type(observed) is not float
            or not math.isfinite(observed)
            or observed < 0.0
            or (last_monotonic is not None and observed < last_monotonic)
        ):
            _fail("driver_unavailable")
        last_monotonic = observed
        return observed

    def wait(seconds: object) -> None:
        revoke_all_observation_facts()
        if type(seconds) is not float or seconds != _READY_WAIT_SECONDS:
            _fail("driver_unavailable")
        try:
            time.sleep(seconds)
        except Exception:
            _fail("driver_unavailable")

    def mark_process_rollback_unproven() -> None:
        nonlocal process_rollback_unproven, product_rollback_unproven
        nonlocal preserve_uninstall_token
        process_rollback_unproven = True
        product_rollback_unproven = True
        preserve_uninstall_token = None
        revoke_managed_generation_observation()

    def mark_process_rollback_proven() -> None:
        nonlocal process_rollback_unproven, preserve_uninstall_token
        if (
            not process_rollback_unproven
            or run_directory is None
            or active_process_lease is not None
        ):
            _driver_fail()
        preserve_uninstall_token = run_directory.preserve_uninstall_token()
        process_rollback_unproven = False
        revoke_managed_generation_observation()

    def mark_managed_process_rollback_proven() -> None:
        if (
            run_directory is None
            or stopped_execution_token != run_directory.preserve_uninstall_token()
            or managed_generation_continuity_proven is not True
        ):
            _driver_fail()
        mark_process_rollback_proven()

    def closed_execution_environment() -> dict[str, str]:
        try:
            current_uid = os.getuid()
            current_home = Path(pwd.getpwuid(current_uid).pw_dir)
            expected_runtime = f"/run/user/{current_uid}"
            expected_bus = f"unix:path={expected_runtime}/systemd/private"
            selected = {
                "HOME": os.environ.get("HOME"),
                "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR"),
                "DBUS_SESSION_BUS_ADDRESS": os.environ.get(
                    "DBUS_SESSION_BUS_ADDRESS"
                ),
                "TMPDIR": os.environ.get("TMPDIR"),
                "DISPLAY": os.environ.get("DISPLAY"),
                "XAUTHORITY": os.environ.get("XAUTHORITY"),
            }
            if (
                profile_home is None
                or current_home != profile_home
                or Path.home() != current_home
                or selected["HOME"] != str(current_home)
                or selected["XDG_RUNTIME_DIR"] != expected_runtime
                or selected["DBUS_SESSION_BUS_ADDRESS"] != expected_bus
                or type(selected["TMPDIR"]) is not str
                or not os.path.isabs(selected["TMPDIR"])
                or type(selected["DISPLAY"]) is not str
                or not selected["DISPLAY"].startswith(":")
                or any(
                    character not in ":.0123456789"
                    for character in selected["DISPLAY"]
                )
                or type(selected["XAUTHORITY"]) is not str
                or not os.path.isabs(selected["XAUTHORITY"])
                or os.environ.get("XDG_CONFIG_HOME") not in {None, ""}
            ):
                _driver_fail()
            tmp_directory = selected["TMPDIR"]
            xauthority = selected["XAUTHORITY"]
            assert isinstance(tmp_directory, str)
            assert isinstance(xauthority, str)
            xauthority_metadata = os.lstat(xauthority)
            if (
                os.path.commonpath((tmp_directory, xauthority))
                != tmp_directory
                or not stat.S_ISREG(xauthority_metadata.st_mode)
                or xauthority_metadata.st_uid != current_uid
                or xauthority_metadata.st_nlink != 1
                or stat.S_IMODE(xauthority_metadata.st_mode) & 0o077 != 0
            ):
                _driver_fail()
            environment = {
                key: value
                for key, value in selected.items()
                if value is not None
            }
            if profile_xdg_binding:
                environment["XDG_DATA_HOME"] = profile_xdg_binding
            environment.update(
                {
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PYTHONNOUSERSITE": "1",
                    "APPIMAGE_EXTRACT_AND_RUN": "1",
                }
            )
            return environment
        except LifecycleEvidenceError:
            raise
        except Exception:
            _fail("driver_unavailable")

    def start_process(argv: object = None) -> _LinuxExecutionProcessLease:
        nonlocal process_rollback_unproven, product_rollback_unproven
        nonlocal preserve_uninstall_token, active_process_lease
        revoke_all_observation_facts()
        if (
            type(argv) is not tuple
            or len(argv) != 1
            or type(argv[0]) is not str
            or run_directory is None
            or active_process_lease is not None
        ):
            _driver_fail()
        token, descriptor, alias = run_directory.prepare_execution_lease()
        if argv != (str(run_directory.path / token[1]),):
            _driver_fail()
        process_rollback_unproven = True
        product_rollback_unproven = True
        preserve_uninstall_token = None
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                (alias,),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=closed_execution_environment(),
                pass_fds=(descriptor,),
            )
            if type(process.pid) is not int or process.pid <= 0:
                _driver_fail()
            lease = _LinuxExecutionProcessLease(
                token=token,
                process=process,
                pid=process.pid,
                start_time_ticks=0,
                process_group_id=process.pid,
            )
            active_process_lease = lease
            process_uid, start_time_ticks, _parent_pid = (
                _read_linux_process_identity(process.pid)
            )
            process_group_id = os.getpgid(process.pid)
            if (
                process_uid != os.getuid()
                or start_time_ticks <= 0
                or process_group_id != process.pid
            ):
                _driver_fail()
            lease.start_time_ticks = start_time_ticks
            return lease
        except LifecycleEvidenceError:
            if active_process_lease is not None:
                pending = active_process_lease
                try:
                    _force_stop_linux_execution_lease(pending)
                    active_process_lease = None
                except LifecycleEvidenceError:
                    if pending.leader_reaped:
                        active_process_lease = None
                    raise
            raise
        except Exception:
            if active_process_lease is not None:
                pending = active_process_lease
                try:
                    _force_stop_linux_execution_lease(pending)
                    active_process_lease = None
                except LifecycleEvidenceError:
                    if pending.leader_reaped:
                        active_process_lease = None
                    raise
            _driver_fail()

    def stop_process(handle: object = None) -> None:
        nonlocal active_process_lease, managed_generation_before_values
        nonlocal stopped_execution_token
        nonlocal managed_generation_continuity_proven
        before_values = managed_generation_before_values
        revoke_all_observation_facts()
        if (
            type(handle) is not _LinuxExecutionProcessLease
            or handle is not active_process_lease
            or run_directory is None
            or handle.token != run_directory.preserve_uninstall_token()
        ):
            _driver_fail()
        try:
            process_uid, start_time_ticks, _parent_pid = (
                _read_linux_process_identity(handle.pid)
            )
            if (
                process_uid != os.getuid()
                or start_time_ticks != handle.start_time_ticks
                or os.getpgid(handle.pid) != handle.process_group_id
                or handle.process_group_id != handle.pid
            ):
                _driver_fail()
            os.killpg(handle.process_group_id, signal.SIGTERM)
            if not _wait_for_linux_reserved_leader(handle.pid, 5.0):
                os.killpg(handle.process_group_id, signal.SIGKILL)
                if not _wait_for_linux_reserved_leader(handle.pid, 5.0):
                    _driver_fail()
            if _linux_process_group_has_live_member(handle.process_group_id):
                os.killpg(handle.process_group_id, signal.SIGKILL)
                _wait_for_linux_process_group_exit(handle.process_group_id)
            result = handle.process.wait(timeout=5.0)
            if type(result) is not int:
                _driver_fail()
            active_process_lease = None
            managed_generation_before_values = before_values
            stopped_execution_token = handle.token
            managed_generation_continuity_proven = False
        except LifecycleEvidenceError:
            raise
        except Exception:
            _driver_fail()

    def profile_paths(platform: object) -> NativeProfilePaths:
        nonlocal profile_home, profile_projection, package_projection
        nonlocal profile_xdg_binding, profile_xdg_trusted_root
        nonlocal profile_xdg_runtime_parts
        nonlocal runtime_install_absence_fact
        nonlocal service_absence_confirmed
        nonlocal service_presence_observation
        nonlocal local_listener_absence_confirmed
        revoke_transient_observation_facts()
        revoke_managed_generation_observation()
        profile_projection = None
        profile_xdg_binding = None
        profile_xdg_trusted_root = None
        profile_xdg_runtime_parts = None
        package_projection = None
        runtime_install_absence_fact = None
        if type(platform) is not str or platform != "linux":
            _driver_fail()
        try:
            from openusage_bar.lifecycle_state import LifecycleStatePaths

            authority = LifecycleStatePaths.for_current_user(platform="linux")
            if (
                type(authority) is not LifecycleStatePaths
                or authority.platform != "linux"
                or not _valid_native_path(authority.home)
            ):
                _driver_fail()
            configured = os.environ.get("XDG_DATA_HOME")
            if configured:
                data_root = Path(configured)
                if not _valid_native_path(data_root):
                    _driver_fail()
                try:
                    metadata = data_root.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if stat.S_ISLNK(metadata.st_mode):
                        _driver_fail()
                trusted_root = data_root
                while not os.path.lexists(trusted_root):
                    parent = trusted_root.parent
                    if parent == trusted_root:
                        break
                    trusted_root = parent
                runtime_path = data_root / "usagehub" / "runtime"
                try:
                    runtime_parts = runtime_path.relative_to(
                        trusted_root
                    ).parts
                except ValueError:
                    _driver_fail()
                if not runtime_parts:
                    _driver_fail()
                profile_xdg_binding = configured
                profile_xdg_trusted_root = trusted_root
                profile_xdg_runtime_parts = runtime_parts
            else:
                data_root = authority.home / ".local" / "share"
            profile = NativeProfilePaths(
                state_root=(
                    authority.home / ".local" / "state" / "openusage-bar"
                ),
                config_root=authority.home / ".config" / "openusage-bar",
                runtime_root=data_root / "usagehub" / "runtime",
                task_definition=(
                    authority.home
                    / ".config"
                    / "systemd"
                    / "user"
                    / "openusage-bar.service"
                ),
            )
            if profile_home is not None and profile_home != authority.home:
                _driver_fail()
            profile_home = authority.home
            profile_projection = profile
            service_absence_confirmed = False
            service_presence_observation = None
            local_listener_absence_confirmed = False
            return profile
        except LifecycleEvidenceError:
            raise
        except Exception:
            _driver_fail()

    def current_runtime_authority() -> tuple[Path, tuple[str, ...], Path]:
        if profile_projection is None or profile_home is None:
            _driver_fail()
        try:
            current_home = Path.home()
            current_xdg_binding = os.environ.get("XDG_DATA_HOME")
        except Exception:
            _fail("driver_unavailable")
        if (
            not isinstance(current_home, Path)
            or not _valid_native_path(current_home)
            or current_home != profile_home
        ):
            _fail("driver_unavailable")
        if profile_xdg_binding is None:
            if current_xdg_binding not in {None, ""}:
                _fail("driver_unavailable")
            anchor = profile_home
            relative_components = (
                ".local",
                "share",
                "usagehub",
                "runtime",
            )
            expected_runtime_root = anchor.joinpath(*relative_components)
        else:
            if (
                current_xdg_binding != profile_xdg_binding
                or profile_xdg_trusted_root is None
                or profile_xdg_runtime_parts is None
            ):
                _fail("driver_unavailable")
            try:
                current_xdg_root = Path(current_xdg_binding)
                current_trusted_root = current_xdg_root
                while not os.path.lexists(current_trusted_root):
                    parent = current_trusted_root.parent
                    if parent == current_trusted_root:
                        break
                    current_trusted_root = parent
                current_runtime_parts = (
                    current_xdg_root / "usagehub" / "runtime"
                ).relative_to(current_trusted_root).parts
            except Exception:
                _fail("driver_unavailable")
            if (
                not _valid_native_path(current_xdg_root)
                or not current_runtime_parts
                or current_trusted_root != profile_xdg_trusted_root
                or current_runtime_parts != profile_xdg_runtime_parts
            ):
                _fail("driver_unavailable")
            anchor = profile_xdg_trusted_root
            relative_components = profile_xdg_runtime_parts
            expected_runtime_root = current_xdg_root / "usagehub" / "runtime"
        if profile_projection.runtime_root != expected_runtime_root:
            _fail("driver_unavailable")
        return anchor, relative_components, expected_runtime_root

    def managed_runtime_identity_sha256(
        service: object,
        local_state: object,
    ) -> str:
        def closed_value(value: object) -> object:
            if type(value) in {str, int, bool}:
                return [type(value).__name__, value]
            if type(value) is bytes:
                return ["bytes", value.hex()]
            if isinstance(value, Path) and value.is_absolute():
                return ["path", str(value)]
            if type(value) is tuple:
                return ["tuple", [closed_value(item) for item in value]]
            _fail("driver_unavailable")

        try:
            from openusage_bar.local_api import LinuxLocalAPIState
            from openusage_bar.platform_services import LinuxCollectorServiceState

            if (
                type(service) is not LinuxCollectorServiceState
                or type(local_state) is not LinuxLocalAPIState
            ):
                _fail("driver_unavailable")
            payload = [
                [
                    "service",
                    [
                        [field.name, closed_value(getattr(service, field.name))]
                        for field in dataclass_fields(service)
                    ],
                ],
                [
                    "localApi",
                    [
                        [field.name, closed_value(getattr(local_state, field.name))]
                        for field in dataclass_fields(local_state)
                    ],
                ],
            ]
            encoded = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            return hashlib.sha256(encoded).hexdigest()
        except LifecycleEvidenceError:
            raise
        except Exception:
            _fail("driver_unavailable")

    def consume_linux_managed_generation_fact(
    ) -> LinuxManagedObserverGenerationFact:
        nonlocal managed_generation_observation
        nonlocal managed_generation_before_values
        nonlocal managed_generation_continuity_proven
        observed = managed_generation_observation
        managed_generation_observation = None
        values = _closed_linux_managed_generation_values(observed)
        if values is None or run_directory is None:
            _driver_fail()
        if active_process_lease is not None:
            if (
                stopped_execution_token is not None
                or values[0] != run_directory.execution_generation
                or values[0] != active_process_lease.token[0]
            ):
                _driver_fail()
            managed_generation_before_values = values
            managed_generation_continuity_proven = False
        else:
            if (
                stopped_execution_token is None
                or values[0] != stopped_execution_token[0]
                or managed_generation_before_values is None
                or values != managed_generation_before_values
            ):
                _driver_fail()
            managed_generation_continuity_proven = True
        return LinuxManagedObserverGenerationFact(*values)

    def inspect_service(platform: object) -> NativeServiceState:
        nonlocal runtime_install_absence_fact
        nonlocal service_absence_confirmed, local_listener_absence_confirmed
        nonlocal service_presence_observation
        revoke_transient_observation_facts()
        service_presence_observation = None
        if (
            type(platform) is not str
            or platform != "linux"
            or profile_home is None
        ):
            _driver_fail()
        service_absence_confirmed = False
        local_listener_absence_confirmed = False
        try:
            configured_xdg_home = os.environ.get("XDG_CONFIG_HOME")
        except Exception:
            _fail("driver_unavailable")
        if configured_xdg_home not in {None, ""}:
            _fail("driver_unavailable")
        try:
            from openusage_bar.platform_services import service_is_registered

            registered = service_is_registered(
                platform="linux",
                home=profile_home,
            )
        except Exception:
            _fail("driver_unavailable")
        if registered is False:
            service_absence_confirmed = True
            return NativeServiceState(False, False, None)
        if registered is not True:
            _fail("driver_unavailable")
        if package_projection is None or profile_projection is None:
            _driver_fail()
        try:
            from openusage_bar.platform_services import (
                LinuxCollectorServiceState,
                read_current_user_collector_service_state,
                systemd_unit,
            )

            observed = read_current_user_collector_service_state()
            collector = package_projection.collector
            api_socket = profile_projection.state_root / "openusage.sock"
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
            expected_unit = systemd_unit(
                interval=300,
                api_socket=str(api_socket),
                command=str(collector),
            ).encode("utf-8")
            expected_argv_nul = ("\0".join(command) + "\0").encode("utf-8")
        except LifecycleEvidenceError:
            raise
        except Exception:
            _fail("driver_unavailable")
        if (
            type(observed) is not LinuxCollectorServiceState
            or observed.unit_size_bytes != len(expected_unit)
            or observed.unit_sha256 != hashlib.sha256(expected_unit).hexdigest()
            or observed.unit_id != "openusage-bar.service"
            or observed.load_state != "loaded"
            or observed.active_state != "active"
            or observed.sub_state != "running"
            or observed.unit_file_state != "enabled"
            or observed.fragment_path != profile_projection.task_definition
            or observed.drop_in_paths != ()
            or observed.needs_reload
            or observed.process_uid != os.getuid()
            or observed.process_start_time_ticks <= 0
            or observed.process_executable != collector
            or observed.process_argv_nul != expected_argv_nul
        ):
            _fail("driver_unavailable")
        service_presence_observation = observed
        return NativeServiceState(True, True, command)

    def prove_authoritative_path_absence(
        *,
        anchor: Path,
        relative_components: tuple[str, ...],
        root_missing: bool,
        entry_names: tuple[str, ...],
    ) -> None:
        if (
            not isinstance(anchor, Path)
            or not anchor.is_absolute()
            or not str(anchor).isprintable()
            or ".." in anchor.parts
            or type(relative_components) is not tuple
            or not relative_components
            or any(
                type(component) is not str
                or not component
                or not component.isprintable()
                or component in {".", ".."}
                or "/" in component
                for component in relative_components
            )
            or type(root_missing) is not bool
            or type(entry_names) is not tuple
            or root_missing == bool(entry_names)
            or any(
                type(name) is not str
                or not name
                or not name.isprintable()
                or name in {".", ".."}
                or "/" in name
                for name in entry_names
            )
        ):
            _fail("driver_unavailable")
        descriptors: list[int] = []
        bindings: list[tuple[int, str, tuple[int, int]]] = []
        try:
            home_entry = anchor.lstat()
            directory_flag = getattr(os, "O_DIRECTORY", None)
            nofollow_flag = getattr(os, "O_NOFOLLOW", None)
            if (
                type(directory_flag) is not int
                or directory_flag == 0
                or type(nofollow_flag) is not int
                or nofollow_flag == 0
            ):
                _fail("driver_unavailable")
            directory_flags = (
                os.O_RDONLY
                | directory_flag
                | nofollow_flag
                | getattr(os, "O_CLOEXEC", 0)
            )
            home_fd = os.open(anchor, directory_flags)
            descriptors.append(home_fd)
            home_opened = os.fstat(home_fd)
            if (
                stat.S_ISLNK(home_entry.st_mode)
                or not stat.S_ISDIR(home_entry.st_mode)
                or not stat.S_ISDIR(home_opened.st_mode)
                or (home_entry.st_dev, home_entry.st_ino)
                != (home_opened.st_dev, home_opened.st_ino)
            ):
                _fail("driver_unavailable")
            home_identity = (home_opened.st_dev, home_opened.st_ino)

            current_fd = home_fd
            missing_parent_fd: int | None = None
            missing_name: str | None = None
            for component in relative_components:
                try:
                    child_fd = os.open(
                        component,
                        directory_flags,
                        dir_fd=current_fd,
                    )
                except FileNotFoundError:
                    missing_parent_fd = current_fd
                    missing_name = component
                    break
                descriptors.append(child_fd)
                child = os.fstat(child_fd)
                if not stat.S_ISDIR(child.st_mode):
                    _fail("driver_unavailable")
                bindings.append(
                    (
                        current_fd,
                        component,
                        (child.st_dev, child.st_ino),
                    )
                )
                current_fd = child_fd
            else:
                if root_missing:
                    _fail("driver_unavailable")
                for entry_name in entry_names:
                    try:
                        os.stat(
                            entry_name,
                            dir_fd=current_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        continue
                    _fail("driver_unavailable")

            def revalidate_public_chain() -> None:
                final_home = anchor.lstat()
                if (
                    stat.S_ISLNK(final_home.st_mode)
                    or not stat.S_ISDIR(final_home.st_mode)
                    or (final_home.st_dev, final_home.st_ino)
                    != home_identity
                ):
                    _fail("driver_unavailable")
                for parent_fd, component, identity in bindings:
                    current = os.stat(
                        component,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISDIR(current.st_mode)
                        or (current.st_dev, current.st_ino) != identity
                    ):
                        _fail("driver_unavailable")

            revalidate_public_chain()
            if missing_parent_fd is not None:
                assert missing_name is not None
                try:
                    os.stat(
                        missing_name,
                        dir_fd=missing_parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    _fail("driver_unavailable")
            else:
                if root_missing:
                    _fail("driver_unavailable")
                for entry_name in entry_names:
                    try:
                        os.stat(
                            entry_name,
                            dir_fd=current_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        continue
                    _fail("driver_unavailable")
            revalidate_public_chain()
            if root_missing:
                assert missing_parent_fd is not None
                assert missing_name is not None
                revalidate_public_chain()
                try:
                    os.stat(
                        missing_name,
                        dir_fd=missing_parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    _fail("driver_unavailable")
        except LifecycleEvidenceError:
            raise
        except Exception:
            _fail("driver_unavailable")
        finally:
            close_failed = False
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except Exception:
                    close_failed = True
            if close_failed:
                _fail("driver_unavailable")

    def inspect_listener(
        platform: object,
        namespace: object,
    ) -> NativeListenerState:
        nonlocal runtime_install_absence_fact
        nonlocal service_absence_confirmed, local_listener_absence_confirmed
        nonlocal service_presence_observation, preserve_uninstall_token
        nonlocal managed_generation_observation
        revoke_runtime_install_absence_fact()
        preserve_uninstall_token = None
        positive_service_observation = service_presence_observation
        service_presence_observation = None
        if (
            type(platform) is not str
            or platform != "linux"
            or type(namespace) is not str
            or namespace
            not in {"local", "gateway", "gateway_default_endpoint"}
        ):
            _driver_fail()
        if namespace in {"gateway", "gateway_default_endpoint"}:
            if profile_home is None:
                _driver_fail()
            if namespace == "gateway":
                _fail("driver_unavailable")
            # Instantaneous TCP 17823 fact in the calling thread's current
            # network namespace; this is not a generic Gateway-state claim.
            _prove_linux_default_gateway_endpoint_absent()
            return NativeListenerState(False, False)
        local_listener_absence_confirmed = False
        if profile_home is None:
            _driver_fail()
        if not service_absence_confirmed:
            if positive_service_observation is None:
                _driver_fail()
            try:
                from openusage_bar.local_api import (
                    LinuxLocalAPIState,
                    closed_linux_shared_client_boundary_values,
                    read_current_user_local_api_state,
                    read_current_user_shared_client_boundary_state,
                )
                from openusage_bar.platform_services import (
                    LinuxCollectorServiceState,
                    read_current_user_collector_service_state,
                )

                boundary_before = (
                    read_current_user_shared_client_boundary_state()
                )
                local_state = read_current_user_local_api_state()
                boundary_after = (
                    read_current_user_shared_client_boundary_state()
                )
                service_after = read_current_user_collector_service_state()
                current_uid = os.getuid()
                current_gid = os.getgid()

                boundary_before_values = (
                    closed_linux_shared_client_boundary_values(
                        boundary_before
                    )
                )
                boundary_after_values = (
                    closed_linux_shared_client_boundary_values(boundary_after)
                )
                if (
                    type(positive_service_observation)
                    is not LinuxCollectorServiceState
                    or type(service_after) is not LinuxCollectorServiceState
                    or type(local_state) is not LinuxLocalAPIState
                    or boundary_before_values is None
                    or boundary_after_values is None
                    or boundary_before_values != boundary_after_values
                    or boundary_before_values[4:] != (0, 0)
                    or service_after != positive_service_observation
                ):
                    _fail("driver_unavailable")
                expected_executable_path_sha256 = hashlib.sha256(
                    os.fsencode(positive_service_observation.process_executable)
                ).hexdigest()
                expected_argv_sha256 = hashlib.sha256(
                    positive_service_observation.process_argv_nul
                ).hexdigest()
                expected_cgroup = (
                    "0::/user.slice/"
                    f"user-{current_uid}.slice/user@{current_uid}.service/"
                    "app.slice/openusage-bar.service\n"
                ).encode("ascii")
                expected_cgroup_sha256 = hashlib.sha256(
                    expected_cgroup
                ).hexdigest()
                peer_is_main = (
                    local_state.peer_pid
                    == positive_service_observation.main_pid
                    and local_state.peer_start_time_ticks
                    == positive_service_observation.process_start_time_ticks
                )
                peer_is_stable_direct_child = (
                    local_state.peer_pid
                    != positive_service_observation.main_pid
                    and local_state.peer_parent_pid
                    == positive_service_observation.main_pid
                    and local_state.peer_start_time_ticks
                    > positive_service_observation.process_start_time_ticks
                )
                if (
                    local_state.socket_mode != 0o600
                    or local_state.socket_uid != current_uid
                    or boundary_before_values[:3]
                    != (
                        local_state.peer_pid,
                        local_state.peer_uid,
                        local_state.peer_gid,
                    )
                    or not (peer_is_main or peer_is_stable_direct_child)
                    or local_state.peer_uid != current_uid
                    or local_state.peer_uid
                    != positive_service_observation.process_uid
                    or local_state.peer_gid != current_gid
                    or local_state.peer_executable_file_id
                    != positive_service_observation.process_executable_file_id
                    or local_state.peer_executable_signature_sha256
                    != positive_service_observation.process_executable_signature_sha256
                    or local_state.peer_executable_path_sha256
                    != expected_executable_path_sha256
                    or local_state.peer_argv_sha256 != expected_argv_sha256
                    or local_state.peer_cgroup_sha256
                    != expected_cgroup_sha256
                    or local_state.http_status != 200
                    or local_state.schema_version != "1.0"
                    or local_state.health_ok is not True
                    or local_state.health_status != "ok"
                ):
                    _fail("driver_unavailable")
                execution_token = (
                    active_process_lease.token
                    if active_process_lease is not None
                    else stopped_execution_token
                )
                if execution_token is not None:
                    if (
                        run_directory is None
                        or execution_token
                        != run_directory.preserve_uninstall_token()
                        or type(execution_token[0]) is not int
                        or execution_token[0] not in {1, 2}
                    ):
                        _fail("driver_unavailable")
                    managed_generation_observation = (
                        LinuxManagedObserverGenerationFact(
                            generation=execution_token[0],
                            runtime_identity_sha256=(
                                managed_runtime_identity_sha256(
                                    positive_service_observation,
                                    local_state,
                                )
                            ),
                            process_epoch_sha256=boundary_before_values[3],
                            bounded_http_open_attempts_zero=True,
                            headless_keychain_get_attempts_zero=True,
                        )
                    )
            except LifecycleEvidenceError:
                raise
            except Exception:
                _fail("driver_unavailable")
            return NativeListenerState(True, True)
        service_absence_confirmed = False
        prove_authoritative_path_absence(
            anchor=profile_home,
            relative_components=(".local", "state", "openusage-bar"),
            root_missing=False,
            entry_names=("openusage.sock",),
        )
        try:
            from openusage_bar.platform_services import service_is_registered

            registered = service_is_registered(
                platform="linux",
                home=profile_home,
            )
        except Exception:
            _fail("driver_unavailable")
        if registered is not False:
            _fail("driver_unavailable")
        prove_authoritative_path_absence(
            anchor=profile_home,
            relative_components=(".local", "state", "openusage-bar"),
            root_missing=False,
            entry_names=("openusage.sock",),
        )
        local_listener_absence_confirmed = True
        return NativeListenerState(False, False)

    def inspect_ledger(platform: object) -> NativeLedgerState:
        nonlocal runtime_install_absence_fact
        nonlocal local_listener_absence_confirmed
        revoke_transient_observation_facts()
        if (
            type(platform) is not str
            or platform != "linux"
            or profile_home is None
            or not local_listener_absence_confirmed
        ):
            _driver_fail()
        local_listener_absence_confirmed = False
        ledger_entries = (
            "activity.sqlite3",
            "activity.sqlite3-wal",
            "activity.sqlite3-shm",
            "activity.sqlite3-journal",
        )
        prove_authoritative_path_absence(
            anchor=profile_home,
            relative_components=(".local", "state", "openusage-bar"),
            root_missing=False,
            entry_names=ledger_entries,
        )
        prove_authoritative_path_absence(
            anchor=profile_home,
            relative_components=(".local", "state", "openusage-bar"),
            root_missing=False,
            entry_names=("openusage.sock",),
        )
        try:
            from openusage_bar.platform_services import service_is_registered

            registered = service_is_registered(
                platform="linux",
                home=profile_home,
            )
        except Exception:
            _fail("driver_unavailable")
        if registered is not False:
            _fail("driver_unavailable")
        prove_authoritative_path_absence(
            anchor=profile_home,
            relative_components=(".local", "state", "openusage-bar"),
            root_missing=False,
            entry_names=("openusage.sock",),
        )
        prove_authoritative_path_absence(
            anchor=profile_home,
            relative_components=(".local", "state", "openusage-bar"),
            root_missing=False,
            entry_names=ledger_entries,
        )
        return NativeLedgerState(False, None, 0, None, False, 0)

    def package_paths(
        platform: object,
        profile: object,
    ) -> NativePackagePaths:
        nonlocal package_projection, runtime_install_absence_fact
        revoke_transient_observation_facts()
        revoke_managed_generation_observation()
        package_projection = None
        runtime_install_absence_fact = None
        if (
            type(platform) is not str
            or platform != "linux"
            or type(profile) is not NativeProfilePaths
        ):
            _driver_fail()
        assert isinstance(profile, NativeProfilePaths)
        if any(
            not _valid_native_path(value)
            for value in (
                profile.state_root,
                profile.config_root,
                profile.runtime_root,
                profile.task_definition,
            )
        ):
            _driver_fail()
        try:
            try:
                metadata = profile.runtime_root.lstat()
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISLNK(metadata.st_mode):
                    _driver_fail()
            package = NativePackagePaths(
                install_root=profile.runtime_root,
                app=None,
                uninstaller=None,
                collector=profile.runtime_root / "openusage-collector",
            )
            if profile_projection is not None and profile == profile_projection:
                package_projection = package
            return package
        except LifecycleEvidenceError:
            raise
        except Exception:
            _driver_fail()

    def make_run_directory(platform: object, arch: object) -> Path:
        nonlocal run_directory
        revoke_transient_observation_facts()
        revoke_managed_generation_observation()
        if (
            type(platform) is not str
            or platform != "linux"
            or type(arch) is not str
            or arch != "x64"
            or run_directory is not None
        ):
            _driver_fail()
        parent_fd: int | None = None
        directory_fd: int | None = None
        child_name: str | None = None
        created_identity: tuple[int, int] | None = None
        entry_changed = False
        try:
            temp_root = Path(tempfile.gettempdir())
            if not _valid_native_path(temp_root):
                _driver_fail()
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            root_entry = temp_root.lstat()
            parent_fd = os.open(temp_root, flags)
            root_opened = os.fstat(parent_fd)
            root_mode = stat.S_IMODE(root_opened.st_mode)
            safe_temp_root = (
                root_opened.st_uid == os.getuid()
                and root_mode & 0o022 == 0
            ) or (
                root_opened.st_uid == 0
                and root_mode & stat.S_ISVTX != 0
                and root_mode & 0o002 != 0
            )
            if (
                stat.S_ISLNK(root_entry.st_mode)
                or not stat.S_ISDIR(root_entry.st_mode)
                or not stat.S_ISDIR(root_opened.st_mode)
                or (root_entry.st_dev, root_entry.st_ino)
                != (root_opened.st_dev, root_opened.st_ino)
                or not safe_temp_root
            ):
                _driver_fail()
            child_name = (
                "usagehub-native-lifecycle-" + secrets.token_hex(16)
            )
            candidate = temp_root / child_name
            os.mkdir(child_name, 0o700, dir_fd=parent_fd)
            created = os.stat(
                child_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            created_identity = (created.st_dev, created.st_ino)
            if (
                not stat.S_ISDIR(created.st_mode)
                or stat.S_ISLNK(created.st_mode)
                or stat.S_IMODE(created.st_mode) != 0o700
                or created.st_uid != os.getuid()
            ):
                _driver_fail()
            directory_fd = os.open(child_name, flags, dir_fd=parent_fd)
            metadata = os.fstat(directory_fd)
            entry = os.stat(
                child_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            entry_changed = (
                (metadata.st_dev, metadata.st_ino) != created_identity
                or (entry.st_dev, entry.st_ino) != created_identity
            )
            if (
                not candidate.is_absolute()
                or stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or entry_changed
            ):
                _driver_fail()
        except Exception:
            if directory_fd is not None:
                try:
                    os.close(directory_fd)
                except Exception:
                    pass
            if (
                parent_fd is not None
                and child_name is not None
                and created_identity is not None
                and not entry_changed
            ):
                quarantine = (
                    f".{child_name}.quarantine-{secrets.token_hex(16)}"
                )
                try:
                    os.rename(
                        child_name,
                        quarantine,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    current = os.stat(
                        quarantine,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    if (
                        stat.S_ISDIR(current.st_mode)
                        and (current.st_dev, current.st_ino)
                        == created_identity
                    ):
                        os.rmdir(quarantine, dir_fd=parent_fd)
                    else:
                        os.rename(
                            quarantine,
                            child_name,
                            src_dir_fd=parent_fd,
                            dst_dir_fd=parent_fd,
                        )
                except Exception:
                    pass
            if parent_fd is not None:
                try:
                    os.close(parent_fd)
                except Exception:
                    pass
            _driver_fail()
        assert parent_fd is not None
        assert directory_fd is not None
        run_directory = _BoundRunDirectory(
            path=candidate,
            parent_fd=parent_fd,
            directory_fd=directory_fd,
            identity=(metadata.st_dev, metadata.st_ino),
        )
        return candidate

    def inspect_path(purpose: object, path: object) -> NativePathState:
        nonlocal runtime_install_absence_fact, preserve_uninstall_token
        pending_runtime_install_fact = runtime_install_absence_fact
        runtime_install_absence_fact = None
        preserve_uninstall_token = None
        revoke_service_presence_observation()
        if run_directory is None:
            _driver_fail()
        try:
            input_is_valid = (
                type(purpose) is str
                and isinstance(path, Path)
                and _valid_native_path(path)
            )
        except Exception:
            _driver_fail()
        if not input_is_valid:
            _driver_fail()
        if pending_runtime_install_fact is not None:
            try:
                alias_is_exact = (
                    purpose == "fresh_install_root"
                    and profile_projection is not None
                    and package_projection is not None
                    and package_projection.install_root
                    == profile_projection.runtime_root
                    and path == package_projection.install_root
                )
            except Exception:
                _driver_fail()
            if alias_is_exact is not True:
                _driver_fail()
            return pending_runtime_install_fact
        if purpose == "fresh_install_root":
            _driver_fail()
        if purpose == "fresh_runtime_root":
            if (
                profile_projection is None
                or package_projection is None
                or profile_home is None
                or package_projection.install_root
                != profile_projection.runtime_root
                or path != profile_projection.runtime_root
            ):
                _driver_fail()
            anchor, relative_components, _expected_runtime_root = (
                current_runtime_authority()
            )
            prove_authoritative_path_absence(
                anchor=anchor,
                relative_components=relative_components,
                root_missing=True,
                entry_names=(),
            )
            fact = NativePathState(False, "missing", 0, None, 0, None)
            runtime_install_absence_fact = fact
            return fact
        if purpose == "fresh_stable_collector":
            if (
                profile_projection is None
                or package_projection is None
                or profile_home is None
            ):
                _driver_fail()
            try:
                collector_is_exact = (
                    package_projection.install_root
                    == profile_projection.runtime_root
                    and package_projection.collector
                    == profile_projection.runtime_root
                    / "openusage-collector"
                    and path == package_projection.collector
                )
            except Exception:
                _driver_fail()
            if collector_is_exact is not True:
                _driver_fail()
            try:
                configured_xdg_home = os.environ.get("XDG_CONFIG_HOME")
            except Exception:
                _fail("driver_unavailable")
            if configured_xdg_home not in {None, ""}:
                _fail("driver_unavailable")
            anchor, relative_components, _expected_runtime_root = (
                current_runtime_authority()
            )
            prove_authoritative_path_absence(
                anchor=anchor,
                relative_components=relative_components,
                root_missing=True,
                entry_names=(),
            )
            return NativePathState(False, "missing", 0, None, 0, None)
        if purpose == "fresh_task_definition":
            if profile_projection is None or package_projection is None:
                _driver_fail()
            try:
                task_path_is_exact = path == profile_projection.task_definition
            except Exception:
                _driver_fail()
            if task_path_is_exact is not True:
                _driver_fail()
            try:
                current_home = Path.home()
                configured_xdg_home = os.environ.get("XDG_CONFIG_HOME")
                authority_is_exact = (
                    isinstance(current_home, Path)
                    and _valid_native_path(current_home)
                    and current_home == profile_home
                )
            except Exception:
                _fail("driver_unavailable")
            if (
                authority_is_exact is not True
                or configured_xdg_home not in {None, ""}
            ):
                _fail("driver_unavailable")
            prove_authoritative_path_absence(
                anchor=profile_home,
                relative_components=(
                    ".config",
                    "systemd",
                    "user",
                    "openusage-bar.service",
                ),
                root_missing=True,
                entry_names=(),
            )
            return NativePathState(False, "missing", 0, None, 0, None)
        if (
            profile_projection is not None
            and path == profile_projection.state_root
            and purpose not in {"fresh_state_root", "fresh_config_root"}
        ):
            _driver_fail()
        if purpose == "fresh_config_root":
            if (
                profile_projection is None
                or package_projection is None
                or path != profile_projection.config_root
            ):
                _driver_fail()
            try:
                current_home = Path.home()
            except Exception:
                _fail("driver_unavailable")
            if (
                not isinstance(current_home, Path)
                or not _valid_native_path(current_home)
                or current_home != profile_home
            ):
                _fail("driver_unavailable")
            prove_authoritative_path_absence(
                anchor=profile_home,
                relative_components=(".config", "openusage-bar"),
                root_missing=True,
                entry_names=(),
            )
            return NativePathState(False, "missing", 0, None, 0, None)
        if purpose == "fresh_state_root":
            if (
                profile_projection is None
                or package_projection is None
                or path != profile_projection.state_root
            ):
                _driver_fail()
            prove_authoritative_path_absence(
                anchor=profile_home,
                relative_components=(".local", "state", "openusage-bar"),
                root_missing=True,
                entry_names=(),
            )
            return NativePathState(False, "missing", 0, None, 0, None)
        return run_directory.inspect_path(purpose, path)

    def copy_file(source: object, destination: object) -> None:
        revoke_transient_observation_facts()
        revoke_managed_generation_observation()
        if run_directory is None:
            _driver_fail()
        run_directory.copy_file(source, destination)

    def set_file_mode(path: object, mode: object) -> None:
        revoke_transient_observation_facts()
        revoke_managed_generation_observation()
        if run_directory is None:
            _driver_fail()
        run_directory.set_file_mode(path, mode)

    def remove_path(path: object) -> None:
        revoke_transient_observation_facts()
        revoke_managed_generation_observation()
        if run_directory is None:
            _driver_fail()
        run_directory.remove_path(path)

    def run_process(argv: object, timeout: object) -> NativeProcessResult:
        nonlocal preserve_uninstall_token, product_rollback_unproven
        invocation_completed = False
        token = preserve_uninstall_token
        preserve_uninstall_token = None
        revoke_runtime_install_absence_fact()
        revoke_service_presence_observation()
        if token is None or run_directory is None:
            _driver_fail()
        execution = run_directory.execution_copy
        if execution is None:
            _driver_fail()
        preserve_request = (
            str(run_directory.path / execution.name),
            "--usagehub-uninstall",
        )
        delete_request = preserve_request + ("--delete-data",)
        if (
            type(argv) is not tuple
            or any(type(value) is not str for value in argv)
            or type(timeout) is not float
            or timeout != 180.0
        ):
            _driver_fail()
        if argv == preserve_request:
            expected_generation = 1
        elif argv == delete_request:
            expected_generation = 2
        else:
            _driver_fail()
        if (
            type(token) is not tuple
            or not token
            or type(token[0]) is not int
            or token[0] != expected_generation
        ):
            _driver_fail()
        current_runtime_authority()
        try:
            configured_xdg_config = os.environ.get("XDG_CONFIG_HOME")
            current_uid = os.getuid()
        except Exception:
            _fail("driver_unavailable")
        if configured_xdg_config not in {None, ""} or profile_home is None:
            _fail("driver_unavailable")
        child_environment = {
            "HOME": str(profile_home),
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "XDG_RUNTIME_DIR": f"/run/user/{current_uid}",
        }
        if profile_xdg_binding is not None:
            child_environment["XDG_DATA_HOME"] = profile_xdg_binding
        descriptor, held_executable = run_directory.prepare_preserve_uninstall(token)
        try:
            from openusage_bar.bounded_process import run_bounded

            completed = run_bounded(
                (held_executable, *argv[1:]),
                timeout=180.0,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                env=child_environment,
                pass_fds=(descriptor,),
            )
            if type(completed.returncode) is not int or completed.returncode != 0:
                _driver_fail()
            invocation_completed = True
            run_directory.prepare_preserve_uninstall(token)
            from openusage_bar.platform_services import (
                LinuxCollectorServiceAbsenceState,
                read_current_user_collector_service_absence_state,
            )

            service_absence_before = (
                read_current_user_collector_service_absence_state()
            )
            if profile_home is None:
                _driver_fail()

            def prove_product_paths_absent() -> None:
                try:
                    configured_xdg_config_after = os.environ.get(
                        "XDG_CONFIG_HOME"
                    )
                except Exception:
                    _fail("driver_unavailable")
                if configured_xdg_config_after not in {None, ""}:
                    _fail("driver_unavailable")
                if expected_generation == 1:
                    prove_authoritative_path_absence(
                        anchor=profile_home,
                        relative_components=(
                            ".local",
                            "state",
                            "openusage-bar",
                        ),
                        root_missing=False,
                        entry_names=("openusage.sock",),
                    )
                else:
                    prove_authoritative_path_absence(
                        anchor=profile_home,
                        relative_components=(
                            ".local",
                            "state",
                            "openusage-bar",
                        ),
                        root_missing=True,
                        entry_names=(),
                    )
                    prove_authoritative_path_absence(
                        anchor=profile_home,
                        relative_components=(
                            ".config",
                            "openusage-bar",
                        ),
                        root_missing=True,
                        entry_names=(),
                    )

            prove_product_paths_absent()
            (
                runtime_trusted_root,
                runtime_parts,
                _runtime_root,
            ) = current_runtime_authority()
            if (
                profile_projection is None
                or package_projection is None
                or _runtime_root != profile_projection.runtime_root
                or package_projection.collector.parent != _runtime_root
            ):
                _driver_fail()
            prove_authoritative_path_absence(
                anchor=runtime_trusted_root,
                relative_components=runtime_parts,
                root_missing=True,
                entry_names=(),
            )
            service_absence_after = (
                read_current_user_collector_service_absence_state()
            )
            if (
                type(service_absence_before)
                is not LinuxCollectorServiceAbsenceState
                or type(service_absence_after)
                is not LinuxCollectorServiceAbsenceState
                or service_absence_after != service_absence_before
            ):
                _driver_fail()
            prove_product_paths_absent()
            (
                final_runtime_trusted_root,
                final_runtime_parts,
                final_runtime_root,
            ) = current_runtime_authority()
            if (
                final_runtime_root != _runtime_root
                or final_runtime_trusted_root != runtime_trusted_root
                or final_runtime_parts != runtime_parts
            ):
                _driver_fail()
            prove_authoritative_path_absence(
                anchor=final_runtime_trusted_root,
                relative_components=final_runtime_parts,
                root_missing=True,
                entry_names=(),
            )
            run_directory.prepare_preserve_uninstall(token)
            product_rollback_unproven = False
        except LifecycleEvidenceError:
            if invocation_completed:
                _driver_fail()
            raise
        except Exception:
            _driver_fail()
        return NativeProcessResult(0, b"", b"", False)

    dependencies = NativeLifecycleDependencies(
        make_run_directory=make_run_directory,
        inspect_path=inspect_path,
        copy_file=copy_file,
        set_file_mode=set_file_mode,
        remove_path=remove_path,
        start_process=start_process,
        run_process=run_process,
        stop_process=stop_process,
        read_registry_value=unavailable,
        profile_paths=profile_paths,
        package_paths=package_paths,
        inspect_service=inspect_service,
        inspect_listener=inspect_listener,
        inspect_ledger=inspect_ledger,
        network_events=unavailable,
        credential_events=unavailable,
        monotonic=monotonic,
        wait=wait,
    )
    try:
        object.__setattr__(dependencies, "_owns_run_directory_cleanup", True)
        object.__setattr__(
            dependencies,
            "_mark_process_rollback_unproven",
            mark_process_rollback_unproven,
        )
        object.__setattr__(
            dependencies,
            "_mark_process_rollback_proven",
            mark_process_rollback_proven,
        )
        object.__setattr__(
            dependencies,
            "_mark_managed_process_rollback_proven",
            mark_managed_process_rollback_proven,
        )
        object.__setattr__(
            dependencies,
            "_consume_linux_managed_generation_fact",
            consume_linux_managed_generation_fact,
        )
        yield dependencies
    finally:
        cleanup_failed = False
        if active_process_lease is not None:
            try:
                _force_stop_linux_execution_lease(active_process_lease)
                active_process_lease = None
            except LifecycleEvidenceError:
                cleanup_failed = True
        if run_directory is not None:
            try:
                if process_rollback_unproven or product_rollback_unproven:
                    run_directory.abandon()
                else:
                    run_directory.cleanup()
            except LifecycleEvidenceError:
                cleanup_failed = True
        if cleanup_failed:
            _driver_fail()


class PlatformLifecycleDriver(Protocol):
    def execute(
        self,
        *,
        platform: str,
        arch: str,
        artifact: Path,
        artifact_sha256: str,
    ) -> dict[str, object]: ...


_WINDOWS_UNINSTALL_REGISTRY_KEY = (
    r"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\com.lune.openusagebar"
)
_PROCESS_TIMEOUT_SECONDS = 180.0
_READY_TIMEOUT_SECONDS = 30.0
_READY_WAIT_SECONDS = 0.25


def _driver_fail() -> None:
    _fail("driver_failed")


def _require_path(value: object) -> Path:
    if not _valid_native_path(value):
        _driver_fail()
    assert isinstance(value, Path)
    return value


def _require_process_success(value: object) -> None:
    if (
        not isinstance(value, NativeProcessResult)
        or value.timed_out
        or value.returncode != 0
    ):
        _driver_fail()


def _require_file(
    value: object,
    *,
    sha256: str | None = None,
    mode: int | None = None,
    executable: bool = False,
) -> NativePathState:
    if (
        not isinstance(value, NativePathState)
        or not value.exists
        or value.kind != "file"
        or value.size_bytes <= 0
        or value.sha256 is None
        or not value.file_id
        or (sha256 is not None and value.sha256 != sha256)
        or (mode is not None and value.mode != mode)
        or (executable and value.mode & 0o111 == 0)
    ):
        _driver_fail()
    return value


def _require_missing(value: object) -> None:
    if (
        not isinstance(value, NativePathState)
        or value.exists
        or value.kind != "missing"
        or value.size_bytes != 0
        or value.sha256 is not None
        or value.mode != 0
        or value.file_id is not None
    ):
        _driver_fail()


def _require_listener(
    value: object,
    *,
    active: bool,
    authenticated_ready: bool,
) -> NativeListenerState:
    if (
        not isinstance(value, NativeListenerState)
        or value.active is not active
        or value.authenticated_ready is not authenticated_ready
    ):
        _driver_fail()
    return value


def _require_service_absent(value: object) -> None:
    if (
        not isinstance(value, NativeServiceState)
        or value.registered
        or value.active
        or value.command is not None
    ):
        _driver_fail()


def _require_ledger_present(value: object) -> NativeLedgerState:
    if (
        not isinstance(value, NativeLedgerState)
        or not value.exists
        or not value.file_id
        or value.size_bytes <= 0
        or value.sha256 is None
        or not value.integrity_ok
        or value.revision <= 0
    ):
        _driver_fail()
    return value


def _require_ledger_absent(value: object) -> None:
    if (
        not isinstance(value, NativeLedgerState)
        or value.exists
        or value.file_id is not None
        or value.size_bytes != 0
        or value.sha256 is not None
        or value.integrity_ok
        or value.revision != 0
    ):
        _driver_fail()


def _require_profile_paths(value: object) -> NativeProfilePaths:
    if not isinstance(value, NativeProfilePaths):
        _driver_fail()
    return value


def _require_package_paths(
    value: object,
    *,
    platform: str,
    profile_paths: NativeProfilePaths,
) -> NativePackagePaths:
    if not isinstance(value, NativePackagePaths):
        _driver_fail()
    if platform == "win":
        if value.app is None or value.uninstaller is None:
            _driver_fail()
    elif platform == "linux":
        if (
            value.install_root != profile_paths.runtime_root
            or value.app is not None
            or value.uninstaller is not None
            or value.collector
            != profile_paths.runtime_root / "openusage-collector"
        ):
            _driver_fail()
    else:
        _driver_fail()
    return value


def _require_fresh_baseline(
    dependencies: NativeLifecycleDependencies,
    *,
    platform: str,
    profile_paths: NativeProfilePaths,
    package_paths: NativePackagePaths,
    execution_copy: Path,
    sentinel: Path,
) -> None:
    _require_service_absent(dependencies.inspect_service(platform))
    _require_listener(
        dependencies.inspect_listener(platform, "local"),
        active=False,
        authenticated_ready=False,
    )
    _require_listener(
        dependencies.inspect_listener(
            platform,
            "gateway_default_endpoint" if platform == "linux" else "gateway",
        ),
        active=False,
        authenticated_ready=False,
    )
    _require_ledger_absent(dependencies.inspect_ledger(platform))
    if platform == "linux":
        baseline_paths = (
            ("fresh_state_root", profile_paths.state_root),
            ("fresh_config_root", profile_paths.config_root),
            ("fresh_runtime_root", profile_paths.runtime_root),
            ("fresh_install_root", package_paths.install_root),
            ("fresh_task_definition", profile_paths.task_definition),
        )
    else:
        baseline_paths = (
            ("fresh_state_root", profile_paths.state_root),
            ("fresh_config_root", profile_paths.config_root),
            ("fresh_runtime_root", profile_paths.runtime_root),
            ("fresh_task_definition", profile_paths.task_definition),
            ("fresh_install_root", package_paths.install_root),
        )
    for purpose, path in baseline_paths:
        _require_missing(dependencies.inspect_path(purpose, path))
    if platform == "win":
        assert package_paths.app is not None
        assert package_paths.uninstaller is not None
        package_facts = (
            ("fresh_installed_app", package_paths.app),
            ("fresh_installed_uninstaller", package_paths.uninstaller),
            ("fresh_installed_collector", package_paths.collector),
        )
    else:
        package_facts = (("fresh_stable_collector", package_paths.collector),)
    for purpose, path in package_facts:
        _require_missing(dependencies.inspect_path(purpose, path))
    _require_missing(
        dependencies.inspect_path("fresh_execution_copy", execution_copy)
    )
    _require_missing(dependencies.inspect_path("fresh_sentinel", sentinel))


def _windows_registry_paths(
    dependencies: NativeLifecycleDependencies,
    *,
    package_paths: NativePackagePaths,
) -> tuple[Path, Path, Path, Path]:
    try:
        install_root = _require_path(Path(dependencies.read_registry_value(
            _WINDOWS_UNINSTALL_REGISTRY_KEY,
            "InstallLocation",
        )))
        uninstaller = _require_path(Path(dependencies.read_registry_value(
            _WINDOWS_UNINSTALL_REGISTRY_KEY,
            "UninstallString",
        )))
    except LifecycleEvidenceError:
        raise
    except Exception:
        _driver_fail()
    try:
        uninstaller.relative_to(install_root)
    except ValueError:
        _driver_fail()
    installed_app = install_root / "UsageHub.exe"
    installed_collector = (
        install_root / "resources" / "collector" / "openusage-collector.exe"
    )
    if (
        install_root != package_paths.install_root
        or installed_app != package_paths.app
        or uninstaller != package_paths.uninstaller
        or installed_collector != package_paths.collector
    ):
        _driver_fail()
    return install_root, installed_app, uninstaller, installed_collector


def _require_windows_service(
    value: object,
    *,
    collector: Path,
    api_token: Path,
) -> NativeServiceState:
    if not isinstance(value, NativeServiceState):
        _driver_fail()
    command = value.command
    if (
        not value.registered
        or not value.active
        or command is None
        or len(command) != 10
        or command[0] != str(collector)
        or command[1:9]
        != (
            "daemon",
            "--interval",
            "300",
            "--api-transport",
            "tcp",
            "--api-port",
            "17821",
            "--api-token-path",
        )
    ):
        _driver_fail()
    token_path = _require_path(Path(command[9]))
    if token_path != api_token:
        _driver_fail()
    return value


def _wait_for_windows_observer(
    dependencies: NativeLifecycleDependencies,
    *,
    collector: Path,
    api_token: Path,
) -> NativeServiceState:
    try:
        started = dependencies.monotonic()
    except Exception:
        _driver_fail()
    if type(started) not in {int, float} or isinstance(started, bool):
        _driver_fail()
    current = started
    for _ in range(121):
        service = dependencies.inspect_service("win")
        listener = dependencies.inspect_listener("win", "local")
        ready = (
            isinstance(service, NativeServiceState)
            and service.registered
            and service.active
            and isinstance(listener, NativeListenerState)
            and listener.active
            and listener.authenticated_ready
        )
        if ready:
            return _require_windows_service(
                service,
                collector=collector,
                api_token=api_token,
            )
        if not isinstance(service, NativeServiceState) or not isinstance(
            listener, NativeListenerState
        ):
            _driver_fail()
        try:
            dependencies.wait(_READY_WAIT_SECONDS)
            next_value = dependencies.monotonic()
        except Exception:
            _driver_fail()
        if (
            type(next_value) not in {int, float}
            or isinstance(next_value, bool)
            or next_value < current
            or next_value - started > _READY_TIMEOUT_SECONDS
        ):
            _driver_fail()
        current = next_value
    _driver_fail()


def _run_windows_installer(
    dependencies: NativeLifecycleDependencies,
    argv: tuple[str, ...],
) -> None:
    try:
        result = dependencies.run_process(argv, _PROCESS_TIMEOUT_SECONDS)
    except Exception:
        _driver_fail()
    _require_process_success(result)


def _windows_observe_install(
    dependencies: NativeLifecycleDependencies,
    *,
    installed_app: Path,
    uninstaller: Path,
    installed_collector: Path,
    profile_paths: NativeProfilePaths,
) -> tuple[object, NativeLedgerState, tuple[Path, ...]]:
    try:
        handle = dependencies.start_process((str(installed_app),))
    except Exception:
        _driver_fail()
    if handle is None:
        _driver_fail()
    try:
        service = _wait_for_windows_observer(
            dependencies,
            collector=installed_collector,
            api_token=profile_paths.runtime_root / "api.token",
        )
        _require_listener(
            dependencies.inspect_listener("win", "gateway"),
            active=False,
            authenticated_ready=False,
        )
        ledger = _require_ledger_present(dependencies.inspect_ledger("win"))
        app_state = _require_file(
            dependencies.inspect_path("installed_app", installed_app),
            executable=True,
        )
        uninstaller_state = _require_file(
            dependencies.inspect_path("installed_uninstaller", uninstaller),
            executable=True,
        )
        collector_state = _require_file(
            dependencies.inspect_path("installed_collector", installed_collector),
            executable=True,
        )
        roots = (
            profile_paths.state_root,
            profile_paths.config_root,
            profile_paths.runtime_root,
            profile_paths.task_definition,
            profile_paths.runtime_root / "gateway.token",
            profile_paths.state_root / "gateway-cache.sqlite3",
            profile_paths.state_root / "gateway-telemetry.sqlite3",
        )
        for purpose, path in zip(
            ("gateway_token", "gateway_cache", "gateway_telemetry"),
            roots[4:],
            strict=True,
        ):
            _require_missing(dependencies.inspect_path(purpose, path))
        del app_state, uninstaller_state
        return handle, ledger, (collector_state, *roots)
    except Exception as error:
        try:
            dependencies.stop_process(handle)
        except Exception:
            pass
        if isinstance(error, LifecycleEvidenceError):
            raise
        _driver_fail()


def _windows_native_lifecycle(
    dependencies: NativeLifecycleDependencies,
    *,
    artifact: Path,
    artifact_sha256: str,
) -> dict[str, object]:
    run_directory: Path | None = None
    active_handle: object | None = None
    try:
        run_directory = _require_path(dependencies.make_run_directory("win", "x64"))
        execution_copy = run_directory / artifact.name
        sentinel = run_directory / "outside-product-sentinel.bin"
        profile_paths = _require_profile_paths(dependencies.profile_paths("win"))
        package_paths = _require_package_paths(
            dependencies.package_paths("win", profile_paths),
            platform="win",
            profile_paths=profile_paths,
        )
        _require_fresh_baseline(
            dependencies,
            platform="win",
            profile_paths=profile_paths,
            package_paths=package_paths,
            execution_copy=execution_copy,
            sentinel=sentinel,
        )
        dependencies.copy_file(artifact, execution_copy)
        dependencies.set_file_mode(execution_copy, 0o700)
        execution_state = _require_file(
            dependencies.inspect_path("execution_copy", execution_copy),
            sha256=artifact_sha256,
            mode=0o700,
        )
        dependencies.copy_file(artifact, sentinel)

        _run_windows_installer(
            dependencies,
            (str(execution_copy), "/S"),
        )
        install_root, installed_app, uninstaller, installed_collector = (
            _windows_registry_paths(dependencies, package_paths=package_paths)
        )
        active_handle, initial_ledger, observed = _windows_observe_install(
            dependencies,
            installed_app=installed_app,
            uninstaller=uninstaller,
            installed_collector=installed_collector,
            profile_paths=profile_paths,
        )
        initial_collector = observed[0]
        roots = observed[1:]
        dependencies.stop_process(active_handle)
        active_handle = None

        _run_windows_installer(dependencies, (str(uninstaller), "/S"))
        _require_service_absent(dependencies.inspect_service("win"))
        _require_listener(
            dependencies.inspect_listener("win", "local"),
            active=False,
            authenticated_ready=False,
        )
        _require_listener(
            dependencies.inspect_listener("win", "gateway"),
            active=False,
            authenticated_ready=False,
        )
        for purpose, path in zip(
            (
                "preserve_gateway_token",
                "preserve_gateway_cache",
                "preserve_gateway_telemetry",
            ),
            roots[4:],
            strict=True,
        ):
            _require_missing(dependencies.inspect_path(purpose, path))
        if dependencies.inspect_ledger("win") != initial_ledger:
            _driver_fail()
        _require_missing(
            dependencies.inspect_path("preserve_installed_app", installed_app)
        )
        _require_missing(
            dependencies.inspect_path("preserve_install_root", install_root)
        )

        _run_windows_installer(dependencies, (str(execution_copy), "/S"))
        second_root, second_app, second_uninstaller, second_collector = (
            _windows_registry_paths(dependencies, package_paths=package_paths)
        )
        if (
            second_root != install_root
            or second_app != installed_app
            or second_uninstaller != uninstaller
            or second_collector != installed_collector
        ):
            _driver_fail()
        active_handle, reinstalled_ledger, second_observed = _windows_observe_install(
            dependencies,
            installed_app=installed_app,
            uninstaller=uninstaller,
            installed_collector=installed_collector,
            profile_paths=profile_paths,
        )
        if reinstalled_ledger != initial_ledger:
            _driver_fail()
        second_collector_state = second_observed[0]
        if (
            not isinstance(initial_collector, NativePathState)
            or not isinstance(second_collector_state, NativePathState)
            or initial_collector.size_bytes != second_collector_state.size_bytes
            or initial_collector.sha256 != second_collector_state.sha256
            or initial_collector.mode != second_collector_state.mode
        ):
            _driver_fail()
        if second_observed[1:] != roots:
            _driver_fail()
        dependencies.stop_process(active_handle)
        active_handle = None

        _run_windows_installer(
            dependencies,
            (str(uninstaller), "/S", "--delete-app-data"),
        )
        _require_service_absent(dependencies.inspect_service("win"))
        _require_listener(
            dependencies.inspect_listener("win", "local"),
            active=False,
            authenticated_ready=False,
        )
        _require_listener(
            dependencies.inspect_listener("win", "gateway"),
            active=False,
            authenticated_ready=False,
        )
        _require_ledger_absent(dependencies.inspect_ledger("win"))
        _require_missing(
            dependencies.inspect_path("delete_installed_app", installed_app)
        )
        _require_missing(
            dependencies.inspect_path("delete_install_root", install_root)
        )
        for purpose, path in zip(
            ("state_root", "config_root", "runtime_root", "task_definition"),
            roots[:4],
            strict=True,
        ):
            _require_missing(dependencies.inspect_path(purpose, path))
        sentinel_state = _require_file(
            dependencies.inspect_path("sentinel", sentinel),
            sha256=artifact_sha256,
            mode=0o600,
        )
        if sentinel_state.size_bytes != execution_state.size_bytes:
            _driver_fail()
        if dependencies.network_events() != () or dependencies.credential_events() != ():
            _driver_fail()
        return {
            "checks": {name: "passed" for name in CHECK_NAMES},
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
    except LifecycleEvidenceError:
        raise
    except Exception:
        _driver_fail()
    finally:
        primary_failed = sys.exc_info()[0] is not None
        cleanup_failed = False
        if active_handle is not None:
            try:
                dependencies.stop_process(active_handle)
            except Exception:
                cleanup_failed = True
        if run_directory is not None:
            try:
                dependencies.remove_path(run_directory)
            except Exception:
                cleanup_failed = True
        if cleanup_failed and not primary_failed:
            _driver_fail()


def _require_linux_service(
    value: object,
    *,
    collector: Path,
    api_socket: Path,
) -> NativeServiceState:
    if not isinstance(value, NativeServiceState):
        _driver_fail()
    if (
        not value.registered
        or not value.active
        or value.command
        != (
            str(collector),
            "daemon",
            "--interval",
            "300",
            "--api-transport",
            "unix",
            "--api-socket",
            str(api_socket),
        )
    ):
        _driver_fail()
    return value


def _wait_for_linux_observer(
    dependencies: NativeLifecycleDependencies,
    *,
    collector: Path,
    api_socket: Path,
) -> NativeServiceState:
    try:
        started = dependencies.monotonic()
    except Exception:
        _driver_fail()
    if type(started) not in {int, float} or isinstance(started, bool):
        _driver_fail()
    current = started
    for _ in range(121):
        service = dependencies.inspect_service("linux")
        listener = dependencies.inspect_listener("linux", "local")
        ready = (
            isinstance(service, NativeServiceState)
            and service.registered
            and service.active
            and isinstance(listener, NativeListenerState)
            and listener.active
            and listener.authenticated_ready
        )
        if ready:
            return _require_linux_service(
                service,
                collector=collector,
                api_socket=api_socket,
            )
        if not isinstance(service, NativeServiceState) or not isinstance(
            listener, NativeListenerState
        ):
            _driver_fail()
        try:
            dependencies.wait(_READY_WAIT_SECONDS)
            next_value = dependencies.monotonic()
        except Exception:
            _driver_fail()
        if (
            type(next_value) not in {int, float}
            or isinstance(next_value, bool)
            or next_value < current
            or next_value - started > _READY_TIMEOUT_SECONDS
        ):
            _driver_fail()
        current = next_value
    _driver_fail()


def _start_linux_install(
    dependencies: NativeLifecycleDependencies,
    *,
    execution_copy: Path,
) -> object:
    try:
        handle = dependencies.start_process((str(execution_copy),))
    except Exception:
        _driver_fail()
    if handle is None:
        _driver_fail()
    marker = getattr(dependencies, "_mark_process_rollback_unproven", None)
    if marker is not None:
        try:
            marker()
        except Exception:
            try:
                dependencies.stop_process(handle)
            except Exception:
                pass
            _driver_fail()
    return handle


def _stop_linux_install(
    dependencies: NativeLifecycleDependencies,
    handle: object,
) -> None:
    dependencies.stop_process(handle)


def _mark_linux_process_rollback_proven(
    dependencies: NativeLifecycleDependencies,
) -> None:
    marker = getattr(
        dependencies,
        "_mark_managed_process_rollback_proven",
        None,
    )
    if marker is None:
        marker = getattr(dependencies, "_mark_process_rollback_proven", None)
    if marker is not None:
        try:
            marker()
        except Exception:
            _driver_fail()


def _consume_linux_managed_generation_fact(
    dependencies: NativeLifecycleDependencies,
) -> LinuxManagedObserverGenerationFact:
    consumer = getattr(
        dependencies,
        "_consume_linux_managed_generation_fact",
        None,
    )
    if not callable(consumer):
        _driver_fail()
    try:
        observed = consumer()
    except Exception:
        _driver_fail()
    values = _closed_linux_managed_generation_values(observed)
    if values is None:
        _driver_fail()
    return LinuxManagedObserverGenerationFact(*values)


def _reobserve_linux_managed_generation_after_stop(
    dependencies: NativeLifecycleDependencies,
    *,
    collector: Path,
    api_socket: Path,
    expected: LinuxManagedObserverGenerationFact,
) -> None:
    service = _require_linux_service(
        dependencies.inspect_service("linux"),
        collector=collector,
        api_socket=api_socket,
    )
    listener = dependencies.inspect_listener("linux", "local")
    if (
        type(service) is not NativeServiceState
        or type(listener) is not NativeListenerState
        or listener.active is not True
        or listener.authenticated_ready is not True
    ):
        _driver_fail()
    observed = _consume_linux_managed_generation_fact(dependencies)
    expected_values = _closed_linux_managed_generation_values(expected)
    observed_values = _closed_linux_managed_generation_values(observed)
    if expected_values is None or observed_values != expected_values:
        _driver_fail()
    _mark_linux_process_rollback_proven(dependencies)


def _linux_observe_install(
    dependencies: NativeLifecycleDependencies,
    *,
    profile_paths: NativeProfilePaths,
) -> tuple[
    NativeLedgerState,
    NativePathState,
    LinuxManagedObserverGenerationFact,
]:
    stable_collector = profile_paths.runtime_root / "openusage-collector"
    api_socket = profile_paths.state_root / "openusage.sock"
    _wait_for_linux_observer(
        dependencies,
        collector=stable_collector,
        api_socket=api_socket,
    )
    generation_fact = _consume_linux_managed_generation_fact(dependencies)
    _require_listener(
        dependencies.inspect_listener("linux", "gateway_default_endpoint"),
        active=False,
        authenticated_ready=False,
    )
    ledger = _require_ledger_present(dependencies.inspect_ledger("linux"))
    collector_state = _require_file(
        dependencies.inspect_path("stable_collector", stable_collector),
        executable=True,
    )
    for purpose, path in (
        ("gateway_token", profile_paths.state_root / "gateway.token"),
        ("gateway_cache", profile_paths.state_root / "gateway-cache.sqlite3"),
        (
            "gateway_telemetry",
            profile_paths.state_root / "gateway-telemetry.sqlite3",
        ),
    ):
        _require_missing(dependencies.inspect_path(purpose, path))
    return ledger, collector_state, generation_fact


def _linux_native_lifecycle(
    dependencies: NativeLifecycleDependencies,
    *,
    artifact: Path,
    artifact_sha256: str,
) -> dict[str, object]:
    run_directory: Path | None = None
    active_handle: object | None = None
    try:
        run_directory = _require_path(
            dependencies.make_run_directory("linux", "x64")
        )
        execution_copy = run_directory / artifact.name
        sentinel = run_directory / "outside-product-sentinel.bin"
        profile_paths = _require_profile_paths(dependencies.profile_paths("linux"))
        package_paths = _require_package_paths(
            dependencies.package_paths("linux", profile_paths),
            platform="linux",
            profile_paths=profile_paths,
        )
        _require_fresh_baseline(
            dependencies,
            platform="linux",
            profile_paths=profile_paths,
            package_paths=package_paths,
            execution_copy=execution_copy,
            sentinel=sentinel,
        )
        dependencies.copy_file(artifact, execution_copy)
        dependencies.set_file_mode(execution_copy, 0o700)
        execution_state = _require_file(
            dependencies.inspect_path("execution_copy", execution_copy),
            sha256=artifact_sha256,
            mode=0o700,
        )
        dependencies.copy_file(artifact, sentinel)
        stable_collector = package_paths.collector

        active_handle = _start_linux_install(
            dependencies,
            execution_copy=execution_copy,
        )
        initial_ledger, initial_collector, initial_generation = _linux_observe_install(
            dependencies,
            profile_paths=profile_paths,
        )
        _stop_linux_install(dependencies, active_handle)
        active_handle = None
        _reobserve_linux_managed_generation_after_stop(
            dependencies,
            collector=stable_collector,
            api_socket=profile_paths.state_root / "openusage.sock",
            expected=initial_generation,
        )

        _require_process_success(
            dependencies.run_process(
                (str(execution_copy), "--usagehub-uninstall"),
                _PROCESS_TIMEOUT_SECONDS,
            )
        )
        _require_service_absent(dependencies.inspect_service("linux"))
        _require_listener(
            dependencies.inspect_listener("linux", "local"),
            active=False,
            authenticated_ready=False,
        )
        _require_listener(
            dependencies.inspect_listener("linux", "gateway_default_endpoint"),
            active=False,
            authenticated_ready=False,
        )
        for purpose, path in (
            (
                "preserve_gateway_token",
                profile_paths.state_root / "gateway.token",
            ),
            (
                "preserve_gateway_cache",
                profile_paths.state_root / "gateway-cache.sqlite3",
            ),
            (
                "preserve_gateway_telemetry",
                profile_paths.state_root / "gateway-telemetry.sqlite3",
            ),
        ):
            _require_missing(dependencies.inspect_path(purpose, path))
        if dependencies.inspect_ledger("linux") != initial_ledger:
            _driver_fail()
        _require_missing(
            dependencies.inspect_path(
                "preserve_stable_collector",
                stable_collector,
            )
        )
        _require_missing(
            dependencies.inspect_path(
                "preserve_unit",
                profile_paths.task_definition,
            )
        )
        _require_missing(
            dependencies.inspect_path(
                "preserve_runtime_root",
                profile_paths.runtime_root,
            )
        )
        dependencies.remove_path(execution_copy)
        _require_missing(
            dependencies.inspect_path("preserve_execution_copy", execution_copy)
        )

        dependencies.copy_file(artifact, execution_copy)
        dependencies.set_file_mode(execution_copy, 0o700)
        second_execution = _require_file(
            dependencies.inspect_path("execution_copy", execution_copy),
            sha256=artifact_sha256,
            mode=0o700,
        )
        if second_execution.size_bytes != execution_state.size_bytes:
            _driver_fail()
        active_handle = _start_linux_install(
            dependencies,
            execution_copy=execution_copy,
        )
        second_ledger, second_collector, second_generation = _linux_observe_install(
            dependencies,
            profile_paths=profile_paths,
        )
        if second_ledger != initial_ledger or (
            second_collector.size_bytes,
            second_collector.sha256,
            second_collector.mode,
        ) != (
            initial_collector.size_bytes,
            initial_collector.sha256,
            initial_collector.mode,
        ):
            _driver_fail()
        _stop_linux_install(dependencies, active_handle)
        active_handle = None
        _reobserve_linux_managed_generation_after_stop(
            dependencies,
            collector=stable_collector,
            api_socket=profile_paths.state_root / "openusage.sock",
            expected=second_generation,
        )

        _require_process_success(
            dependencies.run_process(
                (
                    str(execution_copy),
                    "--usagehub-uninstall",
                    "--delete-data",
                ),
                _PROCESS_TIMEOUT_SECONDS,
            )
        )
        _require_service_absent(dependencies.inspect_service("linux"))
        _require_listener(
            dependencies.inspect_listener("linux", "local"),
            active=False,
            authenticated_ready=False,
        )
        _require_listener(
            dependencies.inspect_listener("linux", "gateway_default_endpoint"),
            active=False,
            authenticated_ready=False,
        )
        _require_ledger_absent(dependencies.inspect_ledger("linux"))
        for purpose, path in (
            ("delete_stable_collector", stable_collector),
            ("runtime_root", profile_paths.runtime_root),
            ("task_definition", profile_paths.task_definition),
            ("state_root", profile_paths.state_root),
            ("config_root", profile_paths.config_root),
        ):
            _require_missing(dependencies.inspect_path(purpose, path))
        dependencies.remove_path(execution_copy)
        _require_missing(
            dependencies.inspect_path("delete_execution_copy", execution_copy)
        )
        sentinel_state = _require_file(
            dependencies.inspect_path("sentinel", sentinel),
            sha256=artifact_sha256,
            mode=0o600,
        )
        if sentinel_state.size_bytes != execution_state.size_bytes:
            _driver_fail()
        if dependencies.network_events() != () or dependencies.credential_events() != ():
            _driver_fail()
        return {
            "checks": {name: "passed" for name in CHECK_NAMES},
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
    except LifecycleEvidenceError:
        raise
    except Exception:
        _driver_fail()
    finally:
        cleanup_failed = False
        if active_handle is not None:
            try:
                _stop_linux_install(dependencies, active_handle)
            except Exception:
                cleanup_failed = True
            else:
                active_handle = None
        context_owns_run_directory = (
            getattr(dependencies, "_owns_run_directory_cleanup", False) is True
        )
        if (
            run_directory is not None
            and active_handle is None
            and not context_owns_run_directory
        ):
            try:
                dependencies.remove_path(run_directory)
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            _driver_fail()


class _BuiltInPlatformDriver:
    """Independent native lifecycle driver.

    The current product does not yet expose an honest cross-platform delete
    lifecycle seam.  Refuse evidence rather than converting partial install or
    first-launch observations into nine passing checks.  The product seam is
    intentionally completed before this driver can emit a record.
    """

    def __init__(
        self,
        dependencies: NativeLifecycleDependencies | None = None,
    ) -> None:
        self._dependencies = dependencies

    def execute(
        self,
        *,
        platform: str,
        arch: str,
        artifact: Path,
        artifact_sha256: str,
    ) -> dict[str, object]:
        if self._dependencies is None:
            _fail("driver_unavailable")
        if platform == "win" and arch == "x64":
            return _windows_native_lifecycle(
                self._dependencies,
                artifact=artifact,
                artifact_sha256=artifact_sha256,
            )
        if platform == "linux" and arch == "x64":
            return _linux_native_lifecycle(
                self._dependencies,
                artifact=artifact,
                artifact_sha256=artifact_sha256,
            )
        _fail("driver_unavailable")


class NativeLifecycleExecutor:
    """Host-bound adapter around the independent platform lifecycle driver."""

    execution_class = "external_platform_driver"

    def __init__(
        self,
        *,
        driver: PlatformLifecycleDriver | None = None,
        dependencies: NativeLifecycleDependencies | None = None,
        host_platform: str | None = None,
        host_machine: str | None = None,
    ) -> None:
        if driver is not None and dependencies is not None:
            raise ValueError("driver and dependencies are mutually exclusive")
        if driver is not None:
            self._driver: PlatformLifecycleDriver | None = driver
        elif dependencies is not None:
            self._driver = _BuiltInPlatformDriver(dependencies=dependencies)
        else:
            self._driver = None
        self._host_platform = host_platform if host_platform is not None else sys.platform
        self._host_machine = (
            host_machine
            if host_machine is not None
            else host_platform_module.machine()
        )

    def execute(
        self,
        *,
        platform: str,
        arch: str,
        artifact: Path,
        artifact_sha256: str,
    ) -> dict[str, object]:
        expected_host = "win32" if platform == "win" else "linux"
        machine = self._host_machine.casefold()
        if (
            self._host_platform != expected_host
            and not (
                expected_host == "linux"
                and self._host_platform.startswith("linux")
            )
        ) or arch != "x64" or machine not in {"x86_64", "amd64"}:
            _fail("host_invalid")
        try:
            if self._driver is None:
                with native_lifecycle_dependencies_for_host() as dependencies:
                    if not isinstance(dependencies, NativeLifecycleDependencies):
                        _driver_fail()
                    observations = _BuiltInPlatformDriver(
                        dependencies=dependencies
                    ).execute(
                        platform=platform,
                        arch=arch,
                        artifact=artifact,
                        artifact_sha256=artifact_sha256,
                    )
            else:
                observations = self._driver.execute(
                    platform=platform,
                    arch=arch,
                    artifact=artifact,
                    artifact_sha256=artifact_sha256,
                )
        except LifecycleEvidenceError:
            raise
        except Exception:
            _fail("driver_failed")
        if not _exact_keys(observations, OBSERVATION_KEYS):
            _fail("driver_failed")
        return observations


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _observed_at(clock: Callable[[], datetime]) -> str:
    try:
        value = clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            _fail("record_invalid")
        utc = value.astimezone(timezone.utc)
        return utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except LifecycleEvidenceError:
        raise
    except Exception:
        _fail("record_invalid")


def generate_lifecycle_evidence(
    *,
    platform: str,
    arch: str,
    artifact: str | Path,
    source_commit: str,
    output: str | Path,
    clock: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Execute the native lifecycle, then exclusively write its record."""

    if platform not in SERVICE_MANAGERS or arch != "x64":
        _fail("arguments_invalid")
    if type(source_commit) is not str or SHA1.fullmatch(source_commit) is None:
        _fail("arguments_invalid")
    artifact_path = Path(artifact)
    output_path = Path(output)
    before, before_signature = _inspect_artifact(
        artifact_path, expected_platform=platform
    )
    active_executor = NativeLifecycleExecutor()
    if active_executor.execution_class != "external_platform_driver":
        _fail("execution_not_real")
    try:
        observations = active_executor.execute(
            platform=platform,
            arch=arch,
            artifact=artifact_path,
            artifact_sha256=before["sha256"],
        )
    except LifecycleEvidenceError:
        raise
    except Exception:
        _fail("driver_failed")
    after, after_signature = _inspect_artifact(
        artifact_path, expected_platform=platform
    )
    if after != before or after_signature != before_signature:
        _fail("artifact_changed")
    if not _exact_keys(observations, OBSERVATION_KEYS):
        _fail("record_invalid")
    record: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "object": OBJECT,
        "synthetic": False,
        "releaseEligible": False,
        "observedAt": _observed_at(clock),
        "sourceCommit": source_commit,
        "target": {
            "platform": platform,
            "arch": arch,
            "serviceManager": SERVICE_MANAGERS[platform],
        },
        "artifact": before,
        **observations,
    }
    validate_lifecycle_record(record)
    content = _canonical(record)
    if len(content.encode("ascii")) > MAX_REPORT_BYTES:
        _fail("record_invalid")
    _write_exclusive(output_path, content)
    return record


def _read_canonical_report(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > MAX_REPORT_BYTES
        ):
            _fail("report_unavailable")
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or _stable_metadata_signature(opened)
                != _stable_metadata_signature(metadata)
            ):
                _fail("report_unavailable")
            raw = os.read(descriptor, MAX_REPORT_BYTES + 1)
            finished = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        final_link = path.lstat()
        if (
            len(raw) != metadata.st_size
            or _stable_metadata_signature(finished)
            != _stable_metadata_signature(opened)
            or stat.S_ISLNK(final_link.st_mode)
            or not stat.S_ISREG(final_link.st_mode)
            or _stable_metadata_signature(final_link)
            != _stable_metadata_signature(finished)
        ):
            _fail("report_unavailable")
        text = raw.decode("ascii")
        value = json.loads(text)
    except LifecycleEvidenceError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        MemoryError,
        RecursionError,
    ):
        _fail("report_invalid")
    record = validate_lifecycle_record(value)
    if text != _canonical(record):
        _fail("report_not_canonical")
    return record


def verify_lifecycle_evidence(
    *,
    report: str | Path,
    artifact: str | Path,
    expected_source_commit: str,
    expected_platform: str,
    expected_arch: str,
) -> dict[str, Any]:
    """Rehash the final container and bind a canonical lifecycle record."""

    if (
        expected_platform not in SERVICE_MANAGERS
        or expected_arch != "x64"
        or type(expected_source_commit) is not str
        or SHA1.fullmatch(expected_source_commit) is None
    ):
        _fail("arguments_invalid")
    record = _read_canonical_report(Path(report))
    artifact_record = _artifact_record(
        Path(artifact), expected_platform=expected_platform
    )
    if (
        record["sourceCommit"] != expected_source_commit
        or record["target"]
        != {
            "platform": expected_platform,
            "arch": expected_arch,
            "serviceManager": SERVICE_MANAGERS[expected_platform],
        }
        or record["artifact"] != artifact_record
    ):
        _fail("binding_invalid")
    return record


def _parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(prog="native_lifecycle_evidence.py")
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate")
    generate.add_argument("--platform", choices=tuple(SERVICE_MANAGERS), required=True)
    generate.add_argument("--arch", choices=("x64",), required=True)
    generate.add_argument("--artifact", required=True)
    generate.add_argument("--source-commit", required=True)
    generate.add_argument("--output", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--report", required=True)
    verify.add_argument("--artifact", required=True)
    verify.add_argument("--expected-source-commit", required=True)
    verify.add_argument("--expected-platform", choices=tuple(SERVICE_MANAGERS), required=True)
    verify.add_argument("--expected-arch", choices=("x64",), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        if arguments.command == "generate":
            # CLI generation is intentionally not injectable.  Only the
            # built-in independent platform driver may create CI evidence.
            record = generate_lifecycle_evidence(
                platform=arguments.platform,
                arch=arguments.arch,
                artifact=arguments.artifact,
                source_commit=arguments.source_commit,
                output=arguments.output,
            )
            action = "generated"
        else:
            record = verify_lifecycle_evidence(
                report=arguments.report,
                artifact=arguments.artifact,
                expected_source_commit=arguments.expected_source_commit,
                expected_platform=arguments.expected_platform,
                expected_arch=arguments.expected_arch,
            )
            action = "verified"
        print(
            f"native_lifecycle_{action} "
            f"{record['target']['platform']}/{record['target']['arch']} "
            "synthetic=false releaseEligible=false"
        )
        return 0
    except LifecycleEvidenceError as error:
        print(f"native_lifecycle_invalid reason={error}", file=sys.stderr)
        return 1
    except SystemExit:
        raise
    except Exception:
        print("native_lifecycle_invalid reason=operation_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
