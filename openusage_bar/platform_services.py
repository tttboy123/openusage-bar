"""Cross-platform user-service registration for the headless collector.

The collector daemon can run as a LaunchAgent (macOS), a systemd user unit
(Linux), or a scheduled task (Windows). Rendering is pure and testable;
``install_service`` / ``uninstall_service`` dispatch to the active platform and
surface activation errors without hiding secrets.
"""

from __future__ import annotations

import os
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from xml.sax.saxutils import escape

from .runtime_descriptor import RuntimeDescriptor


COLLECTOR_LABEL = "com.lune.openusagebar.collector"
SYSTEMD_UNIT_NAME = "openusage-bar.service"
WINDOWS_TASK_NAME = "OpenUsageBarCollector"
PLUGIN_LABEL = "com.lune.openusagebar.plugin"
PLUGIN_SYSTEMD_UNIT_NAME = "openusage-bar-plugin.service"
PLUGIN_WINDOWS_TASK_NAME = "OpenUsageBarPlugin"
COLLECTOR_COMMAND_NAME = "openusage-bar"
DEFAULT_API_SOCKET = "~/.local/state/openusage-bar/openusage.sock"
DEFAULT_LOG_PATH = "~/.local/state/openusage-bar/collector.log"
DEFAULT_ERROR_LOG_PATH = "~/.local/state/openusage-bar/collector.err.log"
_PURE_WINDOWS_RENDER_STATE_DIR = PureWindowsPath("C:/openusage-bar/state")
_SYSTEMD_UNSAFE_PATH_CHARACTERS = frozenset("%$'\"\\")


class ServiceCommandError(RuntimeError):
    """A path-free service-manager command failure."""

    def __init__(self, *, returncode: int | None = None) -> None:
        super().__init__("service activation command failed")
        self.returncode = returncode


def _expand(path: str) -> str:
    return os.path.expanduser(path)


def _validated_expanded_path(path: str) -> str:
    if not isinstance(path, str):
        raise ValueError("service path is invalid")
    expanded = _expand(path)
    if (
        not expanded
        or any(
            not character.isprintable()
            for character in expanded
        )
        or any(
            character in _SYSTEMD_UNSAFE_PATH_CHARACTERS
            for character in expanded
        )
    ):
        raise ValueError("service path is invalid")
    return expanded


def _systemd_argument(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )
    return f'"{escaped}"'


def _validated_service_command(
    command: str | None, *, windows: bool = False
) -> str:
    if command is None:
        return COLLECTOR_COMMAND_NAME
    path_type = PureWindowsPath if windows else PurePosixPath
    if (
        not isinstance(command, str)
        or not command
        or len(command) > 4096
        or not path_type(command).is_absolute()
        or ".." in path_type(command).parts
        or any(not character.isprintable() for character in command)
    ):
        raise ValueError("service command is invalid")
    return command


def _windows_runtime_descriptor(
    *,
    allow_cross_host_state: bool = False,
) -> RuntimeDescriptor:
    if os.name == "nt":
        from .lifecycle_state import LifecycleStatePaths

        paths = LifecycleStatePaths.for_current_user(platform="win32")
        assert paths.local_app_data is not None
        return RuntimeDescriptor.for_platform(
            "win32",
            state_dir=PureWindowsPath(str(paths.local_app_data)) / "openusage-bar",
        )
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidate = PureWindowsPath(local_app_data) / "openusage-bar"
        try:
            return RuntimeDescriptor.for_platform("win32", state_dir=candidate)
        except ValueError:
            if (
                allow_cross_host_state
                and sys.platform == "win32"
                and os.name != "nt"
                and PurePosixPath(local_app_data).is_absolute()
            ):
                return RuntimeDescriptor.for_platform(
                    "win32",
                    state_dir=_PURE_WINDOWS_RENDER_STATE_DIR,
                )
            if sys.platform == "win32":
                raise ValueError(
                    "Windows state directory is unavailable"
                ) from None
    elif sys.platform == "win32":
        raise ValueError("Windows state directory is unavailable")
    return RuntimeDescriptor.for_platform(
        "win32",
        state_dir=_PURE_WINDOWS_RENDER_STATE_DIR,
    )


def _windows_service_definition_path() -> Path:
    if os.name == "nt":
        from .lifecycle_state import LifecycleStatePaths

        paths = LifecycleStatePaths.for_current_user(platform="win32")
        return paths.auxiliary_files[0]
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home()
    return base / "openusage-bar-task.xml"


def _windows_task_definition_path() -> Path:
    if os.name != "nt":
        raise ServiceCommandError()
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_windows_directory = kernel32.GetWindowsDirectoryW
        get_windows_directory.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32)
        get_windows_directory.restype = ctypes.c_uint32
        length = get_windows_directory(buffer, len(buffer))
    except Exception as error:
        raise ServiceCommandError() from error
    if length == 0 or length >= len(buffer):
        raise ServiceCommandError()
    return Path(buffer.value) / "System32" / "Tasks" / WINDOWS_TASK_NAME


def _windows_task_is_absent() -> bool:
    try:
        _windows_task_definition_path().lstat()
    except FileNotFoundError:
        return True
    except OSError as error:
        raise ServiceCommandError() from error
    return False


def _run_windows_task_command_allow_missing(command: list[str]) -> bool:
    try:
        _run(command)
    except ServiceCommandError as error:
        if error.returncode == 1 and _windows_task_is_absent():
            return False
        raise
    return True


def _end_windows_task_allow_missing() -> bool:
    try:
        _run(["schtasks", "/End", "/TN", WINDOWS_TASK_NAME])
    except ServiceCommandError as error:
        if error.returncode != 1:
            raise
        return not _windows_task_is_absent()
    return True


def _linux_current_home() -> Path:
    from .lifecycle_state import LifecycleStatePaths

    return LifecycleStatePaths.for_current_user(platform="linux").home


def launchd_plist(
    *,
    interval: int = 300,
    api_socket: str = DEFAULT_API_SOCKET,
    stdout_path: str = DEFAULT_LOG_PATH,
    stderr_path: str = DEFAULT_ERROR_LOG_PATH,
    command: str | None = None,
) -> str:
    """Render a macOS LaunchAgent property list (XML) for the collector."""
    import plistlib

    payload = {
        "Label": COLLECTOR_LABEL,
        "ProgramArguments": [
            _validated_service_command(command),
            "daemon",
            "--interval",
            str(interval),
            "--api-transport",
            "unix",
            "--api-socket",
            _expand(api_socket),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": _expand(stdout_path),
        "StandardErrorPath": _expand(stderr_path),
    }
    return plistlib.dumps(payload).decode("utf-8")


def systemd_unit(
    *,
    interval: int = 300,
    api_socket: str = DEFAULT_API_SOCKET,
    command: str | None = None,
) -> str:
    """Render a systemd user unit for the collector."""
    socket_argument = _systemd_argument(_validated_expanded_path(api_socket))
    service_command = _validated_service_command(command)
    rendered_command = (
        COLLECTOR_COMMAND_NAME
        if command is None
        else _systemd_argument(service_command)
    )
    return "\n".join(
        [
            "[Unit]",
            "Description=UsageHub (formerly OpenUsage Bar) headless collector",
            "After=default.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={rendered_command} daemon --interval {int(interval)} --api-transport unix --api-socket {socket_argument}",
            "Restart=on-failure",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def windows_task_xml(
    *,
    interval_minutes: int = 5,
    command: str | None = None,
    _allow_cross_host_state: bool = False,
) -> str:
    """Render a Windows Task Scheduler task definition (UTF-16) for the collector."""
    minutes = int(interval_minutes)
    if minutes < 1:
        raise ValueError("interval_minutes must be positive")
    descriptor = _windows_runtime_descriptor(
        allow_cross_host_state=_allow_cross_host_state
    )
    service_command = _validated_service_command(command, windows=True)
    arguments = subprocess.list2cmdline(
        [
            "daemon",
            "--interval",
            str(minutes * 60),
            "--api-transport",
            "tcp",
            "--api-port",
            str(descriptor.local_api_port),
            "--api-token-path",
            str(descriptor.local_api_token_path),
        ]
    )
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        f"    <Description>UsageHub (formerly OpenUsage Bar) headless collector</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>\n'
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{escape(service_command)}</Command>\n"
        f"      <Arguments>{escape(arguments)}</Arguments>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def _plugin_command() -> str:
    if getattr(sys, "frozen", False) is True:
        candidate = sys.executable
    else:
        candidate = shutil.which(COLLECTOR_COMMAND_NAME)
    if candidate is None:
        raise RuntimeError("Plugin service executable is unavailable")
    resolved = Path(candidate).resolve(strict=True)
    if not resolved.is_absolute() or not resolved.is_file():
        raise RuntimeError("Plugin service executable is unavailable")
    return str(resolved)


def _validated_plugin_command(command: str | None) -> str:
    candidate = _plugin_command() if command is None else command
    path = Path(candidate)
    if not path.is_absolute() or ".." in path.parts or not path.is_file():
        raise ValueError("Plugin service executable is invalid")
    return str(path.resolve(strict=True))


def plugin_launchd_plist(*, command: str | None = None) -> str:
    """Render the independent Plugin listener LaunchAgent."""
    import plistlib

    return plistlib.dumps({
        "Label": PLUGIN_LABEL,
        "ProgramArguments": [_validated_plugin_command(command), "plugin", "start"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
    }).decode("utf-8")


def plugin_systemd_unit(*, command: str | None = None) -> str:
    """Render the independent Plugin listener systemd user unit."""
    return "\n".join([
        "[Unit]", "Description=UsageHub private Plugin API", "After=default.target", "",
        "[Service]", "Type=simple", f"ExecStart={_systemd_argument(_validated_plugin_command(command))} plugin start",
        "Restart=on-failure", "", "[Install]", "WantedBy=default.target", "",
    ])


def plugin_windows_task_xml(*, command: str | None = None) -> str:
    """Render a least-privilege Windows task for only the Plugin listener."""
    arguments = subprocess.list2cmdline(["plugin", "start"])
    executable = _validated_plugin_command(command)
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo><Description>UsageHub private Plugin API</Description></RegistrationInfo>\n"
        "  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>\n"
        "  <Principals><Principal id=\"Author\"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\n"
        "  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT0S</ExecutionTimeLimit></Settings>\n"
        "  <Actions Context=\"Author\"><Exec>"
        f"<Command>{escape(executable)}</Command><Arguments>{escape(arguments)}</Arguments>"
        "</Exec></Actions>\n</Task>\n"
    )


def render_plugin_current_platform(*, command: str | None = None) -> str:
    if sys.platform == "darwin":
        return plugin_launchd_plist(command=command)
    if sys.platform.startswith("linux"):
        return plugin_systemd_unit(command=command)
    if sys.platform == "win32":
        return plugin_windows_task_xml(command=command)
    raise RuntimeError(f"unsupported platform: {sys.platform}")


def render_current_platform(
    *, interval: int = 300, command: str | None = None
) -> str:
    """Render the service definition for the active platform."""
    if sys.platform == "darwin":
        return launchd_plist(interval=interval, command=command)
    if sys.platform.startswith("linux"):
        return systemd_unit(interval=interval, command=command)
    if sys.platform == "win32":
        return windows_task_xml(
            interval_minutes=max(1, int(interval) // 60),
            command=command,
        )
    raise RuntimeError(f"unsupported platform: {sys.platform}")


def _run(command: list[str]) -> None:
    try:
        completed = subprocess.run(
            command,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ServiceCommandError() from error
    if completed.returncode != 0:
        raise ServiceCommandError(returncode=completed.returncode)


def _service_status(command: list[str]) -> int:
    try:
        completed = subprocess.run(
            command,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ServiceCommandError() from error
    return completed.returncode


def service_is_registered(
    *,
    platform: str | None = None,
    home: Path | None = None,
) -> bool:
    """Probe the current user's canonical collector service definition."""

    active_platform = sys.platform if platform is None else platform
    if active_platform.startswith("linux"):
        if home is None:
            from .lifecycle_state import LifecycleStatePaths

            home = LifecycleStatePaths.for_current_user(platform="linux").home
        unit = home / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
        try:
            unit.lstat()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ServiceCommandError() from error
        else:
            return True
        if shutil.which("systemctl") is None:
            raise ServiceCommandError()
        returncode = _service_status(
            ["systemctl", "--user", "is-active", "--quiet", SYSTEMD_UNIT_NAME]
        )
        if returncode == 0:
            return True
        if returncode in {3, 4}:
            return False
        raise ServiceCommandError(returncode=returncode)
    if active_platform == "win32":
        returncode = _service_status(
            ["schtasks", "/Query", "/TN", WINDOWS_TASK_NAME]
        )
        if returncode == 0:
            return True
        if returncode == 1:
            if _windows_task_is_absent():
                return False
            raise ServiceCommandError(returncode=returncode)
        raise ServiceCommandError(returncode=returncode)
    raise RuntimeError("unsupported platform")


def _collector_command() -> str:
    return shutil.which(COLLECTOR_COMMAND_NAME) or COLLECTOR_COMMAND_NAME


def _service_path_is_link(path: Path) -> bool:
    junction_check = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(
        junction_check is not None and junction_check(path)
    )


def _service_ancestor_paths(target: Path, *, trusted_root: Path) -> tuple[Path, ...]:
    if not trusted_root.is_absolute() or not target.is_absolute():
        raise RuntimeError("service target is unsafe")
    try:
        relative_parent = target.parent.relative_to(trusted_root)
    except ValueError:
        raise RuntimeError("service target is unsafe") from None
    if ".." in relative_parent.parts:
        raise RuntimeError("service target is unsafe")
    paths = [trusted_root]
    current = trusted_root
    for part in relative_parent.parts:
        current = current / part
        paths.append(current)
    return tuple(paths)


def _service_directory_identity(path: Path) -> tuple[int, int]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeError("service target is unsafe") from error
    if _service_path_is_link(path) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("service target is unsafe")
    return metadata.st_dev, metadata.st_ino


def _open_service_directory_posix(
    trusted_root: Path,
    relative_parts: tuple[str, ...],
    *,
    create: bool,
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    expected_root = _service_directory_identity(trusted_root)
    try:
        descriptor = os.open(trusted_root, flags)
    except OSError as error:
        raise RuntimeError("service target is unsafe") from error
    try:
        opened_root = os.fstat(descriptor)
        if (opened_root.st_dev, opened_root.st_ino) != expected_root:
            raise RuntimeError("service target is unsafe")
        for name in relative_parts:
            try:
                metadata = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if not create:
                    raise RuntimeError("service target is unsafe") from None
                os.mkdir(name, mode=0o700, dir_fd=descriptor)
                metadata = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("service target is unsafe")
            child = os.open(name, flags, dir_fd=descriptor)
            opened_child = os.fstat(child)
            if (
                opened_child.st_dev != metadata.st_dev
                or opened_child.st_ino != metadata.st_ino
                or not stat.S_ISDIR(opened_child.st_mode)
            ):
                os.close(child)
                raise RuntimeError("service target is unsafe")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except RuntimeError:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        raise RuntimeError("service target is unsafe") from error


def _prepare_service_parent(target: Path, *, trusted_root: Path) -> None:
    """Validate existing ancestors before creating missing directories.

    On POSIX, each child is opened relative to the already verified parent so
    an ancestor swap cannot redirect creation through a symlink.  Windows uses
    the same validate-before-create ordering; junctions are rejected by the
    shared link check.
    """

    ancestors = _service_ancestor_paths(target, trusted_root=trusted_root)
    relative_parts = target.parent.relative_to(trusted_root).parts
    if os.name != "nt":
        descriptor = _open_service_directory_posix(
            trusted_root,
            relative_parts,
            create=True,
        )
        os.close(descriptor)
        return

    first_missing = len(ancestors)
    for index, path in enumerate(ancestors):
        try:
            _service_directory_identity(path)
        except RuntimeError as error:
            try:
                path.lstat()
            except FileNotFoundError:
                first_missing = index
                break
            except OSError:
                pass
            raise error
    if first_missing == len(ancestors):
        return
    if first_missing == 0:
        raise RuntimeError("service target is unsafe")

    for path in ancestors[first_missing:]:
        try:
            path.mkdir(mode=0o700)
        except OSError as error:
            raise RuntimeError("service target is unsafe") from error
        _service_directory_identity(path)


def _service_ancestor_identity(
    target: Path,
    *,
    trusted_root: Path,
) -> tuple[tuple[int, int], ...]:
    return tuple(
        _service_directory_identity(path)
        for path in _service_ancestor_paths(target, trusted_root=trusted_root)
    )


def _service_file_identity(
    target: Path,
    *,
    trusted_root: Path,
) -> tuple[tuple[tuple[int, int], ...], tuple[int, int]]:
    ancestors = _service_ancestor_identity(target, trusted_root=trusted_root)
    try:
        metadata = target.lstat()
    except OSError as error:
        raise RuntimeError("service target is unsafe") from error
    if _service_path_is_link(target) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("service target is unsafe")
    return ancestors, (metadata.st_dev, metadata.st_ino)


def _unlink_service_file(
    target: Path,
    *,
    trusted_root: Path,
    expected_identity: tuple[tuple[tuple[int, int], ...], tuple[int, int]],
    missing_ok: bool = False,
) -> None:
    expected_ancestors, expected_file = expected_identity
    actual_ancestors = _service_ancestor_identity(
        target,
        trusted_root=trusted_root,
    )
    if actual_ancestors != expected_ancestors:
        raise RuntimeError("service target is unsafe")
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        if missing_ok:
            return
        raise RuntimeError("service target is unsafe") from None
    except OSError as error:
        raise RuntimeError("service target is unsafe") from error
    if (
        _service_path_is_link(target)
        or not stat.S_ISREG(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != expected_file
    ):
        raise RuntimeError("service target is unsafe")

    if os.name != "nt":
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
            os, "O_NOFOLLOW", 0
        )
        try:
            descriptor = os.open(target.parent, flags)
            try:
                parent = os.fstat(descriptor)
                if (parent.st_dev, parent.st_ino) != expected_ancestors[-1]:
                    raise RuntimeError("service target is unsafe")
                opened = os.stat(
                    target.name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino) != expected_file
                ):
                    raise RuntimeError("service target is unsafe")
                os.unlink(target.name, dir_fd=descriptor)
            finally:
                os.close(descriptor)
        except RuntimeError:
            raise
        except OSError as error:
            raise RuntimeError("service target is unsafe") from error
    else:
        try:
            target.unlink()
        except OSError as error:
            raise RuntimeError("service target is unsafe") from error


def _write_service_file_posix(
    target: Path,
    payload: str,
    *,
    encoding: str,
    trusted_root: Path,
) -> None:
    relative_parts = target.parent.relative_to(trusted_root).parts
    parent_descriptor = _open_service_directory_posix(
        trusted_root,
        relative_parts,
        create=False,
    )
    temporary_name = f".{target.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    output_descriptor: int | None = None
    temporary_exists = False
    try:
        try:
            existing = os.stat(
                target.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise RuntimeError("service target is unsafe")
        output_descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        temporary_exists = True
        raw = payload.encode(encoding)
        offset = 0
        while offset < len(raw):
            written = os.write(output_descriptor, raw[offset:])
            if written <= 0:
                raise OSError("short service definition write")
            offset += written
        os.fsync(output_descriptor)
        os.close(output_descriptor)
        output_descriptor = None
        os.replace(
            temporary_name,
            target.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        temporary_exists = False
        installed = os.stat(
            target.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(installed.st_mode):
            raise RuntimeError("service target is unsafe")
    except RuntimeError:
        raise
    except (OSError, UnicodeError) as error:
        raise RuntimeError("service target is unsafe") from error
    finally:
        if output_descriptor is not None:
            os.close(output_descriptor)
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except OSError:
                pass
        os.close(parent_descriptor)


def _write_service_file(
    target: Path,
    payload: str,
    *,
    encoding: str,
    trusted_root: Path | None = None,
) -> None:
    root = target.parent if trusted_root is None else trusted_root
    _prepare_service_parent(target, trusted_root=root)
    if os.name != "nt":
        _write_service_file_posix(
            target,
            payload,
            encoding=encoding,
            trusted_root=root,
        )
        return
    parent_before = target.parent.lstat()
    try:
        existing = target.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as error:
        raise RuntimeError("service target is unsafe") from error
    if existing is not None and (
        _service_path_is_link(target) or not stat.S_ISREG(existing.st_mode)
    ):
        raise RuntimeError("service target is unsafe")
    temporary = target.with_name(
        f".{target.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        raw = payload.encode(encoding)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError("short service definition write")
            offset += written
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    try:
        parent_after = target.parent.lstat()
        if (
            parent_before.st_dev != parent_after.st_dev
            or parent_before.st_ino != parent_after.st_ino
            or _service_path_is_link(target.parent)
            or not stat.S_ISDIR(parent_after.st_mode)
        ):
            raise RuntimeError("service target is unsafe")
        try:
            existing = target.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            _service_path_is_link(target) or not stat.S_ISREG(existing.st_mode)
        ):
            raise RuntimeError("service target is unsafe")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _write_plugin_service_file(
    target: Path,
    payload: str,
    *,
    encoding: str,
    trusted_root: Path,
) -> None:
    _write_service_file(
        target,
        payload,
        encoding=encoding,
        trusted_root=trusted_root,
    )


def _service_file_exists_safely(target: Path, *, trusted_root: Path) -> bool:
    if not trusted_root.is_absolute() or not target.is_absolute():
        raise RuntimeError("service target is unsafe")
    try:
        target.relative_to(trusted_root)
    except ValueError:
        raise RuntimeError("service target is unsafe") from None
    current = target.parent
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise RuntimeError("service target is unsafe") from error
        if _service_path_is_link(current) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("service target is unsafe")
        if current == trusted_root:
            break
        if current.parent == current:
            raise RuntimeError("service target is unsafe")
        current = current.parent
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise RuntimeError("service target is unsafe") from error
    if _service_path_is_link(target) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("service target is unsafe")
    return True


def install_service(*, interval: int = 300, command: str | None = None) -> None:
    """Write and activate the user service for the active platform."""
    if sys.platform == "darwin":
        home = Path.home()
        target = home / "Library" / "LaunchAgents" / f"{COLLECTOR_LABEL}.plist"
        _write_service_file(
            target,
            launchd_plist(interval=interval, command=command),
            encoding="utf-8",
            trusted_root=home,
        )
        definition_identity = _service_file_identity(target, trusted_root=home)
        try:
            _run(["launchctl", "load", "-w", str(target)])
        except Exception as activation_error:
            try:
                _unlink_service_file(
                    target,
                    trusted_root=home,
                    expected_identity=definition_identity,
                    missing_ok=True,
                )
            except Exception as cleanup_error:
                raise cleanup_error from activation_error
            raise
        return
    if sys.platform.startswith("linux"):
        if shutil.which("systemctl") is None:
            raise RuntimeError("systemd user service manager is unavailable")
        home = _linux_current_home()
        target = home / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
        _write_service_file(
            target,
            systemd_unit(
                interval=interval,
                api_socket=str(
                    home / ".local" / "state" / "openusage-bar" / "openusage.sock"
                ),
                command=command,
            ),
            encoding="utf-8",
            trusted_root=home,
        )
        unit_identity = _service_file_identity(target, trusted_root=home)
        try:
            _run(["systemctl", "--user", "daemon-reload"])
            _run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME])
        except Exception as activation_error:
            try:
                _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME])
            except Exception:
                pass
            try:
                _unlink_service_file(
                    target,
                    trusted_root=home,
                    expected_identity=unit_identity,
                    missing_ok=True,
                )
            except Exception as cleanup_error:
                raise cleanup_error from activation_error
            try:
                _run(["systemctl", "--user", "daemon-reload"])
            except Exception:
                pass
            raise
        return
    if sys.platform == "win32":
        xml = windows_task_xml(
            interval_minutes=max(1, int(interval) // 60),
            command=command,
            _allow_cross_host_state=True,
        )
        target = _windows_service_definition_path()
        _write_service_file(
            target,
            xml,
            encoding="utf-16",
            trusted_root=target.parent,
        )
        definition_identity = _service_file_identity(
            target,
            trusted_root=target.parent,
        )
        created = False
        try:
            _run(
                [
                    "schtasks", "/Create", "/TN", WINDOWS_TASK_NAME,
                    "/XML", str(target), "/F",
                ]
            )
            created = True
            _run(["schtasks", "/Run", "/TN", WINDOWS_TASK_NAME])
        except Exception:
            if created:
                try:
                    _run(["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"])
                except Exception:
                    pass
            _unlink_service_file(
                target,
                trusted_root=target.parent,
                expected_identity=definition_identity,
                missing_ok=True,
            )
            raise
        return
    raise RuntimeError(f"unsupported platform: {sys.platform}")


def uninstall_service() -> None:
    """Remove and deactivate the user service for the active platform."""
    if sys.platform == "darwin":
        home = Path.home()
        target = home / "Library" / "LaunchAgents" / f"{COLLECTOR_LABEL}.plist"
        if _service_file_exists_safely(target, trusted_root=home):
            definition_identity = _service_file_identity(target, trusted_root=home)
            _run(["launchctl", "unload", str(target)])
            _unlink_service_file(
                target,
                trusted_root=home,
                expected_identity=definition_identity,
            )
        return
    if sys.platform.startswith("linux"):
        if shutil.which("systemctl") is None:
            raise RuntimeError("systemd user service manager is unavailable")
        home = _linux_current_home()
        target = home / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
        if _service_file_exists_safely(target, trusted_root=home):
            unit_identity = _service_file_identity(target, trusted_root=home)
            _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME])
            _unlink_service_file(
                target,
                trusted_root=home,
                expected_identity=unit_identity,
            )
        _run(["systemctl", "--user", "daemon-reload"])
        return
    if sys.platform == "win32":
        task_present = _end_windows_task_allow_missing()
        if task_present:
            _run_windows_task_command_allow_missing(
                ["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"]
            )
        definition = _windows_service_definition_path()
        if _service_file_exists_safely(
            definition,
            trusted_root=definition.parent,
        ):
            definition.unlink()
        return
    raise RuntimeError(f"unsupported platform: {sys.platform}")


def install_plugin_service() -> None:
    """Install only the independent Plugin listener user service."""
    if sys.platform == "darwin":
        home = Path.home()
        target = home / "Library" / "LaunchAgents" / f"{PLUGIN_LABEL}.plist"
        _write_plugin_service_file(
            target,
            plugin_launchd_plist(command=_plugin_command()),
            encoding="utf-8",
            trusted_root=home,
        )
        definition_identity = _service_file_identity(target, trusted_root=home)
        try:
            _run(["launchctl", "load", "-w", str(target)])
        except Exception as activation_error:
            try:
                _unlink_service_file(
                    target,
                    trusted_root=home,
                    expected_identity=definition_identity,
                    missing_ok=True,
                )
            except Exception as cleanup_error:
                raise cleanup_error from activation_error
            raise
        return
    if sys.platform.startswith("linux"):
        if shutil.which("systemctl") is None:
            raise RuntimeError("systemd user service manager is unavailable")
        home = _linux_current_home()
        target = home / ".config" / "systemd" / "user" / PLUGIN_SYSTEMD_UNIT_NAME
        _write_plugin_service_file(
            target,
            plugin_systemd_unit(command=_plugin_command()),
            encoding="utf-8",
            trusted_root=home,
        )
        definition_identity = _service_file_identity(target, trusted_root=home)
        try:
            _run(["systemctl", "--user", "daemon-reload"])
            _run(["systemctl", "--user", "enable", "--now", PLUGIN_SYSTEMD_UNIT_NAME])
        except Exception as activation_error:
            try:
                _run(
                    [
                        "systemctl",
                        "--user",
                        "disable",
                        "--now",
                        PLUGIN_SYSTEMD_UNIT_NAME,
                    ]
                )
            except Exception:
                pass
            try:
                _unlink_service_file(
                    target,
                    trusted_root=home,
                    expected_identity=definition_identity,
                    missing_ok=True,
                )
            except Exception as cleanup_error:
                raise cleanup_error from activation_error
            try:
                _run(["systemctl", "--user", "daemon-reload"])
            except Exception:
                pass
            raise
        return
    if sys.platform == "win32":
        from .lifecycle_state import LifecycleStatePaths

        paths = LifecycleStatePaths.for_current_user(platform="win32")
        if paths.local_app_data is None:
            raise RuntimeError("Windows state directory is unavailable")
        local_app_data = paths.local_app_data
        target = local_app_data / "openusage-bar-plugin-task.xml"
        _write_plugin_service_file(
            target,
            plugin_windows_task_xml(command=_plugin_command()),
            encoding="utf-16",
            trusted_root=local_app_data,
        )
        definition_identity = _service_file_identity(
            target,
            trusted_root=local_app_data,
        )
        try:
            _run(
                [
                    "schtasks",
                    "/Create",
                    "/TN",
                    PLUGIN_WINDOWS_TASK_NAME,
                    "/XML",
                    str(target),
                    "/F",
                ]
            )
        except Exception as activation_error:
            try:
                _unlink_service_file(
                    target,
                    trusted_root=local_app_data,
                    expected_identity=definition_identity,
                    missing_ok=True,
                )
            except Exception as cleanup_error:
                raise cleanup_error from activation_error
            raise
        return
    raise RuntimeError(f"unsupported platform: {sys.platform}")


def uninstall_plugin_service() -> None:
    """Remove only the independent Plugin listener user service."""
    if sys.platform == "darwin":
        home = Path.home()
        target = home / "Library" / "LaunchAgents" / f"{PLUGIN_LABEL}.plist"
        if _service_file_exists_safely(target, trusted_root=home):
            definition_identity = _service_file_identity(
                target,
                trusted_root=home,
            )
            _run(["launchctl", "unload", str(target)])
            _unlink_service_file(
                target,
                trusted_root=home,
                expected_identity=definition_identity,
            )
        return
    if sys.platform.startswith("linux"):
        if shutil.which("systemctl") is None:
            raise RuntimeError("systemd user service manager is unavailable")
        home = _linux_current_home()
        target = home / ".config" / "systemd" / "user" / PLUGIN_SYSTEMD_UNIT_NAME
        if _service_file_exists_safely(target, trusted_root=home):
            definition_identity = _service_file_identity(
                target,
                trusted_root=home,
            )
            _run(["systemctl", "--user", "disable", "--now", PLUGIN_SYSTEMD_UNIT_NAME])
            _unlink_service_file(
                target,
                trusted_root=home,
                expected_identity=definition_identity,
            )
        _run(["systemctl", "--user", "daemon-reload"])
        return
    if sys.platform == "win32":
        from .lifecycle_state import LifecycleStatePaths

        paths = LifecycleStatePaths.for_current_user(platform="win32")
        if paths.local_app_data is None:
            raise RuntimeError("Windows state directory is unavailable")
        local_app_data = paths.local_app_data
        target = local_app_data / "openusage-bar-plugin-task.xml"
        definition_identity = (
            _service_file_identity(target, trusted_root=local_app_data)
            if _service_file_exists_safely(
                target,
                trusted_root=local_app_data,
            )
            else None
        )
        _run(["schtasks", "/Delete", "/TN", PLUGIN_WINDOWS_TASK_NAME, "/F"])
        if definition_identity is not None:
            _unlink_service_file(
                target,
                trusted_root=local_app_data,
                expected_identity=definition_identity,
            )
        return
    raise RuntimeError(f"unsupported platform: {sys.platform}")
