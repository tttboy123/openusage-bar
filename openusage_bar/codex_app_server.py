from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, BinaryIO, Sequence

from .bounded_process import BoundedProcessError, run_bounded
from .openusage_adapter import child_subprocess_environment


INTERNAL_CODEX_RATE_LIMITS_COMMAND = "__codex-rate-limits"
MAX_CODEX_PROTOCOL_BYTES = 512 * 1024
CODEX_QUERY_TIMEOUT_SECONDS = 12


def _valid_command(command: Sequence[str]) -> bool:
    return (
        0 < len(command) <= 8
        and all(
            isinstance(part, str)
            and part
            and len(part.encode("utf-8")) <= 4096
            and not any(character in part for character in "\x00\r\n")
            for part in command
        )
        and Path(command[0]).is_absolute()
        and Path(command[0]).is_file()
        and os.access(command[0], os.X_OK)
    )


def _default_helper_command() -> tuple[str, ...]:
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        return (str(executable),) if _valid_command((str(executable),)) else ()
    entrypoint = Path(__file__).resolve().parent.parent / "openusage_collector.py"
    command = (sys.executable, str(entrypoint))
    return command if _valid_command(command) else ()


def _codex_binary_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    discovered = shutil.which("codex")
    if discovered:
        candidates.append(Path(discovered))
    if sys.platform == "darwin":
        candidates.extend(
            (
                Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
                Path("/Applications/Codex.app/Contents/Resources/codex"),
                Path.home()
                / "Applications/ChatGPT.app/Contents/Resources/codex",
                Path.home() / "Applications/Codex.app/Contents/Resources/codex",
            )
        )
    candidates.extend(
        (
            Path.home() / ".local/bin/codex",
            Path.home() / ".bun/bin/codex",
            Path("/opt/homebrew/bin/codex"),
            Path("/usr/local/bin/codex"),
        )
    )
    return tuple(candidates)


def resolve_codex_command() -> tuple[str, ...]:
    for candidate in _codex_binary_candidates():
        try:
            resolved = candidate.expanduser().resolve(strict=True)
        except OSError:
            continue
        command = (str(resolved),)
        if _valid_command(command):
            return command
    return ()


def _read_response(stream: BinaryIO, request_id: int) -> dict[str, Any]:
    consumed = 0
    for _ in range(256):
        remaining = MAX_CODEX_PROTOCOL_BYTES - consumed
        if remaining <= 0:
            break
        line = stream.readline(remaining + 1)
        if not line or len(line) > remaining:
            break
        consumed += len(line)
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, UnicodeError):
            continue
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        if message.get("error") not in (None, {}):
            break
        result = message.get("result")
        if isinstance(result, dict):
            return result
        break
    raise ValueError("invalid Codex app-server response")


def _write_request(stream: BinaryIO, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(encoded) > 16 * 1024:
        raise ValueError("invalid Codex app-server request")
    stream.write(encoded + b"\n")
    stream.flush()


def run_codex_app_server_helper(
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    *,
    environment: dict[str, str] | None = None,
) -> int:
    process: subprocess.Popen[bytes] | None = None
    try:
        raw = input_stream.read(32 * 1024 + 1)
        if len(raw) > 32 * 1024:
            raise ValueError("invalid helper request")
        request = json.loads(raw)
        command = request.get("command") if isinstance(request, dict) else None
        if (
            not isinstance(request, dict)
            or set(request) != {"version", "command"}
            or request.get("version") != 1
            or not isinstance(command, list)
            or not _valid_command(command)
        ):
            raise ValueError("invalid helper request")

        process = subprocess.Popen(
            [
                *command,
                "app-server",
                "--stdio",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=child_subprocess_environment(environment),
            shell=False,
        )
        if process.stdin is None or process.stdout is None:
            raise ValueError("Codex app-server unavailable")
        _write_request(
            process.stdin,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "usagehub", "version": "0.8.6"}
                },
            },
        )
        _read_response(process.stdout, 1)
        _write_request(process.stdin, {"method": "initialized", "params": {}})
        _write_request(
            process.stdin,
            {"id": 2, "method": "account/rateLimits/read", "params": None},
        )
        result = _read_response(process.stdout, 2)
        encoded = json.dumps(
            {"version": 1, "ok": True, "result": result},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_CODEX_PROTOCOL_BYTES:
            raise ValueError("Codex app-server response is too large")
        output_stream.write(encoded)
        output_stream.flush()
        return 0
    except Exception:
        return 1
    finally:
        if process is not None:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                pass
            try:
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass


def read_codex_app_server_rate_limits(
    *,
    codex_command: Sequence[str] | None = None,
    helper_command: Sequence[str] | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    command = tuple(codex_command or resolve_codex_command())
    helper = tuple(helper_command or _default_helper_command())
    if not _valid_command(command) or not _valid_command(helper):
        return None
    payload = json.dumps(
        {"version": 1, "command": list(command)},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        completed = run_bounded(
            (*helper, INTERNAL_CODEX_RATE_LIMITS_COMMAND),
            timeout=CODEX_QUERY_TIMEOUT_SECONDS,
            stdout_limit=MAX_CODEX_PROTOCOL_BYTES,
            stderr_limit=0,
            input_data=payload,
            env=child_subprocess_environment(environment),
        )
        if completed.returncode != 0 or not isinstance(completed.stdout, bytes):
            return None
        response = json.loads(completed.stdout)
    except (
        BoundedProcessError,
        json.JSONDecodeError,
        OSError,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        return None
    if (
        not isinstance(response, dict)
        or response.get("version") != 1
        or response.get("ok") is not True
        or not isinstance(response.get("result"), dict)
    ):
        return None
    return response["result"]
