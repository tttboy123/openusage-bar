#!/usr/bin/env python3
"""Verify one revision across the installed CLI and private Local API."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import signal
import socket
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


API_SCHEMA = "1.0"
REPORT_SCHEMA = "openusage-canary-surfaces-1"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ATTEMPTS = 5
CLI_TIMEOUT_SECONDS = 10


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("invalid snapshot")
    return value


def _revision(snapshot: dict[str, Any]) -> int:
    revision = snapshot.get("dataRevision")
    if (
        snapshot.get("schemaVersion") != API_SCHEMA
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
    ):
        raise ValueError("invalid snapshot")
    generated_at = snapshot.get("generatedAt")
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("invalid snapshot")
    return revision


def _timestamp(value: str) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid capture time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid capture time") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid capture time")
    normalized = parsed.astimezone(timezone.utc)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


def _product(value: dict[str, str]) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "build"}
        or not isinstance(value.get("version"), str)
        or not isinstance(value.get("build"), str)
        or not value["version"]
        or len(value["version"]) > 32
        or not value["build"].isdigit()
        or not 1 <= len(value["build"]) <= 12
    ):
        raise ValueError("invalid product")
    return {"build": value["build"], "version": value["version"]}


def _semantic_bytes(snapshot: dict[str, Any]) -> bytes:
    semantic = {key: value for key, value in snapshot.items() if key != "generatedAt"}
    for collection in ("balances", "quotaWindows"):
        rows = semantic.get(collection)
        if isinstance(rows, list):
            semantic[collection] = [
                {
                    key: value
                    for key, value in row.items()
                    if key != "freshnessSeconds"
                }
                if isinstance(row, dict)
                else row
                for row in rows
            ]
    try:
        return json.dumps(
            semantic,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("invalid snapshot") from error


def _validate_report(report: dict[str, Any]) -> None:
    if set(report) != {
        "capturedAt", "checks", "dataRevision", "product", "schemaVersion",
    }:
        raise ValueError("invalid report")
    if report.get("schemaVersion") != REPORT_SCHEMA:
        raise ValueError("invalid report")
    _timestamp(report.get("capturedAt"))
    revision = report.get("dataRevision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("invalid report")
    _product(report.get("product"))
    if report.get("checks") != {
        "apiCliSnapshot": "pass",
        "sourceHealthAgreement": "pass",
        "visualMenu": "pending_manual",
    }:
        raise ValueError("invalid report")


def verify_surfaces(
    *,
    api_get: Callable[[], dict[str, Any]],
    cli_get: Callable[[], dict[str, Any]],
    product: dict[str, str],
    captured_at: str,
    attempts: int = 3,
) -> dict[str, Any]:
    if (
        isinstance(attempts, bool)
        or not isinstance(attempts, int)
        or not 1 <= attempts <= MAX_ATTEMPTS
    ):
        raise ValueError("invalid attempts")
    normalized_product = _product(product)
    normalized_time = _timestamp(captured_at)
    for _ in range(attempts):
        api_before = _mapping(api_get())
        cli_snapshot = _mapping(cli_get())
        api_after = _mapping(api_get())
        revisions = (
            _revision(api_before),
            _revision(cli_snapshot),
            _revision(api_after),
        )
        if len(set(revisions)) != 1:
            continue
        semantic = (
            _semantic_bytes(api_before),
            _semantic_bytes(cli_snapshot),
            _semantic_bytes(api_after),
        )
        if semantic[0] != semantic[1] or semantic[1] != semantic[2]:
            raise ValueError("surface mismatch")
        report = {
            "capturedAt": normalized_time,
            "checks": {
                "apiCliSnapshot": "pass",
                "sourceHealthAgreement": "pass",
                "visualMenu": "pending_manual",
            },
            "dataRevision": revisions[0],
            "product": normalized_product,
            "schemaVersion": REPORT_SCHEMA,
        }
        _validate_report(report)
        return report
    raise ValueError("revision did not stabilize")


def _socket_path(path: Path) -> Path:
    if not path.is_absolute() or "\x00" in str(path):
        raise ValueError("invalid socket")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError("invalid socket") from error
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ValueError("invalid socket")
    return path


def get_api_snapshot(path: Path) -> dict[str, Any]:
    resolved = _socket_path(path)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(3)
    try:
        client.connect(str(resolved))
        client.sendall(
            b"GET /v1/snapshot HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Connection: close\r\n\r\n"
        )
        chunks: list[bytes] = []
        size = 0
        while chunk := client.recv(65_536):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ValueError("invalid response")
            chunks.append(chunk)
    finally:
        client.close()
    response = b"".join(chunks)
    head, separator, body = response.partition(b"\r\n\r\n")
    if not separator or not head.startswith(b"HTTP/1.1 200 "):
        raise ValueError("invalid response")
    try:
        return _mapping(json.loads(body))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError("invalid response") from error


def _kill_and_reap(process: subprocess.Popen[Any]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        process.wait()


def _child_environment() -> dict[str, str]:
    environment = {"PATH": "/usr/bin:/bin"}
    for name in ("HOME", "USER", "LOGNAME", "TMPDIR"):
        value = os.environ.get(name)
        if value and "\x00" not in value:
            environment[name] = value
    return environment


def get_cli_snapshot(collector: Path) -> dict[str, Any]:
    if (
        not collector.is_absolute()
        or "\x00" in str(collector)
        or collector.is_symlink()
        or not collector.is_file()
        or not os.access(collector, os.X_OK)
    ):
        raise ValueError("invalid collector")
    with tempfile.TemporaryFile() as output:
        process: subprocess.Popen[Any] | None = None
        try:
            process = subprocess.Popen(
                [str(collector), "snapshot", "--format", "json", "--offline"],
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                env=_child_environment(),
                start_new_session=True,
            )
            try:
                returncode = process.wait(timeout=CLI_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as error:
                _kill_and_reap(process)
                raise ValueError("collector unavailable") from error
            if returncode != 0:
                raise ValueError("collector unavailable")
            size = output.tell()
            if not 0 < size <= MAX_RESPONSE_BYTES:
                raise ValueError("collector unavailable")
            output.seek(0)
            return _mapping(json.load(output))
        except (OSError, json.JSONDecodeError, UnicodeError) as error:
            if process is not None:
                _kill_and_reap(process)
            raise ValueError("collector unavailable") from error
        finally:
            if process is not None and process.poll() is None:
                _kill_and_reap(process)


def verify_app_signature(
    app: Path,
    *,
    popen: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
) -> None:
    process: subprocess.Popen[Any] | None = None
    try:
        process = popen(
            [
                "/usr/bin/codesign",
                "--verify",
                "--deep",
                "--strict",
                str(app),
            ],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"},
            start_new_session=True,
        )
        try:
            returncode = process.wait(timeout=CLI_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            _kill_and_reap(process)
            raise ValueError("invalid app") from error
        if returncode != 0:
            raise ValueError("invalid app")
    except OSError as error:
        if process is not None:
            _kill_and_reap(process)
        raise ValueError("invalid app") from error
    finally:
        if process is not None and process.poll() is None:
            _kill_and_reap(process)


def installed_product(app: Path) -> tuple[dict[str, str], Path]:
    if (
        not app.is_absolute()
        or "\x00" in str(app)
        or app.is_symlink()
        or not app.is_dir()
    ):
        raise ValueError("invalid app")
    verify_app_signature(app)
    info = app / "Contents/Info.plist"
    collector = app / "Contents/MacOS/OpenUsage Collector"
    try:
        with info.open("rb") as handle:
            metadata = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as error:
        raise ValueError("invalid app") from error
    if metadata.get("CFBundleIdentifier") != "com.lune.openusagebar":
        raise ValueError("invalid app")
    product = _product({
        "version": metadata.get("CFBundleShortVersionString"),
        "build": metadata.get("CFBundleVersion"),
    })
    if (
        collector.is_symlink()
        or not collector.is_file()
        or not os.access(collector, os.X_OK)
    ):
        raise ValueError("invalid app")
    return product, collector


def write_private(path: Path, payload: dict[str, Any]) -> None:
    _validate_report(payload)
    if (
        not path.is_absolute()
        or "\x00" in str(path)
        or not path.parent.is_dir()
    ):
        raise ValueError("invalid output")
    encoded = (
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify installed CLI and Local API snapshot agreement."
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--socket",
        type=Path,
        default=Path.home() / ".local/state/openusage-bar/openusage.sock",
    )
    parser.add_argument(
        "--app",
        type=Path,
        default=Path("/Applications/OpenUsage Bar.app"),
    )
    parser.add_argument("--attempts", type=int, default=3)
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.attempts <= MAX_ATTEMPTS:
        parser.error(f"--attempts must be between 1 and {MAX_ATTEMPTS}")
    return arguments


def main(argv: list[str] | None = None) -> int:
    arguments = _arguments(argv)
    try:
        app = arguments.app
        default_app = Path("/Applications/OpenUsage Bar.app")
        home_app = Path.home() / "Applications/OpenUsage Bar.app"
        if app == default_app and not app.is_dir() and home_app.is_dir():
            app = home_app
        product, collector = installed_product(app)
        report = verify_surfaces(
            api_get=lambda: get_api_snapshot(arguments.socket),
            cli_get=lambda: get_cli_snapshot(collector),
            product=product,
            captured_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            attempts=arguments.attempts,
        )
        write_private(arguments.output, report)
    except (OSError, ValueError):
        print("canary surface verification unavailable", file=os.sys.stderr)
        return 1
    print(
        "canary_surface_verification_passed=1 "
        "visual_menu_check=pending_manual"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
