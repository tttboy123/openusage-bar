#!/usr/bin/env python3
"""Capture and verify privacy-safe evidence for a real macOS reboot recovery.

This tool never starts, refreshes, bootstraps, or reconfigures OpenUsage Bar. It
only reads bundle metadata, filtered launchd status, the private read-only API,
and aggregate SQLite cursors. A successful runtime check intentionally leaves
the visible menu-bar check pending for a human observer.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import plistlib
import re
import socket
import sqlite3
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


BASELINE_SCHEMA_VERSION = 2
API_SCHEMA_VERSION = "1.0"
APP_BUNDLE_ID = "com.lune.openusagebar"
STATUS_LABEL = "com.lune.openusagebar"
COLLECTOR_LABEL = "com.lune.openusagebar.collector"
MAX_REBOOT_START_DELAY_SECONDS = 6 * 60 * 60
MAX_REBOOT_VERIFY_DELAY_SECONDS = 6 * 60 * 60
SOURCE_CYCLE_WINDOW_SECONDS = 5 * 60
MAX_BASELINE_BYTES = 64 * 1024
MAX_API_BYTES = 1024 * 1024
MAX_COMMAND_BYTES = 64 * 1024
DEFAULT_STATE_DIR = Path.home() / ".local/state/openusage-bar"
DEFAULT_BASELINE = DEFAULT_STATE_DIR / "reboot-baseline.json"
DEFAULT_SOCKET = DEFAULT_STATE_DIR / "openusage.sock"
DEFAULT_LEDGER = DEFAULT_STATE_DIR / "activity.sqlite3"
DEFAULT_APP = Path("/Applications/OpenUsage Bar.app")
BOOT_TIME_PATTERN = re.compile(r"^\{\s*sec\s*=\s*([1-9][0-9]*),\s*usec\s*=\s*[0-9]+\s*\}")
ISO_TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$"
)
SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SIGNATURE_HASH_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")


@dataclass(frozen=True)
class RecoveryResult:
    ok: bool
    reason: str


class ProbeUnavailable(RuntimeError):
    """A privacy-safe runtime fact could not be collected."""


def _exact_keys(value: object, expected: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"invalid {name}")
    return value


def _integer(value: object, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"invalid {name}")
    if value < (1 if positive else 0):
        raise ValueError(f"invalid {name}")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 256:
        raise ValueError(f"invalid {name}")
    return value


def _timestamp(value: object, name: str) -> datetime:
    raw = _string(value, name)
    if not ISO_TIMESTAMP_PATTERN.fullmatch(raw):
        raise ValueError(f"invalid {name}")
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"invalid {name}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"invalid {name}")
    return parsed


def parse_boot_time(payload: str) -> int:
    if not isinstance(payload, str) or len(payload) > 256:
        raise ValueError("invalid boot time")
    match = BOOT_TIME_PATTERN.match(payload)
    if match is None:
        raise ValueError("invalid boot time")
    return _integer(int(match.group(1)), "boot time", positive=True)


def validate_baseline(payload: object) -> dict[str, Any]:
    baseline = _exact_keys(
        payload,
        {
            "schemaVersion",
            "capturedAt",
            "bootTimeSeconds",
            "app",
            "api",
            "ledger",
        },
        "baseline",
    )
    if baseline["schemaVersion"] != BASELINE_SCHEMA_VERSION:
        raise ValueError("invalid baseline schema")
    _timestamp(baseline["capturedAt"], "capture time")
    _integer(baseline["bootTimeSeconds"], "boot time", positive=True)

    app = _exact_keys(
        baseline["app"],
        {
            "bundleId",
            "version",
            "build",
            "signatureHash",
            "statusProgramHash",
            "collectorProgramHash",
        },
        "app",
    )
    if _string(app["bundleId"], "bundle id") != APP_BUNDLE_ID:
        raise ValueError("invalid bundle id")
    _string(app["version"], "app version")
    _string(app["build"], "app build")
    for field, label in (
        ("signatureHash", "signature hash"),
        ("statusProgramHash", "status program hash"),
        ("collectorProgramHash", "collector program hash"),
    ):
        signature_hash = _string(app[field], label)
        if not SIGNATURE_HASH_PATTERN.fullmatch(signature_hash):
            raise ValueError(f"invalid {label}")

    api = _exact_keys(baseline["api"], {"schemaVersion", "dataRevision"}, "api")
    if _string(api["schemaVersion"], "API schema") != API_SCHEMA_VERSION:
        raise ValueError("invalid API schema")
    _integer(api["dataRevision"], "API revision")

    ledger = _exact_keys(baseline["ledger"], {"changeSeq", "sourceAttempts"}, "ledger")
    _integer(ledger["changeSeq"], "change sequence")
    source_attempts = ledger["sourceAttempts"]
    if not isinstance(source_attempts, dict) or not 1 <= len(source_attempts) <= 64:
        raise ValueError("invalid source attempts")
    for source_id, attempt in source_attempts.items():
        if not isinstance(source_id, str) or not SOURCE_ID_PATTERN.fullmatch(source_id):
            raise ValueError("invalid source id")
        _timestamp(attempt, "last attempt time")
    return baseline


def write_baseline(destination: Path, payload: object) -> None:
    baseline = validate_baseline(payload)
    destination = Path(destination)
    if not destination.is_absolute() or "\x00" in str(destination):
        raise ValueError("baseline path must be absolute")
    if destination.is_symlink():
        raise ValueError("baseline path must not be a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (json.dumps(baseline, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def load_baseline(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("invalid baseline path")
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("invalid baseline permissions")
        if metadata.st_size <= 0 or metadata.st_size > MAX_BASELINE_BYTES:
            raise ValueError("invalid baseline size")
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid baseline") from error
    return validate_baseline(payload)


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"invalid {name}")
    return value


def evaluate_recovery(
    baseline_payload: object,
    current_payload: object,
    *,
    evaluated_at: datetime | None = None,
) -> RecoveryResult:
    baseline = validate_baseline(baseline_payload)
    current = _mapping(current_payload, "runtime snapshot")

    try:
        current_boot = _integer(current.get("bootTimeSeconds"), "boot time", positive=True)
    except ValueError:
        return RecoveryResult(False, "boot_time_invalid")
    if current_boot <= baseline["bootTimeSeconds"]:
        return RecoveryResult(False, "boot_unchanged")
    current_boot_at = datetime.fromtimestamp(current_boot, timezone.utc)
    captured_at = _timestamp(baseline["capturedAt"], "capture time")
    if current_boot_at <= captured_at:
        return RecoveryResult(False, "reboot_before_baseline")
    if (
        current_boot_at - captured_at
    ).total_seconds() > MAX_REBOOT_START_DELAY_SECONDS:
        return RecoveryResult(False, "baseline_too_old")
    verification_time = evaluated_at or datetime.now(timezone.utc)
    if not isinstance(verification_time, datetime) or verification_time.tzinfo is None:
        return RecoveryResult(False, "verification_time_invalid")
    verification_time = verification_time.astimezone(timezone.utc)
    if verification_time < current_boot_at:
        return RecoveryResult(False, "verification_before_boot")
    if (
        verification_time - current_boot_at
    ).total_seconds() > MAX_REBOOT_VERIFY_DELAY_SECONDS:
        return RecoveryResult(False, "verification_too_late")
    if current.get("signatureOk") is not True:
        return RecoveryResult(False, "signature_invalid")
    if current.get("app") != baseline["app"]:
        return RecoveryResult(False, "app_metadata_changed")

    launch_agents = current.get("launchAgents")
    if not isinstance(launch_agents, dict):
        return RecoveryResult(False, "launch_agent_invalid")
    for label in (STATUS_LABEL, COLLECTOR_LABEL):
        agent = launch_agents.get(label)
        if not isinstance(agent, dict) or any(
            agent.get(field) is not True
            for field in (
                "running",
                "runAtLoad",
                "keepAlive",
                "programMatches",
                "startedAfterBoot",
            )
        ):
            return RecoveryResult(False, "launch_agent_invalid")

    socket_state = current.get("socket")
    if not isinstance(socket_state, dict) or socket_state.get("isSocket") is not True:
        return RecoveryResult(False, "socket_unavailable")
    if socket_state.get("mode") != 0o600:
        return RecoveryResult(False, "socket_permissions_invalid")
    if socket_state.get("ownerMatches") is not True:
        return RecoveryResult(False, "socket_owner_invalid")

    api = current.get("api")
    if not isinstance(api, dict) or api.get("healthOk") is not True:
        return RecoveryResult(False, "local_api_unhealthy")
    if api.get("schemaVersion") != baseline["api"]["schemaVersion"]:
        return RecoveryResult(False, "api_schema_changed")
    try:
        api_revision = _integer(api.get("dataRevision"), "API revision")
    except ValueError:
        return RecoveryResult(False, "api_revision_invalid")
    if api_revision < baseline["api"]["dataRevision"]:
        return RecoveryResult(False, "api_revision_regressed")

    ledger = current.get("ledger")
    if not isinstance(ledger, dict) or ledger.get("quickCheck") != "ok":
        return RecoveryResult(False, "ledger_integrity_failed")
    try:
        change_sequence = _integer(ledger.get("changeSeq"), "change sequence")
    except ValueError:
        return RecoveryResult(False, "ledger_revision_invalid")
    if change_sequence < baseline["ledger"]["changeSeq"]:
        return RecoveryResult(False, "ledger_revision_regressed")
    source_status = ledger.get("sourceStatus")
    if not isinstance(source_status, dict):
        return RecoveryResult(False, "direct_source_missing")
    for source_id, previous_raw in baseline["ledger"]["sourceAttempts"].items():
        current_source = source_status.get(source_id)
        if not isinstance(current_source, dict):
            return RecoveryResult(False, "direct_source_missing")
        if current_source.get("state") != "ok":
            return RecoveryResult(False, "direct_source_unhealthy")
        try:
            previous_attempt = _timestamp(previous_raw, "baseline attempt time")
            current_attempt = _timestamp(
                current_source.get("lastAttemptAt"), "current attempt time"
            )
            current_success = _timestamp(
                current_source.get("lastSuccessAt"), "current success time"
            )
        except ValueError:
            return RecoveryResult(False, "scheduled_collection_time_invalid")
        if current_success != current_attempt:
            return RecoveryResult(False, "direct_source_unhealthy")
        if current_attempt <= previous_attempt:
            return RecoveryResult(False, "scheduled_collection_not_advanced")
        if current_attempt <= current_boot_at:
            return RecoveryResult(False, "scheduled_collection_before_boot")
    return RecoveryResult(True, "ok")


def _run(command: list[str], *, timeout: float = 10.0) -> str:
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(Path.home())},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProbeUnavailable("command unavailable") from error
    if result.returncode != 0 or len(result.stdout) > MAX_COMMAND_BYTES:
        raise ProbeUnavailable("command failed")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeError as error:
        raise ProbeUnavailable("command output invalid") from error


def _bundle_metadata(app: Path) -> dict[str, str]:
    info_path = app / "Contents/Info.plist"
    try:
        if info_path.is_symlink() or info_path.stat().st_size > MAX_BASELINE_BYTES:
            raise ProbeUnavailable("bundle metadata unavailable")
        payload = plistlib.loads(info_path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as error:
        raise ProbeUnavailable("bundle metadata unavailable") from error
    if not isinstance(payload, dict):
        raise ProbeUnavailable("bundle metadata unavailable")
    metadata = {
        "bundleId": payload.get("CFBundleIdentifier"),
        "version": payload.get("CFBundleShortVersionString"),
        "build": payload.get("CFBundleVersion"),
    }
    try:
        _exact_keys(metadata, {"bundleId", "version", "build"}, "app")
        if _string(metadata["bundleId"], "bundle id") != APP_BUNDLE_ID:
            raise ValueError("invalid bundle id")
        _string(metadata["version"], "app version")
        _string(metadata["build"], "app build")
    except ValueError as error:
        raise ProbeUnavailable("bundle metadata unavailable") from error
    return metadata


def _signature_ok(app: Path) -> bool:
    try:
        _run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])
    except ProbeUnavailable:
        return False
    return True


def _signature_hash(app: Path) -> str:
    filter_script = r'''
set -o pipefail
/usr/bin/codesign -dv --verbose=4 "$1" 2>&1 | /usr/bin/awk '
  /^CDHash=[0-9a-f]+$/ { print; matches += 1 }
  END { if (matches != 1) exit 1 }
'
'''
    payload = _run(
        ["/bin/zsh", "-c", filter_script, "reboot-recovery", str(app)], timeout=5
    ).strip()
    prefix = "CDHash="
    if not payload.startswith(prefix):
        raise ProbeUnavailable("signature hash unavailable")
    signature_hash = payload[len(prefix) :]
    if not SIGNATURE_HASH_PATTERN.fullmatch(signature_hash):
        raise ProbeUnavailable("signature hash unavailable")
    return signature_hash


def _filtered_launchctl(label: str) -> str:
    domain = f"gui/{os.getuid()}/{label}"
    filter_script = r'''
set -o pipefail
/bin/launchctl print "$1" 2>/dev/null | /usr/bin/awk '
  /^[[:space:]]*state = / ||
  /^[[:space:]]*program = / ||
  /^[[:space:]]*runs = / ||
  /^[[:space:]]*pid = / ||
  /^[[:space:]]*last exit code = / { print }
'
'''
    return _run(["/bin/zsh", "-c", filter_script, "reboot-recovery", domain], timeout=5)


def _launch_agent_state(
    label: str,
    expected_program: Path,
    launch_agents_directory: Path,
    boot_time: int,
) -> dict[str, bool]:
    plist_path = launch_agents_directory / f"{label}.plist"
    try:
        if plist_path.is_symlink() or plist_path.stat().st_size > MAX_BASELINE_BYTES:
            raise ProbeUnavailable("launch agent unavailable")
        payload = plistlib.loads(plist_path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as error:
        raise ProbeUnavailable("launch agent unavailable") from error
    if not isinstance(payload, dict):
        raise ProbeUnavailable("launch agent unavailable")
    arguments = payload.get("ProgramArguments")
    configured_program = arguments[0] if isinstance(arguments, list) and arguments else None
    filtered = _filtered_launchctl(label)
    running = re.search(r"(?m)^\s*state = running\s*$", filtered) is not None
    runtime_program = re.search(r"(?m)^\s*program = (.+?)\s*$", filtered)
    pid_match = re.search(r"(?m)^\s*pid = ([1-9][0-9]*)\s*$", filtered)
    pid = int(pid_match.group(1)) if pid_match is not None else None
    return {
        "running": running and pid is not None,
        "runAtLoad": payload.get("RunAtLoad") is True,
        "keepAlive": payload.get("KeepAlive") is True,
        "programMatches": payload.get("Label") == label
        and configured_program == str(expected_program)
        and runtime_program is not None
        and runtime_program.group(1) == str(expected_program),
        "startedAfterBoot": pid is not None and _process_started_after_boot(pid, boot_time),
    }


def _socket_state(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except OSError:
        return {"isSocket": False, "mode": None, "ownerMatches": False}
    return {
        "isSocket": stat.S_ISSOCK(metadata.st_mode),
        "mode": stat.S_IMODE(metadata.st_mode),
        "ownerMatches": metadata.st_uid == os.getuid(),
    }


def _process_started_after_boot(pid: int, boot_time: int) -> bool:
    try:
        raw = _run(["/bin/ps", "-p", str(pid), "-o", "lstart="], timeout=5).strip()
        normalized = " ".join(raw.split())
        started = datetime.strptime(normalized, "%a %b %d %H:%M:%S %Y")
        started_seconds = int(time.mktime(started.timetuple()))
    except (ProbeUnavailable, ValueError, OverflowError):
        return False
    return started_seconds >= boot_time


def _api_get(socket_path: Path, route: str) -> dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(3)
    payload = bytearray()
    try:
        client.connect(str(socket_path))
        client.sendall(
            f"GET {route} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode(
                "ascii"
            )
        )
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > MAX_API_BYTES:
                raise ProbeUnavailable("local API response too large")
    except OSError as error:
        raise ProbeUnavailable("local API unavailable") from error
    finally:
        client.close()
    try:
        head, body = bytes(payload).split(b"\r\n\r\n", 1)
        if b" 200 " not in head.split(b"\r\n", 1)[0]:
            raise ProbeUnavailable("local API unavailable")
        decoded = json.loads(body)
    except (ValueError, json.JSONDecodeError) as error:
        raise ProbeUnavailable("local API invalid") from error
    if not isinstance(decoded, dict):
        raise ProbeUnavailable("local API invalid")
    return decoded


def _api_state(socket_path: Path) -> dict[str, object]:
    health_payload = _api_get(socket_path, "/v1/health")
    schema_payload = _api_get(socket_path, "/v1/schema")
    summary_payload = _api_get(socket_path, "/v1/summary")
    contract_ok = (
        health_payload.get("schemaVersion") == API_SCHEMA_VERSION
        and health_payload.get("health") == {"ok": True, "status": "ok"}
        and schema_payload.get("schemaVersion") == API_SCHEMA_VERSION
        and isinstance(schema_payload.get("routes"), list)
        and summary_payload.get("schemaVersion") == API_SCHEMA_VERSION
        and "todayTokens" in summary_payload
        and isinstance(summary_payload.get("dataRevision"), int)
        and not isinstance(summary_payload.get("dataRevision"), bool)
    )
    return {
        "healthOk": contract_ok,
        "schemaVersion": summary_payload.get("schemaVersion"),
        "dataRevision": summary_payload.get("dataRevision"),
    }


def _ledger_state(path: Path) -> dict[str, object]:
    connection: sqlite3.Connection | None = None
    try:
        uri = f"file:{quote(str(path.absolute()), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        connection.execute("PRAGMA query_only=ON")
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        change_sequence = connection.execute(
            "SELECT COALESCE(MAX(change_seq),0) FROM change_log"
        ).fetchone()
        source_rows = connection.execute(
            "SELECT source_id,state,last_attempt_at,last_success_at "
            "FROM source_status ORDER BY source_id,provider_id"
        ).fetchall()
    except (OSError, sqlite3.Error) as error:
        raise ProbeUnavailable("ledger unavailable") from error
    finally:
        if connection is not None:
            connection.close()
    if (
        quick_check is None
        or change_sequence is None
        or not isinstance(change_sequence[0], int)
    ):
        raise ProbeUnavailable("ledger unavailable")
    grouped: dict[str, list[tuple[object, object, object]]] = {}
    for source_id, state_value, attempt, success in source_rows:
        if not isinstance(source_id, str) or not SOURCE_ID_PATTERN.fullmatch(source_id):
            raise ProbeUnavailable("ledger unavailable")
        grouped.setdefault(source_id, []).append((state_value, attempt, success))
    source_status: dict[str, dict[str, object]] = {}
    for source_id, rows in grouped.items():
        attempts = [row[1] for row in rows]
        successes = [row[2] for row in rows]
        if not all(isinstance(value, str) for value in attempts):
            raise ProbeUnavailable("ledger unavailable")
        valid_successes = [value for value in successes if isinstance(value, str)]
        source_status[source_id] = {
            "state": "ok"
            if all(row[0] == "ok" for row in rows) and len(valid_successes) == len(rows)
            else "error",
            "lastAttemptAt": min(attempts),
            "lastSuccessAt": min(valid_successes) if valid_successes else None,
        }
    return {
        "quickCheck": quick_check[0],
        "changeSeq": change_sequence[0],
        "sourceStatus": source_status,
    }


def probe_runtime(
    *, app: Path, socket_path: Path, ledger: Path, launch_agents_directory: Path
) -> dict[str, object]:
    status_program = app / "Contents/MacOS/OpenUsage Bar"
    collector_program = (
        app
        / "Contents/Helpers/OpenUsage Provider Settings.app/Contents/MacOS"
        / "OpenUsage Provider Settings"
    )
    boot_time = parse_boot_time(_run(["/usr/sbin/sysctl", "-n", "kern.boottime"]))
    socket_state = _socket_state(socket_path)
    if (
        socket_state.get("isSocket") is not True
        or socket_state.get("mode") != 0o600
        or socket_state.get("ownerMatches") is not True
    ):
        raise ProbeUnavailable("socket invalid")
    app_state = dict(_bundle_metadata(app))
    app_state["signatureHash"] = _signature_hash(app)
    app_state["statusProgramHash"] = _signature_hash(status_program)
    app_state["collectorProgramHash"] = _signature_hash(collector_program)
    return {
        "bootTimeSeconds": boot_time,
        "signatureOk": _signature_ok(app),
        "app": app_state,
        "launchAgents": {
            STATUS_LABEL: _launch_agent_state(
                STATUS_LABEL, status_program, launch_agents_directory, boot_time
            ),
            COLLECTOR_LABEL: _launch_agent_state(
                COLLECTOR_LABEL, collector_program, launch_agents_directory, boot_time
            ),
        },
        "socket": socket_state,
        "api": _api_state(socket_path),
        "ledger": _ledger_state(ledger),
    }


def baseline_from_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    app = _mapping(snapshot.get("app"), "app")
    api = _mapping(snapshot.get("api"), "api")
    ledger = _mapping(snapshot.get("ledger"), "ledger")
    if snapshot.get("signatureOk") is not True:
        raise ProbeUnavailable("signature invalid")
    launch_agents = snapshot.get("launchAgents")
    if not isinstance(launch_agents, dict) or any(
        not isinstance(launch_agents.get(label), dict)
        or any(
            launch_agents[label].get(field) is not True
            for field in (
                "running",
                "runAtLoad",
                "keepAlive",
                "programMatches",
                "startedAfterBoot",
            )
        )
        for label in (STATUS_LABEL, COLLECTOR_LABEL)
    ):
        raise ProbeUnavailable("launch agent invalid")
    socket_state = _mapping(snapshot.get("socket"), "socket")
    if (
        socket_state.get("isSocket") is not True
        or socket_state.get("mode") != 0o600
        or socket_state.get("ownerMatches") is not True
    ):
        raise ProbeUnavailable("socket invalid")
    if api.get("healthOk") is not True:
        raise ProbeUnavailable("local API unhealthy")
    if ledger.get("quickCheck") != "ok":
        raise ProbeUnavailable("ledger integrity failed")
    source_status = ledger.get("sourceStatus")
    if not isinstance(source_status, dict):
        raise ProbeUnavailable("source status unavailable")
    healthy_attempts: dict[str, tuple[str, datetime]] = {}
    for source_id, status_payload in source_status.items():
        if (
            not isinstance(source_id, str)
            or not isinstance(status_payload, dict)
            or status_payload.get("state") != "ok"
            or status_payload.get("lastAttemptAt")
            != status_payload.get("lastSuccessAt")
            or not isinstance(status_payload.get("lastAttemptAt"), str)
        ):
            continue
        attempt = status_payload["lastAttemptAt"]
        try:
            parsed_attempt = _timestamp(attempt, "source attempt time")
        except ValueError as error:
            raise ProbeUnavailable("source status unavailable") from error
        healthy_attempts[source_id] = (attempt, parsed_attempt)
    if not healthy_attempts:
        raise ProbeUnavailable("source status unavailable")
    newest_attempt = max(parsed for _, parsed in healthy_attempts.values())
    cycle_attempts = {
        source_id: raw_attempt
        for source_id, (raw_attempt, parsed_attempt) in healthy_attempts.items()
        if (newest_attempt - parsed_attempt).total_seconds()
        <= SOURCE_CYCLE_WINDOW_SECONDS
    }
    baseline = {
        "schemaVersion": BASELINE_SCHEMA_VERSION,
        "capturedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "bootTimeSeconds": snapshot.get("bootTimeSeconds"),
        "app": app,
        "api": {
            "schemaVersion": api.get("schemaVersion"),
            "dataRevision": api.get("dataRevision"),
        },
        "ledger": {
            "changeSeq": ledger.get("changeSeq"),
            "sourceAttempts": cycle_attempts,
        },
    }
    return validate_baseline(baseline)


def _absolute_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or "\x00" in str(path):
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture or verify privacy-safe OpenUsage Bar reboot evidence."
    )
    parser.add_argument("mode", choices=("capture", "verify"))
    parser.add_argument("--baseline", type=_absolute_path, default=DEFAULT_BASELINE)
    parser.add_argument("--app", type=_absolute_path, default=DEFAULT_APP)
    parser.add_argument("--socket", type=_absolute_path, default=DEFAULT_SOCKET)
    parser.add_argument("--ledger", type=_absolute_path, default=DEFAULT_LEDGER)
    parser.add_argument("--timeout", type=float, default=360.0)
    arguments = parser.parse_args()
    if not math.isfinite(arguments.timeout) or not 0 <= arguments.timeout <= 600:
        parser.error("timeout must be between 0 and 600 seconds")
    return arguments


def main() -> int:
    arguments = _arguments()
    launch_agents_directory = Path.home() / "Library/LaunchAgents"
    if arguments.mode == "capture":
        try:
            snapshot = probe_runtime(
                app=arguments.app,
                socket_path=arguments.socket,
                ledger=arguments.ledger,
                launch_agents_directory=launch_agents_directory,
            )
            baseline = baseline_from_snapshot(snapshot)
            write_baseline(arguments.baseline, baseline)
        except (OSError, ValueError, ProbeUnavailable):
            print("reboot_baseline_failed", flush=True)
            return 1
        print(
            "reboot_baseline_ok "
            f"version={baseline['app']['version']} "
            f"build={baseline['app']['build']} "
            f"dataRevision={baseline['api']['dataRevision']}",
            flush=True,
        )
        return 0

    try:
        baseline = load_baseline(arguments.baseline)
        current_boot = parse_boot_time(
            _run(["/usr/sbin/sysctl", "-n", "kern.boottime"])
        )
    except (OSError, ValueError, ProbeUnavailable):
        print("reboot_recovery_failed reason=baseline_or_boot_unavailable", flush=True)
        return 1
    if current_boot <= baseline["bootTimeSeconds"]:
        print("reboot_recovery_failed reason=boot_unchanged", flush=True)
        return 1

    deadline = time.monotonic() + arguments.timeout
    last_reason = "runtime_unavailable"
    while True:
        try:
            snapshot = probe_runtime(
                app=arguments.app,
                socket_path=arguments.socket,
                ledger=arguments.ledger,
                launch_agents_directory=launch_agents_directory,
            )
            result = evaluate_recovery(baseline, snapshot)
            last_reason = result.reason
            if result.ok:
                print(
                    "reboot_recovery_ok "
                    f"version={snapshot['app']['version']} "
                    f"build={snapshot['app']['build']} "
                    f"dataRevision={snapshot['api']['dataRevision']} "
                    "scheduledCollectionAdvanced=1 visualMenuCheck=pending",
                    flush=True,
                )
                return 0
        except (OSError, ValueError, ProbeUnavailable):
            last_reason = "runtime_unavailable"
        if time.monotonic() >= deadline:
            print(f"reboot_recovery_failed reason={last_reason}", flush=True)
            return 1
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))


if __name__ == "__main__":
    raise SystemExit(main())
