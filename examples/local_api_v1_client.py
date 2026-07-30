#!/usr/bin/env python3
"""Minimal bounded reader for OpenUsage Bar Local API v1."""

from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import sys
from datetime import date
from pathlib import Path
from typing import Any


DEFAULT_SOCKET = Path.home() / ".local/state/openusage-bar/openusage.sock"
DEFAULT_TIMEOUT_SECONDS = 3.0
MAXIMUM_BODY_BYTES = 1_048_576
MAXIMUM_HEADER_BYTES = 16_384


class LocalAPIClientError(Exception):
    """A fixed, sanitized client failure safe for CLI output."""


def _fail(code: str) -> None:
    raise LocalAPIClientError(code)


def _validate_socket(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        _fail("unavailable")
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        _fail("unavailable")


def _split_response(data: bytes) -> tuple[int, dict[str, str], bytes]:
    header, separator, body = data.partition(b"\r\n\r\n")
    if not separator:
        _fail("invalid_response")
    try:
        lines = header.decode("ascii").split("\r\n")
    except UnicodeDecodeError:
        _fail("invalid_response")
    if not lines or not lines[0].startswith("HTTP/1.1 "):
        _fail("invalid_response")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        _fail("invalid_response")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, marker, value = line.partition(":")
        normalized = name.strip().lower()
        if (
            not marker
            or not normalized
            or normalized in headers
            or any(character in value for character in "\r\n")
        ):
            _fail("invalid_response")
        headers[normalized] = value.strip()
    return int(parts[1]), headers, body


def _read_response(connection: socket.socket, maximum_body_bytes: int) -> bytes:
    received = bytearray()
    while b"\r\n\r\n" not in received:
        if len(received) >= MAXIMUM_HEADER_BYTES:
            _fail("invalid_response")
        chunk = connection.recv(min(4096, MAXIMUM_HEADER_BYTES - len(received)))
        if not chunk:
            _fail("invalid_response")
        received.extend(chunk)
    status, headers, body = _split_response(bytes(received))
    if status != 200 or "transfer-encoding" in headers:
        _fail("invalid_response")
    raw_length = headers.get("content-length")
    if raw_length is None or not raw_length.isascii() or not raw_length.isdigit():
        _fail("invalid_response")
    content_length = int(raw_length)
    if content_length > maximum_body_bytes:
        _fail("response_too_large")
    if len(body) > content_length:
        _fail("invalid_response")
    buffered_body = bytearray(body)
    while len(buffered_body) < content_length:
        chunk = connection.recv(
            min(65_536, content_length - len(buffered_body))
        )
        if not chunk:
            _fail("invalid_response")
        buffered_body.extend(chunk)
    return bytes(buffered_body)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _project_snapshot(payload: Any) -> dict[str, object]:
    if not isinstance(payload, dict) or payload.get("schemaVersion") != "1.0":
        _fail("schema_mismatch")
    revision = payload.get("dataRevision")
    generated_at = payload.get("generatedAt")
    local_day = payload.get("localDay")
    summary = payload.get("summary")
    if (
        not _integer(revision)
        or not isinstance(generated_at, str)
        or not 1 <= len(generated_at) <= 64
        or not isinstance(local_day, str)
        or len(local_day) != 10
        or not isinstance(summary, dict)
    ):
        _fail("invalid_response")
    today_tokens = summary.get("todayTokens")
    model_count = summary.get("modelCount")
    covered_day_count = summary.get("coveredDayCount")
    if (
        (today_tokens is not None and not _integer(today_tokens))
        or not _integer(model_count)
        or not _integer(covered_day_count)
    ):
        _fail("invalid_response")
    if today_tokens is None:
        if model_count != 0 or covered_day_count != 0:
            _fail("invalid_response")
    elif model_count == 0 and not (today_tokens == 0 and covered_day_count > 0):
        _fail("invalid_response")
    try:
        if date.fromisoformat(local_day).isoformat() != local_day:
            _fail("invalid_response")
    except ValueError:
        _fail("invalid_response")
    for field in ("quotaWindows", "providers", "sources"):
        values = payload.get(field)
        if not isinstance(values, list) or len(values) > 10_000:
            _fail("invalid_response")
    return {
        "schemaVersion": "1.0",
        "dataRevision": revision,
        "localDay": local_day,
        "summary": {
            "todayTokens": today_tokens,
            "modelCount": model_count,
            "coveredDayCount": covered_day_count,
        },
    }


def read_snapshot(
    socket_path: Path,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    maximum_body_bytes: int = MAXIMUM_BODY_BYTES,
) -> dict[str, object]:
    if (
        isinstance(timeout_seconds, bool)
        or not 0 < timeout_seconds <= 30
        or isinstance(maximum_body_bytes, bool)
        or not 1 <= maximum_body_bytes <= MAXIMUM_BODY_BYTES
    ):
        _fail("invalid_configuration")
    path = Path(socket_path)
    _validate_socket(path)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout_seconds)
            connection.connect(str(path))
            connection.sendall(
                b"GET /v1/snapshot HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Connection: close\r\n\r\n"
            )
            body = _read_response(connection, maximum_body_bytes)
    except LocalAPIClientError:
        raise
    except socket.timeout:
        _fail("timed_out")
    except OSError:
        _fail("unavailable")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("invalid_response")
    return _project_snapshot(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    arguments = parser.parse_args(argv)
    try:
        result = read_snapshot(arguments.socket)
    except LocalAPIClientError as error:
        print(
            json.dumps(
                {"error": str(error), "ok": False},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
