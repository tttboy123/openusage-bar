"""Cross-platform user-service registration for the headless collector.

The collector daemon can run as a LaunchAgent (macOS), a systemd user unit
(Linux), or a scheduled task (Windows). Rendering is pure and testable;
``install_service`` / ``uninstall_service`` dispatch to the active platform and
surface activation errors without hiding secrets.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


COLLECTOR_LABEL = "com.lune.openusagebar.collector"
SYSTEMD_UNIT_NAME = "openusage-bar.service"
WINDOWS_TASK_NAME = "OpenUsageBarCollector"
COLLECTOR_COMMAND_NAME = "openusage-bar"
DEFAULT_API_SOCKET = "~/.local/state/openusage-bar/openusage.sock"
DEFAULT_LOG_PATH = "~/.local/state/openusage-bar/collector.log"
DEFAULT_ERROR_LOG_PATH = "~/.local/state/openusage-bar/collector.err.log"


def _expand(path: str) -> str:
    return os.path.expanduser(path)


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
    return "\n".join(
        [
            "[Unit]",
            "Description=UsageHub (formerly OpenUsage Bar) headless collector",
            "After=default.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={COLLECTOR_COMMAND_NAME} daemon --interval {int(interval)} --api-socket {_expand(api_socket)}",
            "Restart=on-failure",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def windows_task_xml(*, interval_minutes: int = 5) -> str:
    """Render a Windows Task Scheduler task definition (UTF-16) for the collector."""
    minutes = int(interval_minutes)
    if minutes < 1:
        raise ValueError("interval_minutes must be positive")
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
        f"      <Command>{COLLECTOR_COMMAND_NAME}</Command>\n"
        f"      <Arguments>daemon --interval {minutes * 60}</Arguments>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


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
        xml = windows_task_xml(interval_minutes=max(1, int(interval) // 60))
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
