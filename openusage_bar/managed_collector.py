"""Install the frozen Linux collector at a product-managed stable path.

The desktop process never supplies either the source or destination path.  The
source is the current frozen executable and the destination is derived from the
OS-authoritative current-user profile.  All mutations below are relative to
already-open directory descriptors so a concurrent pathname swap cannot
redirect a copy or removal outside UsageHub's managed runtime directory.
"""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from . import platform_services
from .lifecycle_state import LifecycleStatePaths


_STABLE_NAME = "openusage-collector"
_MAX_INTERVAL_SECONDS = 86_400


class ManagedCollectorError(RuntimeError):
    """A deliberately path-free managed collector lifecycle failure."""


def _fail() -> "None":
    raise ManagedCollectorError("managed collector action failed")


@dataclass(frozen=True)
class _ManagedLocation:
    trusted_root: Path
    runtime_parts: tuple[str, ...]
    stable: Path


@dataclass(frozen=True)
class _FileFacts:
    device: int
    inode: int
    size: int
    mode: int
    modified_ns: int
    changed_ns: int
    sha256: str


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _path_is_safe_absolute(path: Path) -> bool:
    raw = str(path)
    return (
        path.is_absolute()
        and path != Path(path.anchor)
        and ".." not in path.parts
        and 0 < len(raw) <= 4096
        and all(character.isprintable() and ord(character) != 0x7F for character in raw)
    )


def _managed_location() -> _ManagedLocation:
    try:
        paths = LifecycleStatePaths.for_current_user(platform="linux")
    except Exception:
        _fail()
    if (
        not isinstance(paths, LifecycleStatePaths)
        or paths.platform != "linux"
        or not _path_is_safe_absolute(paths.home)
    ):
        _fail()
    configured = os.environ.get("XDG_DATA_HOME")
    if configured:
        data_root = Path(configured)
        if not _path_is_safe_absolute(data_root):
            _fail()
    else:
        data_root = paths.home / ".local" / "share"
    stable = data_root / "usagehub" / "runtime" / _STABLE_NAME
    if not _path_is_safe_absolute(stable):
        _fail()
    if configured:
        trusted_root = data_root
        while not os.path.lexists(trusted_root):
            parent = trusted_root.parent
            if parent == trusted_root:
                break
            trusted_root = parent
    else:
        trusted_root = paths.home
    try:
        runtime_parts = stable.parent.relative_to(trusted_root).parts
    except ValueError:
        _fail()
    if not runtime_parts:
        _fail()
    return _ManagedLocation(
        trusted_root=trusted_root,
        runtime_parts=runtime_parts,
        stable=stable,
    )


def _require_frozen_linux() -> None:
    if not sys.platform.startswith("linux") or getattr(sys, "frozen", False) is not True:
        _fail()


def _open_trusted_root(root: Path) -> int:
    try:
        expected = root.lstat()
        if not stat.S_ISDIR(expected.st_mode) or root.is_symlink():
            _fail()
        descriptor = os.open(root, _directory_flags())
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            os.close(descriptor)
            _fail()
        return descriptor
    except ManagedCollectorError:
        raise
    except OSError:
        _fail()


def _open_child_directory(parent: int, name: str, *, create: bool) -> int | None:
    try:
        metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            return None
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
        except OSError:
            _fail()
        try:
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError:
            _fail()
    except OSError:
        _fail()
    if not stat.S_ISDIR(metadata.st_mode):
        _fail()
    try:
        child = os.open(name, _directory_flags(), dir_fd=parent)
        opened = os.fstat(child)
    except OSError:
        _fail()
    if (
        not stat.S_ISDIR(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
    ):
        os.close(child)
        _fail()
    return child


def _open_runtime(location: _ManagedLocation, *, create: bool) -> int | None:
    descriptor = _open_trusted_root(location.trusted_root)
    try:
        for name in location.runtime_parts:
            child = _open_child_directory(descriptor, name, create=create)
            if child is None:
                os.close(descriptor)
                return None
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _regular_entry_identity(descriptor: int, name: str) -> tuple[int, int] | None:
    try:
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        _fail()
    if not stat.S_ISREG(metadata.st_mode):
        _fail()
    return metadata.st_dev, metadata.st_ino


def _open_frozen_source() -> int:
    source = Path(sys.executable)
    if not _path_is_safe_absolute(source):
        _fail()
    try:
        expected = source.lstat()
        if source.is_symlink() or not stat.S_ISREG(expected.st_mode):
            _fail()
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size <= 0
            or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            os.close(descriptor)
            _fail()
        return descriptor
    except ManagedCollectorError:
        raise
    except OSError:
        _fail()


def _facts_for_open_file(descriptor: int) -> _FileFacts:
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            _fail()
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(descriptor)
    except ManagedCollectorError:
        raise
    except OSError:
        _fail()
    facts = _FileFacts(
        device=after.st_dev,
        inode=after.st_ino,
        size=after.st_size,
        mode=stat.S_IMODE(after.st_mode),
        modified_ns=after.st_mtime_ns,
        changed_ns=after.st_ctime_ns,
        sha256=digest.hexdigest(),
    )
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    ):
        _fail()
    return facts


def _facts_for_regular_entry(descriptor: int, name: str) -> _FileFacts:
    opened: int | None = None
    try:
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            _fail()
        opened = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=descriptor,
        )
        actual = os.fstat(opened)
        if (
            not stat.S_ISREG(actual.st_mode)
            or (actual.st_dev, actual.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            _fail()
        return _facts_for_open_file(opened)
    except ManagedCollectorError:
        raise
    except OSError:
        _fail()
    finally:
        if opened is not None:
            os.close(opened)


def _copy_source_to_temporary(source: int, runtime: int, temporary: str) -> None:
    output: int | None = None
    try:
        output = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o700,
            dir_fd=runtime,
        )
        os.fchmod(output, 0o700)
        os.lseek(source, 0, os.SEEK_SET)
        while True:
            block = os.read(source, 1024 * 1024)
            if not block:
                break
            offset = 0
            while offset < len(block):
                written = os.write(output, block[offset:])
                if written <= 0:
                    raise OSError("short managed collector write")
                offset += written
        os.fsync(output)
    except OSError:
        _fail()
    finally:
        if output is not None:
            os.close(output)


def _same_directory_identity(left: int, right: int) -> bool:
    first = os.fstat(left)
    second = os.fstat(right)
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _canonical_runtime_matches(location: _ManagedLocation, runtime: int) -> bool:
    try:
        current = _open_runtime(location, create=False)
    except ManagedCollectorError:
        return False
    if current is None:
        return False
    try:
        return _same_directory_identity(runtime, current)
    finally:
        os.close(current)


def _unlink_if_identity(
    descriptor: int,
    name: str,
    expected: tuple[int, int],
) -> None:
    actual = _regular_entry_identity(descriptor, name)
    if actual != expected:
        _fail()
    try:
        os.unlink(name, dir_fd=descriptor)
    except OSError:
        _fail()


def _rollback_install(
    runtime: int,
    installed_identity: tuple[int, int] | None,
    backup: str | None,
) -> None:
    if installed_identity is not None:
        _unlink_if_identity(runtime, _STABLE_NAME, installed_identity)
    if backup is not None:
        if _regular_entry_identity(runtime, backup) is None:
            _fail()
        try:
            os.rename(
                backup,
                _STABLE_NAME,
                src_dir_fd=runtime,
                dst_dir_fd=runtime,
            )
        except OSError:
            _fail()


def _remove_temporary(runtime: int, name: str) -> None:
    try:
        metadata = os.stat(name, dir_fd=runtime, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        _fail()
    if not stat.S_ISREG(metadata.st_mode):
        _fail()
    try:
        os.unlink(name, dir_fd=runtime)
    except OSError:
        _fail()


def _remove_empty_runtime(
    location: _ManagedLocation,
    expected_runtime: tuple[int, int],
) -> None:
    parent = _open_trusted_root(location.trusted_root)
    try:
        for name in location.runtime_parts[:-1]:
            child = _open_child_directory(parent, name, create=False)
            if child is None:
                return
            os.close(parent)
            parent = child
        try:
            runtime = os.stat(
                location.runtime_parts[-1],
                dir_fd=parent,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if (
            not stat.S_ISDIR(runtime.st_mode)
            or (runtime.st_dev, runtime.st_ino) != expected_runtime
        ):
            _fail()
        try:
            os.rmdir(location.runtime_parts[-1], dir_fd=parent)
        except OSError as error:
            if error.errno != errno.ENOTEMPTY:
                _fail()
    finally:
        os.close(parent)


def install_managed_collector(*, interval: int = 300) -> None:
    """Copy this frozen collector to its stable path and activate the service."""

    _require_frozen_linux()
    if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= _MAX_INTERVAL_SECONDS:
        _fail()
    location = _managed_location()
    source = _open_frozen_source()
    source_before = _facts_for_open_file(source)
    runtime: int | None = None
    temporary = f".{_STABLE_NAME}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    backup: str | None = None
    installed_identity: tuple[int, int] | None = None
    try:
        runtime = _open_runtime(location, create=True)
        if runtime is None:
            _fail()
        _copy_source_to_temporary(source, runtime, temporary)
        existing = _regular_entry_identity(runtime, _STABLE_NAME)
        if existing is not None:
            backup = f".{_STABLE_NAME}.previous-{secrets.token_hex(8)}"
            os.rename(
                _STABLE_NAME,
                backup,
                src_dir_fd=runtime,
                dst_dir_fd=runtime,
            )
        os.replace(
            temporary,
            _STABLE_NAME,
            src_dir_fd=runtime,
            dst_dir_fd=runtime,
        )
        installed_identity = _regular_entry_identity(runtime, _STABLE_NAME)
        source_after = _facts_for_open_file(source)
        stable_facts = _facts_for_regular_entry(runtime, _STABLE_NAME)
        if (
            installed_identity is None
            or source_after != source_before
            or stable_facts.size != source_before.size
            or stable_facts.sha256 != source_before.sha256
            or stable_facts.mode != 0o700
            or not _canonical_runtime_matches(location, runtime)
        ):
            _fail()
        platform_services.install_service(
            interval=interval,
            command=str(location.stable),
        )
        if not _canonical_runtime_matches(location, runtime):
            try:
                platform_services.uninstall_service()
            except Exception:
                pass
            _fail()
        if backup is not None:
            _remove_temporary(runtime, backup)
            backup = None
    except Exception as error:
        cleanup_failed = False
        if runtime is not None:
            try:
                _remove_temporary(runtime, temporary)
                _rollback_install(runtime, installed_identity, backup)
            except Exception:
                cleanup_failed = True
        if cleanup_failed:
            raise ManagedCollectorError("managed collector action failed") from None
        if isinstance(error, ManagedCollectorError):
            raise error from None
        raise ManagedCollectorError("managed collector action failed") from None
    finally:
        os.close(source)
        if runtime is not None:
            os.close(runtime)


def uninstall_managed_collector() -> None:
    """Deactivate the service and remove only the bound managed copy."""

    _require_frozen_linux()
    location = _managed_location()
    runtime = _open_runtime(location, create=False)
    expected_runtime: tuple[int, int] | None = None
    expected_stable: tuple[int, int] | None = None
    try:
        if runtime is not None:
            metadata = os.fstat(runtime)
            expected_runtime = metadata.st_dev, metadata.st_ino
            expected_stable = _regular_entry_identity(runtime, _STABLE_NAME)
        try:
            platform_services.uninstall_service()
        except Exception:
            _fail()
        if runtime is None:
            return
        if not _canonical_runtime_matches(location, runtime):
            _fail()
        if expected_stable is not None:
            _unlink_if_identity(runtime, _STABLE_NAME, expected_stable)
    except ManagedCollectorError:
        raise
    except Exception:
        _fail()
    finally:
        if runtime is not None:
            os.close(runtime)
    if expected_runtime is not None:
        _remove_empty_runtime(location, expected_runtime)
