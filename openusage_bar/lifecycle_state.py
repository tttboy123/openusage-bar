"""Safe current-user state removal for the native uninstall lifecycle."""

from __future__ import annotations

import os
import shutil
import socket
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


DELETE_CONFIRMATION = "DELETE-LOCAL-USAGEHUB-STATE"
_WINDOWS_PROFILE_FOLDER_ID = "5e6c858f-0e22-4760-9afe-ea3317b67173"
_WINDOWS_LOCAL_APP_DATA_FOLDER_ID = "f1b32785-6fba-4fcf-9d55-7b8e7f157091"


class LifecycleStateError(RuntimeError):
    """A deliberately path-free local-state lifecycle failure."""


def _posix_current_home() -> Path:
    try:
        import pwd

        raw_home = pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        raise LifecycleStateError("state path unavailable") from None
    if not isinstance(raw_home, str) or not raw_home:
        raise LifecycleStateError("state path unavailable")
    return Path(raw_home)


def _windows_known_folder_path(folder_id: str) -> Path:
    try:
        import ctypes

        class GUID(ctypes.Structure):
            _fields_ = (
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8),
            )

        identifier = GUID.from_buffer_copy(uuid.UUID(folder_id).bytes_le)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        get_path = shell32.SHGetKnownFolderPath
        get_path.argtypes = (
            ctypes.POINTER(GUID),
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        )
        get_path.restype = ctypes.c_long
        raw_path = ctypes.c_wchar_p()
        result = get_path(
            ctypes.byref(identifier),
            0,
            None,
            ctypes.byref(raw_path),
        )
        try:
            if result != 0 or not raw_path.value:
                raise LifecycleStateError("state path unavailable")
            return Path(raw_path.value)
        finally:
            if raw_path:
                free_memory = ole32.CoTaskMemFree
                free_memory.argtypes = (ctypes.c_void_p,)
                free_memory.restype = None
                free_memory(ctypes.cast(raw_path, ctypes.c_void_p))
    except LifecycleStateError:
        raise
    except Exception:
        raise LifecycleStateError("state path unavailable") from None


@dataclass(frozen=True)
class StateDeleteResult:
    deleted: bool


@dataclass(frozen=True)
class LifecycleStatePaths:
    """Internally derived current-user paths owned by UsageHub."""

    platform: str
    home: Path
    local_app_data: Path | None = None

    @classmethod
    def for_current_user(
        cls,
        platform: str | None = None,
    ) -> "LifecycleStatePaths":
        active_platform = sys.platform if platform is None else platform
        if active_platform == "win32":
            return cls(
                platform="win32",
                home=_windows_known_folder_path(_WINDOWS_PROFILE_FOLDER_ID),
                local_app_data=_windows_known_folder_path(
                    _WINDOWS_LOCAL_APP_DATA_FOLDER_ID
                ),
            )
        if active_platform.startswith("linux"):
            return cls(platform="linux", home=_posix_current_home())
        raise LifecycleStateError("unsupported platform")

    @property
    def roots(self) -> tuple[Path, ...]:
        roots = (
            self.home / ".local" / "state" / "openusage-bar",
            self.home / ".config" / "openusage-bar",
        )
        if self.platform == "win32":
            assert self.local_app_data is not None
            return roots + (self.local_app_data / "openusage-bar",)
        return roots

    @property
    def auxiliary_files(self) -> tuple[Path, ...]:
        if self.platform == "win32":
            assert self.local_app_data is not None
            return (self.local_app_data / "openusage-bar-task.xml",)
        return ()


def current_user_runtime_is_active(
    paths: LifecycleStatePaths,
    *,
    service_is_active: Callable[[], bool] | None = None,
) -> bool:
    """Return whether the private Local API can currently be reached."""

    if service_is_active is None:
        from .platform_services import service_is_registered

        service_is_active = lambda: service_is_registered(
            platform=paths.platform,
            home=paths.home,
        )
    if service_is_active():
        return True

    if paths.platform == "win32":
        try:
            connection = socket.create_connection(("127.0.0.1", 17821), timeout=0.2)
        except OSError:
            return False
        connection.close()
        return True

    socket_path = paths.roots[0] / "openusage.sock"
    if not socket_path.exists():
        return False
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(0.2)
    try:
        connection.connect(str(socket_path))
    except OSError:
        return False
    finally:
        connection.close()
    return True


def _is_link(path: Path) -> bool:
    junction_check = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(
        junction_check is not None and junction_check(path)
    )


def _entry_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _validate_parent(parent: Path) -> None:
    if (
        not parent.is_absolute()
        or parent == Path(parent.anchor)
        or not _entry_exists(parent)
        or _is_link(parent)
        or not parent.is_dir()
    ):
        raise LifecycleStateError("state path unsafe")


def _validate_descendant(target: Path, parent: Path) -> None:
    if not target.is_absolute() or not parent.is_absolute():
        raise LifecycleStateError("state path unsafe")
    if target == parent or target == Path(target.anchor):
        raise LifecycleStateError("state path unsafe")
    try:
        target.relative_to(parent)
    except ValueError:
        raise LifecycleStateError("state path unsafe") from None

    current = target
    while current != parent:
        if _entry_exists(current) and _is_link(current):
            raise LifecycleStateError("state path unsafe")
        current = current.parent


def _validate_paths(paths: LifecycleStatePaths) -> None:
    if not isinstance(paths, LifecycleStatePaths):
        raise LifecycleStateError("state path unsafe")
    _validate_parent(paths.home)
    if paths.platform not in {"linux", "win32"}:
        raise LifecycleStateError("state path unsafe")
    if paths.platform == "win32" and (
        paths.local_app_data is None or not paths.local_app_data.is_absolute()
    ):
        raise LifecycleStateError("state path unsafe")
    if paths.local_app_data is not None:
        _validate_parent(paths.local_app_data)

    _validate_descendant(paths.roots[0], paths.home)
    _validate_descendant(paths.roots[1], paths.home)
    if paths.platform == "win32":
        assert paths.local_app_data is not None
        _validate_descendant(paths.local_app_data, paths.home)
        _validate_descendant(paths.roots[2], paths.local_app_data)
        _validate_descendant(paths.auxiliary_files[0], paths.local_app_data)


def _validate_target_types(paths: LifecycleStatePaths) -> None:
    for root in paths.roots:
        if _entry_exists(root) and (_is_link(root) or not root.is_dir()):
            raise LifecycleStateError("state path unsafe")
    for auxiliary in paths.auxiliary_files:
        if _entry_exists(auxiliary) and (
            _is_link(auxiliary) or not auxiliary.is_file()
        ):
            raise LifecycleStateError("state path unsafe")


def _path_identity(path: Path) -> tuple[int, int, int] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise LifecycleStateError("state path unsafe") from None
    return metadata.st_dev, metadata.st_ino, metadata.st_mode


def _identity_snapshot(paths: LifecycleStatePaths) -> dict[Path, tuple[int, int, int] | None]:
    observed: dict[Path, tuple[int, int, int] | None] = {}
    for target in (*paths.roots, *paths.auxiliary_files):
        current = target
        while True:
            observed.setdefault(current, _path_identity(current))
            if current == paths.home:
                break
            if current.parent == current:
                raise LifecycleStateError("state path unsafe")
            current = current.parent
    return observed


def _delete_bound_posix_targets(
    paths: LifecycleStatePaths,
    snapshot: dict[Path, tuple[int, int, int] | None],
) -> None:
    opened: list[tuple[Path, bool, int, int | None]] = []
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    try:
        for target, is_directory in (
            *((root, True) for root in paths.roots),
            *((auxiliary, False) for auxiliary in paths.auxiliary_files),
        ):
            expected = snapshot[target]
            if expected is None:
                continue
            descriptor = os.open(target.parent, flags)
            try:
                parent = os.fstat(descriptor)
                expected_parent = snapshot[target.parent]
                if expected_parent is None or (
                    parent.st_dev,
                    parent.st_ino,
                    parent.st_mode,
                ) != expected_parent:
                    raise LifecycleStateError("state path unsafe")
                entry = os.stat(
                    target.name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if (entry.st_dev, entry.st_ino, entry.st_mode) != expected:
                    raise LifecycleStateError("state path unsafe")
                if is_directory != stat.S_ISDIR(entry.st_mode):
                    raise LifecycleStateError("state path unsafe")
                target_descriptor = None
                if is_directory:
                    target_descriptor = os.open(
                        target.name,
                        flags,
                        dir_fd=descriptor,
                    )
                    opened_target = os.fstat(target_descriptor)
                    if (
                        opened_target.st_dev,
                        opened_target.st_ino,
                        opened_target.st_mode,
                    ) != expected:
                        os.close(target_descriptor)
                        raise LifecycleStateError("state path unsafe")
            except Exception:
                os.close(descriptor)
                raise
            opened.append(
                (target, is_directory, descriptor, target_descriptor)
            )

        for target, is_directory, descriptor, target_descriptor in opened:
            if is_directory:
                assert target_descriptor is not None
                _delete_directory_contents_fd(
                    descriptor,
                    target.name,
                    target_descriptor,
                    snapshot[target],
                )
                _require_bound_directory_entry(
                    descriptor,
                    target.name,
                    target_descriptor,
                    snapshot[target],
                )
                os.rmdir(target.name, dir_fd=descriptor)
            else:
                os.unlink(target.name, dir_fd=descriptor)
    except LifecycleStateError:
        raise
    except OSError:
        raise LifecycleStateError("state delete failed") from None
    finally:
        for _, _, descriptor, target_descriptor in opened:
            if target_descriptor is not None:
                os.close(target_descriptor)
            os.close(descriptor)


def _require_bound_directory_entry(
    parent_descriptor: int,
    name: str,
    directory_descriptor: int,
    expected: tuple[int, int, int] | None,
) -> None:
    if expected is None:
        raise LifecycleStateError("state path unsafe")
    try:
        entry = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        opened = os.fstat(directory_descriptor)
    except OSError:
        raise LifecycleStateError("state path unsafe") from None
    if (
        (entry.st_dev, entry.st_ino, entry.st_mode) != expected
        or (opened.st_dev, opened.st_ino, opened.st_mode) != expected
        or not stat.S_ISDIR(entry.st_mode)
    ):
        raise LifecycleStateError("state path unsafe")


def _delete_directory_contents_fd(
    parent_descriptor: int,
    target_name: str,
    target_descriptor: int,
    expected: tuple[int, int, int] | None,
) -> None:
    def require_canonical_target() -> None:
        _require_bound_directory_entry(
            parent_descriptor,
            target_name,
            target_descriptor,
            expected,
        )

    def recurse(directory_descriptor: int) -> None:
        try:
            entries = list(os.scandir(directory_descriptor))
        except OSError:
            raise LifecycleStateError("state delete failed") from None
        for entry in entries:
            require_canonical_target()
            try:
                metadata = os.stat(
                    entry.name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if stat.S_ISDIR(metadata.st_mode):
                    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
                        os, "O_NOFOLLOW", 0
                    )
                    child_descriptor = os.open(
                        entry.name,
                        flags,
                        dir_fd=directory_descriptor,
                    )
                    try:
                        child = os.fstat(child_descriptor)
                        child_identity = (
                            child.st_dev,
                            child.st_ino,
                            child.st_mode,
                        )
                        if child_identity != (
                            metadata.st_dev,
                            metadata.st_ino,
                            metadata.st_mode,
                        ):
                            raise LifecycleStateError("state path unsafe")
                        recurse(child_descriptor)
                        require_canonical_target()
                        current = os.stat(
                            entry.name,
                            dir_fd=directory_descriptor,
                            follow_symlinks=False,
                        )
                        if (
                            current.st_dev,
                            current.st_ino,
                            current.st_mode,
                        ) != child_identity:
                            raise LifecycleStateError("state path unsafe")
                        os.rmdir(entry.name, dir_fd=directory_descriptor)
                    finally:
                        os.close(child_descriptor)
                else:
                    require_canonical_target()
                    os.unlink(entry.name, dir_fd=directory_descriptor)
            except LifecycleStateError:
                raise
            except OSError:
                raise LifecycleStateError("state delete failed") from None

    require_canonical_target()
    recurse(target_descriptor)


def _delete_revalidated_targets(
    paths: LifecycleStatePaths,
    snapshot: dict[Path, tuple[int, int, int] | None],
) -> None:
    if _identity_snapshot(paths) != snapshot:
        raise LifecycleStateError("state path unsafe")
    if os.name == "posix" and shutil.rmtree.avoids_symlink_attacks:
        _delete_bound_posix_targets(paths, snapshot)
        return

    def target_identity_is_stable(target: Path) -> bool:
        current = target
        while True:
            if _path_identity(current) != snapshot.get(current):
                return False
            if current == paths.home:
                return True
            if current.parent == current:
                return False
            current = current.parent

    try:
        for root in paths.roots:
            if snapshot[root] is not None:
                if not target_identity_is_stable(root):
                    raise LifecycleStateError("state path unsafe")
                shutil.rmtree(root)
        for auxiliary in paths.auxiliary_files:
            if snapshot[auxiliary] is not None:
                if not target_identity_is_stable(auxiliary):
                    raise LifecycleStateError("state path unsafe")
                auxiliary.unlink()
    except LifecycleStateError:
        raise
    except OSError:
        raise LifecycleStateError("state delete failed") from None


def delete_local_state(
    paths: LifecycleStatePaths,
    *,
    confirmation: str,
    runtime_is_active: Callable[[], bool],
) -> StateDeleteResult:
    """Delete only the internally mapped UsageHub roots for this user."""

    if confirmation != DELETE_CONFIRMATION or not callable(runtime_is_active):
        raise LifecycleStateError("state delete rejected")
    if os.name == "nt":
        raise LifecycleStateError("state delete unavailable")
    _validate_paths(paths)
    _validate_target_types(paths)
    snapshot = _identity_snapshot(paths)
    try:
        if runtime_is_active():
            raise LifecycleStateError("state runtime active")
    except LifecycleStateError:
        raise
    except Exception:
        raise LifecycleStateError("state runtime unavailable") from None

    _delete_revalidated_targets(paths, snapshot)
    return StateDeleteResult(deleted=True)
