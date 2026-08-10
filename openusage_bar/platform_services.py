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


def _windows_runtime_descriptor(
    *,
    allow_cross_host_state: bool = False,
) -> RuntimeDescriptor:
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


def launchd_plist(
    *,
    interval: int = 300,
    api_socket: str = DEFAULT_API_SOCKET,
    stdout_path: str = DEFAULT_LOG_PATH,
    stderr_path: str = DEFAULT_ERROR_LOG_PATH,
) -> str:
    """Render a macOS LaunchAgent property list (XML) for the collector."""
    import plistlib

    payload = {
        "Label": COLLECTOR_LABEL,
        "ProgramArguments": [
            COLLECTOR_COMMAND_NAME,
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


def systemd_unit(*, interval: int = 300, api_socket: str = DEFAULT_API_SOCKET) -> str:
    """Render a systemd user unit for the collector."""
    socket_argument = _systemd_argument(_validated_expanded_path(api_socket))
    return "\n".join(
        [
            "[Unit]",
            "Description=UsageHub (formerly OpenUsage Bar) headless collector",
            "After=default.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={COLLECTOR_COMMAND_NAME} daemon --interval {int(interval)} --api-transport unix --api-socket {socket_argument}",
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
    _allow_cross_host_state: bool = False,
) -> str:
    """Render a Windows Task Scheduler task definition (UTF-16) for the collector."""
    minutes = int(interval_minutes)
    if minutes < 1:
        raise ValueError("interval_minutes must be positive")
    descriptor = _windows_runtime_descriptor(
        allow_cross_host_state=_allow_cross_host_state
    )
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
        "    <CalendarTrigger>\n"
        "      <StartBoundary>2026-01-01T00:00:00</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>\n"
        "    </CalendarTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>\n'
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>false</StartWhenAvailable>\n"
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
        f"      <Command>{escape(COLLECTOR_COMMAND_NAME)}</Command>\n"
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


def render_current_platform(*, interval: int = 300) -> str:
    """Render the service definition for the active platform."""
    if sys.platform == "darwin":
        return launchd_plist(interval=interval)
    if sys.platform.startswith("linux"):
        return systemd_unit(interval=interval)
    if sys.platform == "win32":
        return windows_task_xml(interval_minutes=max(1, int(interval) // 60))
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
        raise RuntimeError("service activation command failed") from error
    if completed.returncode != 0:
        raise RuntimeError(f"service activation command failed with exit {completed.returncode}")


def _collector_command() -> str:
    return shutil.which(COLLECTOR_COMMAND_NAME) or COLLECTOR_COMMAND_NAME


def _write_plugin_service_file(target: Path, payload: str, *, encoding: str) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = target.parent.lstat()
    if not parent or target.parent.is_symlink() or not target.parent.is_dir():
        raise RuntimeError("Plugin service target is unsafe")
    try:
        existing = target.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (target.is_symlink() or not target.is_file()):
        raise RuntimeError("Plugin service target is unsafe")
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
                raise OSError("short Plugin service write")
            offset += written
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    else:
        os.close(descriptor)
    try:
        if target.exists() and target.is_symlink():
            raise RuntimeError("Plugin service target is unsafe")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def install_service(*, interval: int = 300) -> None:
    """Write and activate the user service for the active platform."""
    if sys.platform == "darwin":
        target = Path.home() / "Library" / "LaunchAgents" / f"{COLLECTOR_LABEL}.plist"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(launchd_plist(interval=interval), encoding="utf-8")
        _run(["launchctl", "load", "-w", str(target)])
        return
    if sys.platform.startswith("linux"):
        target = Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(systemd_unit(interval=interval), encoding="utf-8")
        if shutil.which("systemctl"):
            _run(["systemctl", "--user", "daemon-reload"])
            _run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME])
        return
    if sys.platform == "win32":
        xml = windows_task_xml(
            interval_minutes=max(1, int(interval) // 60),
            _allow_cross_host_state=True,
        )
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home()
        target = base / "openusage-bar-task.xml"
        target.write_text(xml, encoding="utf-16")
        _run(
            [
                "schtasks", "/Create", "/TN", WINDOWS_TASK_NAME,
                "/XML", str(target), "/F",
            ]
        )
        return
    raise RuntimeError(f"unsupported platform: {sys.platform}")


def uninstall_service() -> None:
    """Remove and deactivate the user service for the active platform."""
    if sys.platform == "darwin":
        target = Path.home() / "Library" / "LaunchAgents" / f"{COLLECTOR_LABEL}.plist"
        if target.exists():
            _run(["launchctl", "unload", str(target)])
            target.unlink()
        return
    if sys.platform.startswith("linux"):
        if shutil.which("systemctl"):
            _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME])
        target = Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
        if target.exists():
            target.unlink()
        return
    if sys.platform == "win32":
        _run(["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"])
        return
    raise RuntimeError(f"unsupported platform: {sys.platform}")


def install_plugin_service() -> None:
    """Install only the independent Plugin listener user service."""
    if sys.platform == "darwin":
        target = Path.home() / "Library" / "LaunchAgents" / f"{PLUGIN_LABEL}.plist"
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_plugin_service_file(
            target, plugin_launchd_plist(command=_plugin_command()), encoding="utf-8"
        )
        _run(["launchctl", "load", "-w", str(target)])
        return
    if sys.platform.startswith("linux"):
        target = Path.home() / ".config" / "systemd" / "user" / PLUGIN_SYSTEMD_UNIT_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_plugin_service_file(
            target, plugin_systemd_unit(command=_plugin_command()), encoding="utf-8"
        )
        if shutil.which("systemctl"):
            _run(["systemctl", "--user", "daemon-reload"])
            _run(["systemctl", "--user", "enable", "--now", PLUGIN_SYSTEMD_UNIT_NAME])
        return
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise RuntimeError("Windows state directory is unavailable")
        target = Path(local_app_data) / "openusage-bar-plugin-task.xml"
        _write_plugin_service_file(
            target, plugin_windows_task_xml(command=_plugin_command()), encoding="utf-16"
        )
        _run(["schtasks", "/Create", "/TN", PLUGIN_WINDOWS_TASK_NAME, "/XML", str(target), "/F"])
        return
    raise RuntimeError(f"unsupported platform: {sys.platform}")


def uninstall_plugin_service() -> None:
    """Remove only the independent Plugin listener user service."""
    if sys.platform == "darwin":
        target = Path.home() / "Library" / "LaunchAgents" / f"{PLUGIN_LABEL}.plist"
        if target.exists():
            _run(["launchctl", "unload", str(target)])
            target.unlink()
        return
    if sys.platform.startswith("linux"):
        if shutil.which("systemctl"):
            _run(["systemctl", "--user", "disable", "--now", PLUGIN_SYSTEMD_UNIT_NAME])
        target = Path.home() / ".config" / "systemd" / "user" / PLUGIN_SYSTEMD_UNIT_NAME
        if target.exists():
            target.unlink()
        return
    if sys.platform == "win32":
        _run(["schtasks", "/Delete", "/TN", PLUGIN_WINDOWS_TASK_NAME, "/F"])
        return
    raise RuntimeError(f"unsupported platform: {sys.platform}")
