"""Cross-platform user-service registration for the headless collector.

The collector daemon can run as a LaunchAgent (macOS), a systemd user unit
(Linux), or a scheduled task (Windows). Rendering is pure and testable;
``install_service`` / ``uninstall_service`` dispatch to the active platform and
surface activation errors without hiding secrets.
"""

from __future__ import annotations

import hashlib
import ctypes
import errno
import os
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import sys
from dataclasses import dataclass
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
_LINUX_SERVICE_FACT_LIMIT = 64 * 1024
_LINUX_SERVICE_QUERY_TIMEOUT_SECONDS = 5
_LINUX_SERVICE_PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "FragmentPath",
    "DropInPaths",
    "NeedDaemonReload",
    "MainPID",
)
_LINUX_SERVICE_ABSENCE_PROPERTIES = (
    *_LINUX_SERVICE_PROPERTIES,
    "ControlPID",
    "Job",
)
_TRUSTED_SYSTEMCTL_PATHS = frozenset({"/usr/bin/systemctl"})
_LINUX_SOL_SOCKET = 1
_LINUX_SO_PEERCRED = 17
_LINUX_SERVICE_ABSENCE_STAGES = frozenset(
    {
        "authority",
        "runtime-peer",
        "manager-provenance",
        "manager-binary-readlink",
        "manager-binary-path-value",
        "manager-binary-metadata",
        "manager-binary-public-identity",
        "systemctl-binding",
        "unit-absence",
        "manager-query",
        "sandwich",
        "cleanup",
    }
)


class ServiceCommandError(RuntimeError):
    """A path-free service-manager command failure."""

    def __init__(
        self,
        *,
        returncode: int | None = None,
        stage: str | None = None,
    ) -> None:
        if stage is not None and stage not in _LINUX_SERVICE_ABSENCE_STAGES:
            raise ValueError("service command stage invalid")
        super().__init__("service activation command failed")
        self.returncode = returncode
        self.stage = stage


@dataclass(frozen=True, repr=False)
class LinuxCollectorServiceState:
    """Closed, current-user systemd collector ownership observation."""

    unit_file_id: str
    unit_size_bytes: int
    unit_sha256: str
    unit_id: str
    load_state: str
    active_state: str
    sub_state: str
    unit_file_state: str
    fragment_path: Path
    drop_in_paths: tuple[Path, ...]
    needs_reload: bool
    main_pid: int
    process_uid: int
    process_start_time_ticks: int
    process_executable: Path
    process_executable_file_id: str
    process_executable_signature_sha256: str
    process_argv_nul: bytes

    def __post_init__(self) -> None:
        if (
            type(self.unit_file_id) is not str
            or not self.unit_file_id
            or type(self.unit_size_bytes) is not int
            or self.unit_size_bytes <= 0
            or type(self.unit_sha256) is not str
            or len(self.unit_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.unit_sha256)
            or type(self.unit_id) is not str
            or not self.unit_id
            or type(self.load_state) is not str
            or not self.load_state
            or type(self.active_state) is not str
            or not self.active_state
            or type(self.sub_state) is not str
            or not self.sub_state
            or type(self.unit_file_state) is not str
            or not self.unit_file_state
            or not isinstance(self.fragment_path, Path)
            or not self.fragment_path.is_absolute()
            or type(self.drop_in_paths) is not tuple
            or any(
                not isinstance(path, Path) or not path.is_absolute()
                for path in self.drop_in_paths
            )
            or type(self.needs_reload) is not bool
            or type(self.main_pid) is not int
            or self.main_pid <= 0
            or type(self.process_uid) is not int
            or self.process_uid < 0
            or type(self.process_start_time_ticks) is not int
            or self.process_start_time_ticks <= 0
            or not isinstance(self.process_executable, Path)
            or not self.process_executable.is_absolute()
            or type(self.process_executable_file_id) is not str
            or not self.process_executable_file_id
            or type(self.process_executable_signature_sha256) is not str
            or len(self.process_executable_signature_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.process_executable_signature_sha256
            )
            or type(self.process_argv_nul) is not bytes
            or not self.process_argv_nul
            or not self.process_argv_nul.endswith(b"\0")
        ):
            raise ValueError("Linux collector service state invalid")


@dataclass(frozen=True, repr=False)
class LinuxCollectorServiceAbsenceState:
    """Closed negative fact reported by one trusted current-user manager."""

    unit_missing: bool
    unit_id: str
    load_state: str
    active_state: str
    sub_state: str
    unit_file_state: str | None
    main_pid: int
    control_pid: int
    job: str | None
    fragment_path: Path | None
    drop_in_paths: tuple[Path, ...]
    needs_reload: bool

    def __post_init__(self) -> None:
        if (
            type(self.unit_missing) is not bool
            or self.unit_missing is not True
            or type(self.unit_id) is not str
            or self.unit_id != SYSTEMD_UNIT_NAME
            or type(self.load_state) is not str
            or self.load_state != "not-found"
            or type(self.active_state) is not str
            or self.active_state != "inactive"
            or type(self.sub_state) is not str
            or self.sub_state != "dead"
            or self.unit_file_state is not None
            or type(self.main_pid) is not int
            or self.main_pid != 0
            or type(self.control_pid) is not int
            or self.control_pid != 0
            or self.job is not None
            or self.fragment_path is not None
            or type(self.drop_in_paths) is not tuple
            or self.drop_in_paths != ()
            or type(self.needs_reload) is not bool
            or self.needs_reload is not False
        ):
            raise ValueError("Linux collector service absence state invalid")


@dataclass(frozen=True)
class _BoundLinuxExecutable:
    parent_descriptor: int
    executable_descriptor: int
    signature: tuple[int, ...]


@dataclass(frozen=True)
class _BoundLinuxManagerPeer:
    directory_descriptor: int
    connection: object
    directory_signature: tuple[int, ...]
    socket_signature: tuple[int, ...]
    pid: int
    uid: int
    gid: int


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


def read_current_user_collector_service_state() -> LinuxCollectorServiceState:
    """Read a verified active Linux collector fact, or fail closed."""

    if not sys.platform.startswith("linux") or os.name == "nt":
        raise ServiceCommandError()
    runtime_descriptor: int | None = None
    systemctl_binding: _BoundLinuxExecutable | None = None
    manager_peer: _BoundLinuxManagerPeer | None = None
    try:
        configured_xdg_home = os.environ.get("XDG_CONFIG_HOME")
        if configured_xdg_home not in {None, ""}:
            raise ServiceCommandError()
        from .lifecycle_state import LifecycleStatePaths

        authority = LifecycleStatePaths.for_current_user(platform="linux")
        home = authority.home
        if not isinstance(home, Path) or not home.is_absolute():
            raise ServiceCommandError()
        unit = home / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
        systemctl = shutil.which("systemctl")
        if systemctl not in _TRUSTED_SYSTEMCTL_PATHS:
            raise ServiceCommandError()
        current_uid = os.getuid()
        runtime_descriptor = _open_linux_user_runtime_directory(current_uid)
        runtime_identity = _linux_file_signature(os.fstat(runtime_descriptor))
        manager_peer = _bind_linux_systemd_private_peer(
            runtime_descriptor, current_uid
        )
        manager_environment = {
            "HOME": str(home),
            "XDG_RUNTIME_DIR": f"/proc/self/fd/{runtime_descriptor}",
            "DBUS_SESSION_BUS_ADDRESS": (
                "unix:path=/proc/self/fd/"
                f"{manager_peer.directory_descriptor}/private"
            ),
            "LC_ALL": "C",
            "LANG": "C",
            "SYSTEMD_COLORS": "0",
            "PAGER": "cat",
        }
        manager_identity_before = _read_linux_process_identity(manager_peer.pid)
        if manager_identity_before[2] != 1:
            raise ServiceCommandError()
        manager_cgroup_before = _read_linux_systemd_manager_cgroup(
            manager_peer.pid, current_uid
        )
        manager_executable_before = _read_linux_systemd_manager_executable(
            manager_peer.pid
        )
        manager_cmdline_before = _read_linux_systemd_manager_cmdline(
            manager_peer.pid
        )
        systemctl_binding = _bind_linux_systemctl_executable(systemctl)
        unit_before = _read_linux_service_unit(home, unit)
        manager_before = _read_linux_service_manager_state(
            systemctl,
            manager_environment,
            (runtime_descriptor, manager_peer.directory_descriptor),
        )
        main_pid = int(manager_before["MainPID"])
        process_identity_before = _read_linux_process_identity(main_pid)
        if process_identity_before[2] != manager_peer.pid:
            raise ServiceCommandError()
        process_cgroup_before = _read_linux_collector_cgroup(
            main_pid, current_uid
        )
        (
            process_executable,
            executable_file_id,
            process_executable_signature,
        ) = _read_linux_process_executable(main_pid)
        process_argv_nul = _read_linux_process_cmdline(main_pid)
        manager_after = _read_linux_service_manager_state(
            systemctl,
            manager_environment,
            (runtime_descriptor, manager_peer.directory_descriptor),
        )
        process_identity_after = _read_linux_process_identity(main_pid)
        process_cgroup_after = _read_linux_collector_cgroup(
            main_pid, current_uid
        )
        (
            process_executable_after,
            executable_file_id_after,
            process_executable_signature_after,
        ) = _read_linux_process_executable(main_pid)
        process_argv_nul_after = _read_linux_process_cmdline(main_pid)
        process_identity_final = _read_linux_process_identity(main_pid)
        unit_after = _read_linux_service_unit(home, unit)
        manager_identity_after = _read_linux_process_identity(manager_peer.pid)
        manager_cgroup_after = _read_linux_systemd_manager_cgroup(
            manager_peer.pid, current_uid
        )
        manager_executable_after = _read_linux_systemd_manager_executable(
            manager_peer.pid
        )
        manager_cmdline_after = _read_linux_systemd_manager_cmdline(
            manager_peer.pid
        )
        if (
            manager_after != manager_before
            or process_identity_after != process_identity_before
            or process_identity_final != process_identity_before
            or process_cgroup_after != process_cgroup_before
            or process_executable_after != process_executable
            or executable_file_id_after != executable_file_id
            or process_executable_signature_after
            != process_executable_signature
            or process_argv_nul_after != process_argv_nul
            or unit_after != unit_before
            or _linux_file_signature(os.fstat(runtime_descriptor))
            != runtime_identity
            or manager_identity_after != manager_identity_before
            or manager_cgroup_after != manager_cgroup_before
            or manager_executable_after != manager_executable_before
            or manager_cmdline_after != manager_cmdline_before
        ):
            raise ServiceCommandError()
        _revalidate_linux_systemd_private_peer(manager_peer, current_uid)
        _revalidate_linux_systemctl_executable(systemctl_binding)
        return LinuxCollectorServiceState(
            unit_file_id=unit_before[0],
            unit_size_bytes=len(unit_before[1]),
            unit_sha256=hashlib.sha256(unit_before[1]).hexdigest(),
            unit_id=manager_before["Id"],
            load_state=manager_before["LoadState"],
            active_state=manager_before["ActiveState"],
            sub_state=manager_before["SubState"],
            unit_file_state=manager_before["UnitFileState"],
            fragment_path=Path(manager_before["FragmentPath"]),
            drop_in_paths=(),
            needs_reload=manager_before["NeedDaemonReload"] == "yes",
            main_pid=main_pid,
            process_uid=process_identity_before[0],
            process_start_time_ticks=process_identity_before[1],
            process_executable=process_executable,
            process_executable_file_id=executable_file_id,
            process_executable_signature_sha256=hashlib.sha256(
                struct.pack(">9Q", *process_executable_signature)
            ).hexdigest(),
            process_argv_nul=process_argv_nul,
        )
    except ServiceCommandError:
        raise
    except Exception as error:
        raise ServiceCommandError() from error
    finally:
        close_failed = False
        if manager_peer is not None:
            try:
                manager_peer.connection.close()
            except Exception:
                close_failed = True
            try:
                os.close(manager_peer.directory_descriptor)
            except OSError:
                close_failed = True
        if systemctl_binding is not None:
            for descriptor in (
                systemctl_binding.executable_descriptor,
                systemctl_binding.parent_descriptor,
            ):
                try:
                    os.close(descriptor)
                except OSError:
                    close_failed = True
        if runtime_descriptor is not None:
            try:
                os.close(runtime_descriptor)
            except OSError:
                close_failed = True
        if close_failed:
            raise ServiceCommandError()


def read_current_user_collector_service_absence_state(
) -> LinuxCollectorServiceAbsenceState:
    """Read one stable negative fact from the trusted Linux user manager."""

    if not sys.platform.startswith("linux") or os.name == "nt":
        raise ServiceCommandError(stage="authority")
    runtime_descriptor: int | None = None
    systemctl_binding: _BoundLinuxExecutable | None = None
    manager_peer: _BoundLinuxManagerPeer | None = None
    stage = "authority"
    try:
        configured_xdg_home = os.environ.get("XDG_CONFIG_HOME")
        if configured_xdg_home not in {None, ""}:
            raise ServiceCommandError()
        from .lifecycle_state import LifecycleStatePaths

        authority = LifecycleStatePaths.for_current_user(platform="linux")
        home = authority.home
        if not isinstance(home, Path) or not home.is_absolute():
            raise ServiceCommandError()
        unit = home / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
        systemctl = shutil.which("systemctl")
        if systemctl not in _TRUSTED_SYSTEMCTL_PATHS:
            raise ServiceCommandError()
        current_uid = os.getuid()
        stage = "runtime-peer"
        runtime_descriptor = _open_linux_user_runtime_directory(current_uid)
        runtime_identity = _linux_file_signature(os.fstat(runtime_descriptor))
        manager_peer = _bind_linux_systemd_private_peer(
            runtime_descriptor, current_uid
        )
        manager_environment = {
            "HOME": str(home),
            "XDG_RUNTIME_DIR": f"/proc/self/fd/{runtime_descriptor}",
            "DBUS_SESSION_BUS_ADDRESS": (
                "unix:path=/proc/self/fd/"
                f"{manager_peer.directory_descriptor}/private"
            ),
            "LC_ALL": "C",
            "LANG": "C",
            "SYSTEMD_COLORS": "0",
            "PAGER": "cat",
        }
        stage = "manager-provenance"
        manager_identity_before = _read_linux_process_identity(manager_peer.pid)
        if manager_identity_before[2] != 1:
            raise ServiceCommandError()
        manager_cgroup_before = _read_linux_systemd_manager_cgroup(
            manager_peer.pid, current_uid
        )
        stage = "manager-binary-readlink"
        manager_executable_before = _read_linux_systemd_manager_executable(
            manager_peer.pid
        )
        stage = "manager-provenance"
        manager_cmdline_before = _read_linux_systemd_manager_cmdline(
            manager_peer.pid
        )
        stage = "systemctl-binding"
        systemctl_binding = _bind_linux_systemctl_executable(systemctl)
        stage = "unit-absence"
        _prove_linux_service_unit_missing(home, unit)
        stage = "manager-query"
        manager_before = _read_linux_service_manager_absence_state(
            systemctl,
            manager_environment,
            (runtime_descriptor, manager_peer.directory_descriptor),
        )
        manager_after = _read_linux_service_manager_absence_state(
            systemctl,
            manager_environment,
            (runtime_descriptor, manager_peer.directory_descriptor),
        )
        stage = "unit-absence"
        _prove_linux_service_unit_missing(home, unit)
        stage = "manager-provenance"
        manager_identity_after = _read_linux_process_identity(manager_peer.pid)
        manager_cgroup_after = _read_linux_systemd_manager_cgroup(
            manager_peer.pid, current_uid
        )
        stage = "manager-binary-readlink"
        manager_executable_after = _read_linux_systemd_manager_executable(
            manager_peer.pid
        )
        stage = "manager-provenance"
        manager_cmdline_after = _read_linux_systemd_manager_cmdline(
            manager_peer.pid
        )
        stage = "sandwich"
        if (
            manager_after != manager_before
            or _linux_file_signature(os.fstat(runtime_descriptor))
            != runtime_identity
            or manager_identity_after != manager_identity_before
            or manager_cgroup_after != manager_cgroup_before
            or manager_executable_after != manager_executable_before
            or manager_cmdline_after != manager_cmdline_before
        ):
            raise ServiceCommandError()
        _revalidate_linux_systemd_private_peer(manager_peer, current_uid)
        _revalidate_linux_systemctl_executable(systemctl_binding)
        return LinuxCollectorServiceAbsenceState(
            unit_missing=True,
            unit_id=manager_before["Id"],
            load_state=manager_before["LoadState"],
            active_state=manager_before["ActiveState"],
            sub_state=manager_before["SubState"],
            unit_file_state=None,
            main_pid=0,
            control_pid=0,
            job=None,
            fragment_path=None,
            drop_in_paths=(),
            needs_reload=False,
        )
    except ServiceCommandError as error:
        if error.stage is not None:
            raise
        raise ServiceCommandError(stage=stage) from error
    except Exception as error:
        raise ServiceCommandError(stage=stage) from error
    finally:
        close_failed = False
        if manager_peer is not None:
            try:
                manager_peer.connection.close()
            except Exception:
                close_failed = True
            try:
                os.close(manager_peer.directory_descriptor)
            except OSError:
                close_failed = True
        if systemctl_binding is not None:
            for descriptor in (
                systemctl_binding.executable_descriptor,
                systemctl_binding.parent_descriptor,
            ):
                try:
                    os.close(descriptor)
                except OSError:
                    close_failed = True
        if runtime_descriptor is not None:
            try:
                os.close(runtime_descriptor)
            except OSError:
                close_failed = True
        if close_failed:
            raise ServiceCommandError(stage="cleanup")


def _root_owned_directory(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == 0
        and not stat.S_IMODE(metadata.st_mode) & 0o022
    )


def _bind_linux_systemctl_executable(path: str) -> _BoundLinuxExecutable:
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    if (
        path != "/usr/bin/systemctl"
        or not directory_flag
        or not nofollow_flag
    ):
        raise ServiceCommandError()
    directory_flags = (
        os.O_RDONLY
        | directory_flag
        | nofollow_flag
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = os.O_RDONLY | nofollow_flag | getattr(os, "O_CLOEXEC", 0)
    descriptors: list[int] = []
    try:
        root_descriptor = os.open("/", directory_flags)
        descriptors.append(root_descriptor)
        if not _root_owned_directory(os.fstat(root_descriptor)):
            raise ServiceCommandError()
        usr_descriptor = os.open("usr", directory_flags, dir_fd=root_descriptor)
        descriptors.append(usr_descriptor)
        if not _root_owned_directory(os.fstat(usr_descriptor)):
            raise ServiceCommandError()
        bin_descriptor = os.open("bin", directory_flags, dir_fd=usr_descriptor)
        descriptors.append(bin_descriptor)
        if not _root_owned_directory(os.fstat(bin_descriptor)):
            raise ServiceCommandError()
        executable_descriptor = os.open(
            "systemctl", file_flags, dir_fd=bin_descriptor
        )
        descriptors.append(executable_descriptor)
        executable_metadata = os.fstat(executable_descriptor)
        executable_mode = stat.S_IMODE(executable_metadata.st_mode)
        if (
            not stat.S_ISREG(executable_metadata.st_mode)
            or executable_metadata.st_uid != 0
            or executable_metadata.st_nlink != 1
            or not executable_mode & 0o100
            or executable_mode & 0o022
        ):
            os.close(executable_descriptor)
            descriptors.remove(executable_descriptor)
            raise ServiceCommandError()
        public_metadata = os.stat(
            "systemctl", dir_fd=bin_descriptor, follow_symlinks=False
        )
        signature = _linux_file_signature(executable_metadata)
        if _linux_file_signature(public_metadata) != signature:
            os.close(executable_descriptor)
            descriptors.remove(executable_descriptor)
            raise ServiceCommandError()
        for descriptor in (usr_descriptor, root_descriptor):
            os.close(descriptor)
            descriptors.remove(descriptor)
        descriptors.remove(executable_descriptor)
        descriptors.remove(bin_descriptor)
        return _BoundLinuxExecutable(
            parent_descriptor=bin_descriptor,
            executable_descriptor=executable_descriptor,
            signature=signature,
        )
    except ServiceCommandError:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    except Exception as error:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise ServiceCommandError() from error


def _revalidate_linux_systemctl_executable(
    binding: _BoundLinuxExecutable,
) -> None:
    try:
        descriptor_metadata = os.fstat(binding.executable_descriptor)
        public_metadata = os.stat(
            "systemctl",
            dir_fd=binding.parent_descriptor,
            follow_symlinks=False,
        )
    except Exception as error:
        raise ServiceCommandError() from error
    if (
        _linux_file_signature(descriptor_metadata) != binding.signature
        or _linux_file_signature(public_metadata) != binding.signature
    ):
        raise ServiceCommandError()


def _open_linux_user_runtime_directory(current_uid: int) -> int:
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    if (
        type(current_uid) is not int
        or current_uid < 0
        or not directory_flag
        or not nofollow_flag
    ):
        raise ServiceCommandError()
    flags = os.O_RDONLY | directory_flag | nofollow_flag | getattr(
        os, "O_CLOEXEC", 0
    )
    descriptors: list[int] = []
    try:
        run_descriptor = os.open("/run", flags)
        descriptors.append(run_descriptor)
        run_metadata = os.fstat(run_descriptor)
        if (
            not stat.S_ISDIR(run_metadata.st_mode)
            or run_metadata.st_uid != 0
            or stat.S_IMODE(run_metadata.st_mode) & 0o022
        ):
            raise ServiceCommandError()
        user_descriptor = os.open("user", flags, dir_fd=run_descriptor)
        descriptors.append(user_descriptor)
        user_metadata = os.fstat(user_descriptor)
        if (
            not stat.S_ISDIR(user_metadata.st_mode)
            or user_metadata.st_uid != 0
            or stat.S_IMODE(user_metadata.st_mode) & 0o022
        ):
            raise ServiceCommandError()
        runtime_descriptor = os.open(
            str(current_uid), flags, dir_fd=user_descriptor
        )
        runtime_metadata = os.fstat(runtime_descriptor)
        if (
            not stat.S_ISDIR(runtime_metadata.st_mode)
            or runtime_metadata.st_uid != current_uid
            or stat.S_IMODE(runtime_metadata.st_mode) != 0o700
        ):
            os.close(runtime_descriptor)
            raise ServiceCommandError()
        close_failed = False
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                close_failed = True
        descriptors.clear()
        if close_failed:
            os.close(runtime_descriptor)
            raise ServiceCommandError()
        return runtime_descriptor
    except ServiceCommandError:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    except Exception as error:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise ServiceCommandError() from error


def _bind_linux_systemd_private_peer(
    runtime_descriptor: int, current_uid: int
) -> _BoundLinuxManagerPeer:
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    if not directory_flag or not nofollow_flag:
        raise ServiceCommandError()
    flags = os.O_RDONLY | directory_flag | nofollow_flag | getattr(
        os, "O_CLOEXEC", 0
    )
    directory_descriptor: int | None = None
    connection: object | None = None
    try:
        directory_descriptor = os.open(
            "systemd", flags, dir_fd=runtime_descriptor
        )
        directory_metadata = os.fstat(directory_descriptor)
        public_directory = os.stat(
            "systemd",
            dir_fd=runtime_descriptor,
            follow_symlinks=False,
        )
        directory_signature = _linux_file_signature(directory_metadata)
        directory_mode = stat.S_IMODE(directory_metadata.st_mode)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != current_uid
            or directory_mode & 0o022
            or _linux_file_signature(public_directory) != directory_signature
        ):
            raise ServiceCommandError()
        private_metadata = os.stat(
            "private",
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        private_mode = stat.S_IMODE(private_metadata.st_mode)
        if (
            not stat.S_ISSOCK(private_metadata.st_mode)
            or private_metadata.st_uid != current_uid
            or private_metadata.st_nlink != 1
            or private_mode not in {0o600, 0o700}
        ):
            raise ServiceCommandError()
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(1.0)
        connection.connect(
            f"/proc/self/fd/{directory_descriptor}/private"
        )
        raw_credentials = connection.getsockopt(
            _LINUX_SOL_SOCKET, _LINUX_SO_PEERCRED, struct.calcsize("3i")
        )
        if type(raw_credentials) is not bytes or len(raw_credentials) != 12:
            raise ServiceCommandError()
        peer_pid, peer_uid, peer_gid = struct.unpack("3i", raw_credentials)
        if (
            peer_pid <= 0
            or peer_uid != current_uid
            or peer_gid != os.getgid()
        ):
            raise ServiceCommandError()
        return _BoundLinuxManagerPeer(
            directory_descriptor=directory_descriptor,
            connection=connection,
            directory_signature=directory_signature,
            socket_signature=_linux_file_signature(private_metadata),
            pid=peer_pid,
            uid=peer_uid,
            gid=peer_gid,
        )
    except ServiceCommandError:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if directory_descriptor is not None:
            try:
                os.close(directory_descriptor)
            except OSError:
                pass
        raise
    except Exception as error:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if directory_descriptor is not None:
            try:
                os.close(directory_descriptor)
            except OSError:
                pass
        raise ServiceCommandError() from error


def _revalidate_linux_systemd_private_peer(
    binding: _BoundLinuxManagerPeer, current_uid: int
) -> None:
    try:
        directory_metadata = os.fstat(binding.directory_descriptor)
        private_metadata = os.stat(
            "private",
            dir_fd=binding.directory_descriptor,
            follow_symlinks=False,
        )
        raw_credentials = binding.connection.getsockopt(
            _LINUX_SOL_SOCKET, _LINUX_SO_PEERCRED, struct.calcsize("3i")
        )
    except Exception as error:
        raise ServiceCommandError() from error
    if type(raw_credentials) is not bytes or len(raw_credentials) != 12:
        raise ServiceCommandError()
    peer_pid, peer_uid, peer_gid = struct.unpack("3i", raw_credentials)
    if (
        _linux_file_signature(directory_metadata)
        != binding.directory_signature
        or _linux_file_signature(private_metadata) != binding.socket_signature
        or (peer_pid, peer_uid, peer_gid)
        != (binding.pid, current_uid, binding.gid)
    ):
        raise ServiceCommandError()


def _read_linux_systemd_manager_executable(
    manager_pid: int,
) -> tuple[str, tuple[int, ...]]:
    proc_executable = f"/proc/{manager_pid}/exe"
    try:
        raw_path = os.readlink(proc_executable)
    except Exception as error:
        raise ServiceCommandError(stage="manager-binary-readlink") from error
    if raw_path != "/usr/lib/systemd/systemd":
        raise ServiceCommandError(stage="manager-binary-path-value")
    try:
        proc_metadata = os.stat(proc_executable)
    except Exception as error:
        raise ServiceCommandError(stage="manager-binary-metadata") from error
    try:
        public_metadata = os.stat(raw_path, follow_symlinks=False)
    except Exception as error:
        raise ServiceCommandError(
            stage="manager-binary-public-identity"
        ) from error
    mode = stat.S_IMODE(proc_metadata.st_mode)
    signature = _linux_file_signature(proc_metadata)
    if (
        not stat.S_ISREG(proc_metadata.st_mode)
        or proc_metadata.st_uid != 0
        or proc_metadata.st_nlink != 1
        or not mode & 0o100
        or mode & 0o022
    ):
        raise ServiceCommandError(stage="manager-binary-metadata")
    if _linux_file_signature(public_metadata) != signature:
        raise ServiceCommandError(stage="manager-binary-public-identity")
    return raw_path, signature


def _read_linux_systemd_manager_cmdline(manager_pid: int) -> bytes:
    payload = _read_linux_process_cmdline(manager_pid)
    if payload not in {
        b"/usr/lib/systemd/systemd\0--user\0",
        b"/lib/systemd/systemd\0--user\0",
    }:
        raise ServiceCommandError()
    return payload


def _read_linux_systemd_manager_cgroup(
    manager_pid: int, current_uid: int
) -> str:
    try:
        payload = _read_linux_proc_file(manager_pid, "cgroup")
        text = payload.decode("ascii")
    except (UnicodeError, ServiceCommandError) as error:
        raise ServiceCommandError() from error
    expected = (
        f"0::/user.slice/user-{current_uid}.slice/"
        f"user@{current_uid}.service/init.scope\n"
    )
    if text != expected:
        raise ServiceCommandError()
    return text


def _read_bounded_descriptor(descriptor: int, limit: int) -> bytes:
    payload = bytearray()
    while True:
        block = os.read(descriptor, min(8192, limit + 1 - len(payload)))
        if not block:
            return bytes(payload)
        payload.extend(block)
        if len(payload) > limit:
            raise ServiceCommandError()


def _linux_file_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_linux_service_unit(home: Path, unit: Path) -> tuple[str, bytes]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise ServiceCommandError()
    parent = _open_service_directory_posix(
        home,
        (".config", "systemd", "user"),
        create=False,
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            unit.name,
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size <= 0
            or before.st_size > _LINUX_SERVICE_FACT_LIMIT
        ):
            raise ServiceCommandError()
        payload = _read_bounded_descriptor(
            descriptor, _LINUX_SERVICE_FACT_LIMIT
        )
        after = os.fstat(descriptor)
        public = os.stat(unit.name, dir_fd=parent, follow_symlinks=False)
        signature = _linux_file_signature(before)
        if (
            len(payload) != before.st_size
            or _linux_file_signature(after) != signature
            or _linux_file_signature(public) != signature
        ):
            raise ServiceCommandError()
        return f"{before.st_dev}:{before.st_ino}", payload
    except ServiceCommandError:
        raise
    except Exception as error:
        raise ServiceCommandError() from error
    finally:
        close_failed = False
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                close_failed = True
        try:
            os.close(parent)
        except OSError:
            close_failed = True
        if close_failed:
            raise ServiceCommandError()


class _LinuxOpenHow(ctypes.Structure):
    _fields_ = (
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    )


def _prove_linux_service_unit_missing(home: Path, unit: Path) -> None:
    """Prove the canonical unit is absent without following any symlink."""

    canonical = home / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
    if unit != canonical or not home.is_absolute():
        raise ServiceCommandError()
    try:
        active_kernel = os.uname().sysname
    except Exception as error:
        raise ServiceCommandError() from error
    if active_kernel == "Linux":
        open_path = getattr(os, "O_PATH", 0)
        close_on_exec = getattr(os, "O_CLOEXEC", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        if (
            not open_path
            or not close_on_exec
            or not nofollow
            or not directory_flag
        ):
            raise ServiceCommandError()
        how = _LinuxOpenHow(
            flags=open_path | nofollow | close_on_exec,
            mode=0,
            resolve=0x04,  # RESOLVE_NO_SYMLINKS
        )
        home_descriptor: int | None = None
        failed = False
        try:
            home_public_before = os.stat(home, follow_symlinks=False)
            home_descriptor = os.open(
                home,
                os.O_RDONLY | directory_flag | nofollow | close_on_exec,
            )
            home_opened = os.fstat(home_descriptor)
            home_signature = _linux_file_signature(home_opened)
            if (
                not stat.S_ISDIR(home_opened.st_mode)
                or home_opened.st_uid != os.getuid()
                or stat.S_IMODE(home_opened.st_mode) & 0o022
                or _linux_file_signature(home_public_before) != home_signature
            ):
                raise ServiceCommandError()
            libc = ctypes.CDLL(None, use_errno=True)
            syscall = libc.syscall
            syscall.restype = ctypes.c_long
            ctypes.set_errno(0)
            result = syscall(
                ctypes.c_long(437),  # __NR_openat2 on supported Linux arches
                ctypes.c_int(home_descriptor),
                ctypes.c_char_p(
                    b".config/systemd/user/" + os.fsencode(SYSTEMD_UNIT_NAME)
                ),
                ctypes.byref(how),
                ctypes.c_size_t(ctypes.sizeof(how)),
            )
            observed_errno = ctypes.get_errno()
            home_public_after = os.stat(home, follow_symlinks=False)
            if (
                result >= 0
                or observed_errno != errno.ENOENT
                or _linux_file_signature(home_public_after) != home_signature
                or _linux_file_signature(os.fstat(home_descriptor))
                != home_signature
            ):
                if result >= 0:
                    try:
                        os.close(int(result))
                    except OSError:
                        failed = True
                raise ServiceCommandError()
        except ServiceCommandError:
            failed = True
        except Exception:
            failed = True
        finally:
            if home_descriptor is not None:
                try:
                    os.close(home_descriptor)
                except OSError:
                    failed = True
        if failed:
            raise ServiceCommandError()
        return

    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not directory_flag or not nofollow:
        raise ServiceCommandError()
    flags = os.O_RDONLY | directory_flag | nofollow | getattr(
        os, "O_CLOEXEC", 0
    )
    descriptors: list[int] = []
    bindings: list[tuple[int, str, tuple[int, ...]]] = []
    missing_parent: int | None = None
    missing_name: str | None = None
    try:
        home_public = os.stat(home, follow_symlinks=False)
        home_descriptor = os.open(home, flags)
        descriptors.append(home_descriptor)
        home_opened = os.fstat(home_descriptor)
        home_signature = _linux_file_signature(home_opened)
        if (
            not stat.S_ISDIR(home_opened.st_mode)
            or home_opened.st_uid != os.getuid()
            or stat.S_IMODE(home_opened.st_mode) & 0o022
            or _linux_file_signature(home_public) != home_signature
        ):
            raise ServiceCommandError()
        parent = home_descriptor
        for name in (".config", "systemd", "user"):
            try:
                public = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                missing_parent = parent
                missing_name = name
                break
            child = os.open(name, flags, dir_fd=parent)
            descriptors.append(child)
            opened = os.fstat(child)
            signature = _linux_file_signature(opened)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) & 0o022
                or _linux_file_signature(public) != signature
            ):
                raise ServiceCommandError()
            bindings.append((parent, name, signature))
            parent = child
        if missing_parent is None:
            missing_parent = parent
            missing_name = SYSTEMD_UNIT_NAME
            try:
                os.stat(
                    missing_name,
                    dir_fd=missing_parent,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise ServiceCommandError()
        if _linux_file_signature(os.stat(home, follow_symlinks=False)) != home_signature:
            raise ServiceCommandError()
        for parent, name, signature in bindings:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _linux_file_signature(current) != signature:
                raise ServiceCommandError()
        try:
            os.stat(
                missing_name,
                dir_fd=missing_parent,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise ServiceCommandError()
    except ServiceCommandError:
        raise
    except Exception as error:
        raise ServiceCommandError() from error
    finally:
        close_failed = False
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                close_failed = True
        if close_failed:
            raise ServiceCommandError()


def _read_linux_service_manager_properties(
    executable: str,
    environment: dict[str, str],
    pass_fds: tuple[int, ...],
    *,
    properties: tuple[str, ...],
    include_all: bool,
    empty_properties: frozenset[str],
) -> dict[str, str]:
    if executable not in _TRUSTED_SYSTEMCTL_PATHS:
        raise ServiceCommandError()
    command = [
        executable,
        "--user",
        "show",
        SYSTEMD_UNIT_NAME,
        *[f"--property={name}" for name in properties],
    ]
    if include_all:
        command.append("--all")
    command.append("--no-pager")
    try:
        from .bounded_process import BoundedProcessError, run_bounded

        completed = run_bounded(
            command,
            timeout=_LINUX_SERVICE_QUERY_TIMEOUT_SECONDS,
            stdout_limit=_LINUX_SERVICE_FACT_LIMIT,
            stderr_limit=0,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            env=environment,
            pass_fds=pass_fds,
        )
    except (BoundedProcessError, OSError, subprocess.SubprocessError) as error:
        raise ServiceCommandError() from error
    if (
        type(completed.returncode) is not int
        or completed.returncode != 0
        or type(completed.stdout) is not bytes
        or not completed.stdout
        or len(completed.stdout) > _LINUX_SERVICE_FACT_LIMIT
    ):
        raise ServiceCommandError()
    try:
        lines = completed.stdout.decode("utf-8").splitlines()
    except UnicodeError as error:
        raise ServiceCommandError() from error
    if len(lines) != len(properties):
        raise ServiceCommandError()
    values: dict[str, str] = {}
    for line in lines:
        name, separator, value = line.partition("=")
        if (
            separator != "="
            or name not in properties
            or (not value and name not in empty_properties)
            or any(not character.isprintable() for character in value)
            or name in values
        ):
            raise ServiceCommandError()
        values[name] = value
    if set(values) != set(properties):
        raise ServiceCommandError()
    return values


def _read_linux_service_manager_state(
    executable: str,
    environment: dict[str, str],
    pass_fds: tuple[int, ...],
) -> dict[str, str]:
    values = _read_linux_service_manager_properties(
        executable,
        environment,
        pass_fds,
        properties=_LINUX_SERVICE_PROPERTIES,
        include_all=False,
        empty_properties=frozenset({"DropInPaths"}),
    )
    if values["DropInPaths"]:
        raise ServiceCommandError()
    if values["NeedDaemonReload"] not in {"yes", "no"}:
        raise ServiceCommandError()
    pid_text = values["MainPID"]
    if not pid_text.isascii() or not pid_text.isdecimal():
        raise ServiceCommandError()
    pid = int(pid_text)
    if pid <= 0 or str(pid) != pid_text:
        raise ServiceCommandError()
    return values


def _read_linux_service_manager_absence_state(
    executable: str,
    environment: dict[str, str],
    pass_fds: tuple[int, ...],
) -> dict[str, str]:
    values = _read_linux_service_manager_properties(
        executable,
        environment,
        pass_fds,
        properties=_LINUX_SERVICE_ABSENCE_PROPERTIES,
        include_all=True,
        empty_properties=frozenset(
            {"UnitFileState", "FragmentPath", "DropInPaths", "Job"}
        ),
    )
    if values != {
        "Id": SYSTEMD_UNIT_NAME,
        "LoadState": "not-found",
        "ActiveState": "inactive",
        "SubState": "dead",
        "UnitFileState": "",
        "FragmentPath": "",
        "DropInPaths": "",
        "NeedDaemonReload": "no",
        "MainPID": "0",
        "ControlPID": "0",
        "Job": "",
    }:
        raise ServiceCommandError()
    return values


def _read_linux_process_executable(
    main_pid: int,
) -> tuple[Path, str, tuple[int, ...]]:
    proc_executable = f"/proc/{main_pid}/exe"
    try:
        raw_path = os.readlink(proc_executable)
        executable = Path(raw_path)
        proc_metadata = os.stat(proc_executable)
        public_metadata = os.stat(executable, follow_symlinks=False)
    except Exception as error:
        raise ServiceCommandError() from error
    if (
        type(raw_path) is not str
        or not executable.is_absolute()
        or ".." in executable.parts
        or any(not character.isprintable() for character in raw_path)
        or not stat.S_ISREG(proc_metadata.st_mode)
        or proc_metadata.st_uid != os.getuid()
        or proc_metadata.st_nlink != 1
        or stat.S_IMODE(proc_metadata.st_mode) != 0o700
        or (proc_metadata.st_dev, proc_metadata.st_ino)
        != (public_metadata.st_dev, public_metadata.st_ino)
        or _linux_executable_signature(proc_metadata)
        != _linux_executable_signature(public_metadata)
    ):
        raise ServiceCommandError()
    return (
        executable,
        f"{proc_metadata.st_dev}:{proc_metadata.st_ino}",
        _linux_executable_signature(proc_metadata),
    )


def _linux_executable_signature(metadata: os.stat_result) -> tuple[int, ...]:
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


def _read_linux_process_cmdline(main_pid: int) -> bytes:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise ServiceCommandError()
    descriptor: int | None = None
    try:
        descriptor = os.open(
            f"/proc/{main_pid}/cmdline",
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
        )
        payload = _read_bounded_descriptor(
            descriptor, _LINUX_SERVICE_FACT_LIMIT
        )
        if (
            not payload
            or not payload.endswith(b"\0")
            or b"\0\0" in payload
        ):
            raise ServiceCommandError()
        return payload
    except ServiceCommandError:
        raise
    except Exception as error:
        raise ServiceCommandError() from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as error:
                raise ServiceCommandError() from error


def _read_linux_proc_file(main_pid: int, name: str) -> bytes:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow or name not in {"cgroup", "stat", "status"}:
        raise ServiceCommandError()
    descriptor: int | None = None
    try:
        descriptor = os.open(
            f"/proc/{main_pid}/{name}",
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
        )
        payload = _read_bounded_descriptor(
            descriptor, _LINUX_SERVICE_FACT_LIMIT
        )
        if not payload:
            raise ServiceCommandError()
        return payload
    except ServiceCommandError:
        raise
    except Exception as error:
        raise ServiceCommandError() from error
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as error:
                raise ServiceCommandError() from error


def _read_linux_process_identity(main_pid: int) -> tuple[int, int, int]:
    try:
        status_text = _read_linux_proc_file(main_pid, "status").decode("ascii")
        stat_text = _read_linux_proc_file(main_pid, "stat").decode("ascii").strip()
    except (UnicodeError, ServiceCommandError) as error:
        raise ServiceCommandError() from error
    uid_lines = [line for line in status_text.splitlines() if line.startswith("Uid:")]
    if len(uid_lines) != 1:
        raise ServiceCommandError()
    uid_parts = uid_lines[0].split()
    if len(uid_parts) != 5 or uid_parts[0] != "Uid:":
        raise ServiceCommandError()
    try:
        uid_values = tuple(int(value) for value in uid_parts[1:])
    except ValueError as error:
        raise ServiceCommandError() from error
    if (
        any(not value.isascii() or not value.isdecimal() for value in uid_parts[1:])
        or any(str(number) != value for number, value in zip(uid_values, uid_parts[1:], strict=True))
        or len(set(uid_values)) != 1
        or uid_values[0] != os.getuid()
    ):
        raise ServiceCommandError()
    closing_parenthesis = stat_text.rfind(")")
    if (
        not stat_text.startswith(f"{main_pid} (")
        or closing_parenthesis <= len(str(main_pid)) + 2
        or closing_parenthesis + 2 >= len(stat_text)
        or stat_text[closing_parenthesis + 1] != " "
    ):
        raise ServiceCommandError()
    remaining_fields = stat_text[closing_parenthesis + 2 :].split()
    if len(remaining_fields) <= 19:
        raise ServiceCommandError()
    parent_pid_text = remaining_fields[1]
    start_time_text = remaining_fields[19]
    if (
        not parent_pid_text.isascii()
        or not parent_pid_text.isdecimal()
        or not start_time_text.isascii()
        or not start_time_text.isdecimal()
    ):
        raise ServiceCommandError()
    parent_pid = int(parent_pid_text)
    start_time_ticks = int(start_time_text)
    if (
        parent_pid < 0
        or str(parent_pid) != parent_pid_text
        or start_time_ticks <= 0
        or str(start_time_ticks) != start_time_text
    ):
        raise ServiceCommandError()
    return uid_values[0], start_time_ticks, parent_pid


def _read_linux_collector_cgroup(main_pid: int, current_uid: int) -> str:
    try:
        payload = _read_linux_proc_file(main_pid, "cgroup")
        text = payload.decode("ascii")
    except (UnicodeError, ServiceCommandError) as error:
        raise ServiceCommandError() from error
    expected = (
        f"0::/user.slice/user-{current_uid}.slice/"
        f"user@{current_uid}.service/app.slice/{SYSTEMD_UNIT_NAME}\n"
    )
    if text != expected:
        raise ServiceCommandError()
    return text


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
