#!/usr/bin/env python3
"""Measure privacy-safe OpenUsage Bar resident and refresh performance.

The report intentionally excludes PIDs, executable paths, commands, Provider
identities, credentials, payloads, prompts, responses, and account identity.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import plistlib
import signal
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


MIB = 1024 * 1024
RUSAGE_INFO_V2 = 2
EXPECTED_BUNDLE_ID = "com.lune.openusagebar"
STATUS_LABEL = "com.lune.openusagebar"
COLLECTOR_LABEL = "com.lune.openusagebar.collector"


@dataclass(frozen=True)
class RawProcessUsage:
    cpu_nanoseconds: int
    wakeups: int
    footprint_bytes: int
    start_abstime: int = 0


@dataclass(frozen=True)
class Budget:
    metric: str
    threshold: float
    comparison: str


DEFAULT_BUDGETS = (
    Budget("appBytes", 75 * MIB, "maximum"),
    Budget("residentCpuPercentP95", 1.0, "maximum"),
    Budget("residentWakeupsPerSecondP95", 5.0, "maximum"),
    Budget("residentFootprintBytesPeak", 100 * MIB, "maximum"),
    Budget("activityFootprintBytesPeak", 120 * MIB, "maximum"),
    Budget("refreshDurationSecondsP95", 90.0, "maximum"),
)


class _RusageInfoV2(ctypes.Structure):
    _fields_ = [
        ("uuid", ctypes.c_ubyte * 16),
        ("user_time", ctypes.c_uint64),
        ("system_time", ctypes.c_uint64),
        ("pkg_idle_wkups", ctypes.c_uint64),
        ("interrupt_wkups", ctypes.c_uint64),
        ("pageins", ctypes.c_uint64),
        ("wired_size", ctypes.c_uint64),
        ("resident_size", ctypes.c_uint64),
        ("phys_footprint", ctypes.c_uint64),
        ("proc_start_abstime", ctypes.c_uint64),
        ("proc_exit_abstime", ctypes.c_uint64),
        ("child_user_time", ctypes.c_uint64),
        ("child_system_time", ctypes.c_uint64),
        ("child_pkg_idle_wkups", ctypes.c_uint64),
        ("child_interrupt_wkups", ctypes.c_uint64),
        ("child_pageins", ctypes.c_uint64),
        ("child_elapsed_abstime", ctypes.c_uint64),
        ("diskio_bytesread", ctypes.c_uint64),
        ("diskio_byteswritten", ctypes.c_uint64),
    ]


class DarwinProcessReader:
    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("unsupported platform")
        self._libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self._libproc.proc_pid_rusage.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        self._libproc.proc_pid_rusage.restype = ctypes.c_int
        self._libproc.proc_pidpath.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._libproc.proc_pidpath.restype = ctypes.c_int

    def usage(self, pid: int) -> RawProcessUsage | None:
        info = _RusageInfoV2()
        if self._libproc.proc_pid_rusage(
            pid, RUSAGE_INFO_V2, ctypes.byref(info)
        ) != 0:
            return None
        return RawProcessUsage(
            cpu_nanoseconds=int(info.user_time + info.system_time),
            wakeups=int(info.pkg_idle_wkups + info.interrupt_wkups),
            footprint_bytes=int(info.phys_footprint),
            start_abstime=int(info.proc_start_abstime),
        )

    def executable(self, pid: int) -> Path | None:
        buffer = ctypes.create_string_buffer(4096)
        length = self._libproc.proc_pidpath(pid, buffer, len(buffer))
        if length <= 0:
            return None
        try:
            return Path(os.fsdecode(buffer.value))
        except (TypeError, ValueError):
            return None


def _rounded(value: float) -> float:
    return round(value, 3)


def delta_usage(
    before: RawProcessUsage,
    after: RawProcessUsage,
    *,
    elapsed_seconds: float,
) -> dict[str, Any]:
    if (
        elapsed_seconds <= 0
        or after.cpu_nanoseconds < before.cpu_nanoseconds
        or after.wakeups < before.wakeups
        or (
            before.start_abstime > 0
            and after.start_abstime > 0
            and before.start_abstime != after.start_abstime
        )
    ):
        return {
            "cpuPercent": None,
            "wakeupsPerSecond": None,
            "footprintBytes": max(
                before.footprint_bytes, after.footprint_bytes
            ),
            "quality": "unavailable",
        }
    return {
        "cpuPercent": _rounded(
            (after.cpu_nanoseconds - before.cpu_nanoseconds)
            / 1_000_000_000
            / elapsed_seconds
            * 100
        ),
        "wakeupsPerSecond": _rounded(
            (after.wakeups - before.wakeups) / elapsed_seconds
        ),
        "footprintBytes": max(before.footprint_bytes, after.footprint_bytes),
        "quality": "measured",
    }


def _nearest_rank_p95(values: list[float]) -> float:
    ordered = sorted(values)
    rank = max(1, (95 * len(ordered) + 99) // 100)
    return ordered[min(rank, len(ordered)) - 1]


def aggregate_rounds(rounds: Iterable[dict[str, Any]]) -> dict[str, Any]:
    values = list(rounds)
    cpu = [
        float(item["cpuPercent"])
        for item in values
        if item.get("cpuPercent") is not None
    ]
    wakeups = [
        float(item["wakeupsPerSecond"])
        for item in values
        if item.get("wakeupsPerSecond") is not None
    ]
    footprints = [
        int(item["footprintBytes"])
        for item in values
        if item.get("footprintBytes") is not None
    ]
    return {
        "sampleCount": min(len(cpu), len(wakeups), len(footprints)),
        "cpuPercentMedian": _rounded(statistics.median(cpu)) if cpu else None,
        "cpuPercentP95": _rounded(_nearest_rank_p95(cpu)) if cpu else None,
        "wakeupsPerSecondP95": (
            _rounded(_nearest_rank_p95(wakeups)) if wakeups else None
        ),
        "footprintBytesPeak": max(footprints) if footprints else None,
    }


def validate_measurement_plan(*, rounds: int, idle_seconds: float) -> None:
    if rounds < 3 or rounds > 10:
        raise ValueError("rounds must be between 3 and 10")
    if idle_seconds < 1 or idle_seconds > 300:
        raise ValueError("idle duration must be between 1 and 300 seconds")


def _launchd_pid(label: str) -> int | None:
    try:
        completed = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
        return None
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("pid = "):
            continue
        value = stripped.removeprefix("pid = ")
        if value.isdigit() and int(value) > 1:
            return int(value)
    return None


def _activity_pid(executable: Path) -> int | None:
    escaped = re.escape(str(executable)).replace(r"\ ", " ")
    route = r"( --route (activity|capacity|api-spend|local-tools|providers|health|automation))?"
    pattern = rf"^{escaped}{route}$"
    try:
        completed = subprocess.run(
            ["/usr/bin/pgrep", "-f", pattern],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode not in (0, 1) or len(completed.stdout) > 4096:
        return None
    pids = [int(value) for value in completed.stdout.split() if value.isdigit()]
    return pids[0] if len(pids) == 1 else None


def _expected_executables(app: Path) -> dict[str, Path]:
    return {
        "status": app / "Contents/MacOS/OpenUsage Bar.runtime",
        "collector": (
            app
            / "Contents/Helpers/OpenUsage Provider Settings.app/Contents/MacOS"
            / "OpenUsage Provider Settings"
        ),
        "activity": (
            app
            / "Contents/Helpers/OpenUsage Activity.app/Contents/MacOS"
            / "OpenUsage Activity"
        ),
    }


def _role_pids(app: Path, reader: DarwinProcessReader) -> dict[str, int | None]:
    expected = _expected_executables(app)
    candidates = {
        "status": _launchd_pid(STATUS_LABEL),
        "collector": _launchd_pid(COLLECTOR_LABEL),
        "activity": _activity_pid(expected["activity"]),
    }
    verified: dict[str, int | None] = {}
    for role, pid in candidates.items():
        executable = reader.executable(pid) if pid is not None else None
        try:
            matches = (
                executable is not None
                and executable.resolve(strict=True)
                == expected[role].resolve(strict=True)
            )
        except OSError:
            matches = False
        verified[role] = pid if matches else None
    return verified


def _missing_role(role: str) -> dict[str, Any]:
    return {
        "role": role,
        "cpuPercent": None,
        "wakeupsPerSecond": None,
        "footprintBytes": None,
        "quality": "not_running",
    }


def measure_idle(
    app: Path,
    *,
    rounds: int,
    idle_seconds: float,
    reader: DarwinProcessReader | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    active_reader = reader or DarwinProcessReader()
    results: list[dict[str, Any]] = []
    for round_number in range(1, rounds + 1):
        pids = _role_pids(app, active_reader)
        before = {
            role: active_reader.usage(pid) if pid is not None else None
            for role, pid in pids.items()
        }
        started = time.monotonic()
        sleeper(idle_seconds)
        elapsed = max(time.monotonic() - started, 0.001)
        roles: list[dict[str, Any]] = []
        for role in ("status", "collector", "activity"):
            pid = pids[role]
            first = before[role]
            second = active_reader.usage(pid) if pid is not None else None
            if first is None or second is None:
                roles.append(_missing_role(role))
                continue
            roles.append(
                {
                    "role": role,
                    **delta_usage(first, second, elapsed_seconds=elapsed),
                }
            )
        results.append({"round": round_number, "roles": roles})
    return results


def summarize_idle(rounds: Iterable[dict[str, Any]]) -> dict[str, Any]:
    resident_rounds: list[dict[str, Any]] = []
    activity_rounds: list[dict[str, Any]] = []
    for item in rounds:
        by_role = {
            role["role"]: role for role in item.get("roles", []) if "role" in role
        }
        resident = [by_role.get("status"), by_role.get("collector")]
        if all(
            role is not None
            and role.get("cpuPercent") is not None
            and role.get("wakeupsPerSecond") is not None
            and role.get("footprintBytes") is not None
            for role in resident
        ):
            resident_rounds.append(
                {
                    "cpuPercent": sum(role["cpuPercent"] for role in resident),
                    "wakeupsPerSecond": sum(
                        role["wakeupsPerSecond"] for role in resident
                    ),
                    "footprintBytes": sum(
                        role["footprintBytes"] for role in resident
                    ),
                }
            )
        activity = by_role.get("activity")
        if activity is not None:
            activity_rounds.append(activity)

    resident = aggregate_rounds(resident_rounds)
    activity = aggregate_rounds(activity_rounds)
    return {
        "residentSampleCount": resident["sampleCount"],
        "residentCpuPercentMedian": resident["cpuPercentMedian"],
        "residentCpuPercentP95": resident["cpuPercentP95"],
        "residentWakeupsPerSecondP95": resident["wakeupsPerSecondP95"],
        "residentFootprintBytesPeak": resident["footprintBytesPeak"],
        "activitySampleCount": activity["sampleCount"],
        "activityCpuPercentP95": activity["cpuPercentP95"],
        "activityWakeupsPerSecondP95": activity["wakeupsPerSecondP95"],
        "activityFootprintBytesPeak": activity["footprintBytesPeak"],
    }


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def run_refresh(
    command: list[str], *, timeout_seconds: float
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        return {
            "durationSeconds": _rounded(time.monotonic() - started),
            "status": "unavailable",
            "exitCode": None,
        }
    try:
        return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_group(process)
        return {
            "durationSeconds": _rounded(time.monotonic() - started),
            "status": "timeout",
            "exitCode": None,
        }
    return {
        "durationSeconds": _rounded(time.monotonic() - started),
        "status": "success" if return_code == 0 else "failed",
        "exitCode": return_code,
    }


def summarize_refresh(rounds: Iterable[dict[str, Any]]) -> dict[str, Any]:
    values = list(rounds)
    successful = [
        float(item["durationSeconds"])
        for item in values
        if item.get("status") == "success"
    ]
    return {
        "sampleCount": len(successful),
        "durationSecondsMedian": (
            _rounded(statistics.median(successful)) if successful else None
        ),
        "durationSecondsP95": (
            _rounded(_nearest_rank_p95(successful)) if successful else None
        ),
        "timeoutCount": sum(item.get("status") == "timeout" for item in values),
        "failureCount": sum(item.get("status") == "failed" for item in values),
    }


def evaluate_budgets(
    observed: dict[str, Any], budgets: Iterable[Budget]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for budget in budgets:
        value = observed.get(budget.metric)
        if value is None:
            status = "unknown"
        elif budget.comparison == "maximum":
            status = "pass" if float(value) <= budget.threshold else "fail"
        else:
            status = "unknown"
        results.append(
            {
                "metric": budget.metric,
                "comparison": budget.comparison,
                "threshold": budget.threshold,
                "observed": value,
                "status": status,
            }
        )
    return results


def build_report(
    *,
    product_version: str,
    product_build: str,
    architecture: str,
    macos_version: str,
    app_bytes: int,
    idle_rounds: list[dict[str, Any]],
    idle_summary: dict[str, Any],
    refresh_rounds: list[dict[str, Any]],
    budget_results: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "product": {
            "version": product_version,
            "build": product_build,
            "architecture": architecture,
            "macOS": macos_version,
            "appBytes": app_bytes,
        },
        "idle": {"rounds": idle_rounds, "summary": idle_summary},
        "refresh": {
            "scope": "all-configured-sources",
            "perSourceTiming": "not_observable",
            "rounds": refresh_rounds,
            "summary": summarize_refresh(refresh_rounds),
        },
        "budgets": budget_results,
        "privacy": {
            "containsProcessIdentifiers": False,
            "containsLocalPaths": False,
            "containsProviderIdentity": False,
            "containsProviderPayloads": False,
        },
    }


def _bundle_metadata(app: Path) -> tuple[str, str]:
    try:
        with (app / "Contents/Info.plist").open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as error:
        raise ValueError("invalid app bundle") from error
    if payload.get("CFBundleIdentifier") != EXPECTED_BUNDLE_ID:
        raise ValueError("invalid app bundle")
    version = payload.get("CFBundleShortVersionString")
    build = payload.get("CFBundleVersion")
    if not isinstance(version, str) or not isinstance(build, str):
        raise ValueError("invalid app bundle")
    safe_component = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,31}$")
    if not safe_component.fullmatch(version) or not safe_component.fullmatch(build):
        raise ValueError("invalid app bundle")
    return version, build


def _logical_app_size(app: Path) -> int:
    total = 0
    for root, directories, files in os.walk(app, followlinks=False):
        directories[:] = [
            name for name in directories if not Path(root, name).is_symlink()
        ]
        for name in files:
            path = Path(root, name)
            try:
                if not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                raise ValueError("app size unavailable") from None
    return total


def _default_app() -> Path:
    system = Path("/Applications/OpenUsage Bar.app")
    return (
        system
        if system.is_dir()
        else Path.home() / "Applications/OpenUsage Bar.app"
    )


def _refresh_command(app: Path) -> list[str]:
    return [
        str(app / "Contents/MacOS/OpenUsage Collector"),
        "status",
        "--format",
        "json",
        "--fresh",
    ]


def _write_report(payload: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(rendered)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
        output.chmod(0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a privacy-safe OpenUsage Bar performance baseline."
    )
    parser.add_argument("--app", type=Path, default=_default_app())
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--idle-seconds", type=float, default=10)
    parser.add_argument("--refresh-timeout", type=float, default=95)
    parser.add_argument("--skip-refresh", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        validate_measurement_plan(
            rounds=args.rounds, idle_seconds=args.idle_seconds
        )
        if args.refresh_timeout < 1 or args.refresh_timeout > 180:
            raise ValueError("invalid refresh timeout")
        app = args.app.resolve(strict=True)
        version, build = _bundle_metadata(app)
        app_bytes = _logical_app_size(app)
        idle_rounds = measure_idle(
            app, rounds=args.rounds, idle_seconds=args.idle_seconds
        )
        idle_summary = summarize_idle(idle_rounds)
        refresh_rounds: list[dict[str, Any]] = []
        if not args.skip_refresh:
            collector = app / "Contents/MacOS/OpenUsage Collector"
            if not collector.is_file():
                raise ValueError("collector unavailable")
            command = _refresh_command(app)
            refresh_rounds = [
                {
                    "round": index,
                    **run_refresh(
                        command, timeout_seconds=args.refresh_timeout
                    ),
                }
                for index in range(1, args.rounds + 1)
            ]
        refresh_summary = summarize_refresh(refresh_rounds)
        observed = {
            "appBytes": app_bytes,
            **idle_summary,
            "refreshDurationSecondsP95": refresh_summary[
                "durationSecondsP95"
            ],
        }
        budget_results = evaluate_budgets(observed, DEFAULT_BUDGETS)
        report = build_report(
            product_version=version,
            product_build=build,
            architecture=platform.machine(),
            macos_version=platform.mac_ver()[0],
            app_bytes=app_bytes,
            idle_rounds=idle_rounds,
            idle_summary=idle_summary,
            refresh_rounds=refresh_rounds,
            budget_results=budget_results,
        )
        _write_report(report, args.output)
        return 0
    except (OSError, RuntimeError, ValueError):
        sys.stderr.write("performance measurement unavailable\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
