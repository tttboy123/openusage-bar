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
import os
import platform as host_platform_module
import re
import secrets
import stat
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol


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


@dataclass
class _BoundRunDirectory:
    path: Path
    parent_fd: int
    directory_fd: int
    identity: tuple[int, int]

    def cleanup(self) -> None:
        quarantine = f".{self.path.name}.quarantine-{secrets.token_hex(16)}"
        quarantined = False
        cleanup_failed = False
        try:
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


@contextmanager
def native_lifecycle_dependencies_for_host() -> Iterator[NativeLifecycleDependencies]:
    """Own the low-level dependency session for the current native host.

    Only the private Linux x86_64 run-directory resource is active.  Every
    observation callback remains unavailable until its independent host probe
    exists, so this context cannot yet produce lifecycle evidence.
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

    def unavailable(*args: object, **kwargs: object) -> object:
        del args, kwargs
        _fail("driver_unavailable")

    def make_run_directory(platform: object, arch: object) -> Path:
        nonlocal run_directory
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

    dependencies = NativeLifecycleDependencies(
        make_run_directory=make_run_directory,
        inspect_path=unavailable,
        copy_file=unavailable,
        set_file_mode=unavailable,
        remove_path=unavailable,
        start_process=unavailable,
        run_process=unavailable,
        stop_process=unavailable,
        read_registry_value=unavailable,
        profile_paths=unavailable,
        package_paths=unavailable,
        inspect_service=unavailable,
        inspect_listener=unavailable,
        inspect_ledger=unavailable,
        network_events=unavailable,
        credential_events=unavailable,
        monotonic=unavailable,
        wait=unavailable,
    )
    try:
        yield dependencies
    finally:
        if run_directory is not None:
            run_directory.cleanup()


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
        dependencies.inspect_listener(platform, "gateway"),
        active=False,
        authenticated_ready=False,
    )
    _require_ledger_absent(dependencies.inspect_ledger(platform))
    for purpose, path in (
        ("fresh_state_root", profile_paths.state_root),
        ("fresh_config_root", profile_paths.config_root),
        ("fresh_runtime_root", profile_paths.runtime_root),
        ("fresh_task_definition", profile_paths.task_definition),
        ("fresh_install_root", package_paths.install_root),
    ):
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


def _linux_observe_install(
    dependencies: NativeLifecycleDependencies,
    *,
    execution_copy: Path,
    profile_paths: NativeProfilePaths,
) -> tuple[object, NativeLedgerState, NativePathState]:
    stable_collector = profile_paths.runtime_root / "openusage-collector"
    api_socket = profile_paths.state_root / "openusage.sock"
    try:
        handle = dependencies.start_process((str(execution_copy),))
    except Exception:
        _driver_fail()
    if handle is None:
        _driver_fail()
    try:
        _wait_for_linux_observer(
            dependencies,
            collector=stable_collector,
            api_socket=api_socket,
        )
        _require_listener(
            dependencies.inspect_listener("linux", "gateway"),
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
        return handle, ledger, collector_state
    except Exception as error:
        try:
            dependencies.stop_process(handle)
        except Exception:
            pass
        if isinstance(error, LifecycleEvidenceError):
            raise
        _driver_fail()


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

        active_handle, initial_ledger, initial_collector = _linux_observe_install(
            dependencies,
            execution_copy=execution_copy,
            profile_paths=profile_paths,
        )
        dependencies.stop_process(active_handle)
        active_handle = None

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
            dependencies.inspect_listener("linux", "gateway"),
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
        active_handle, second_ledger, second_collector = _linux_observe_install(
            dependencies,
            execution_copy=execution_copy,
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
        dependencies.stop_process(active_handle)
        active_handle = None

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
            dependencies.inspect_listener("linux", "gateway"),
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
