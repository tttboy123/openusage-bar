"""Bounded JSON CLI client for the local Route Decision API."""

from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path
from typing import TextIO
from urllib.parse import quote

from .routing_api import MAX_HEADER_BYTES, MAX_OUTPUT_BYTES, MAX_REQUEST_BYTES


DEFAULT_ROUTER_SOCKET_PATH = (
    Path.home() / ".local" / "state" / "openusage-bar" / "router.sock"
)


class RoutingCLIError(RuntimeError):
    pass


class RoutingCLIInputError(RoutingCLIError):
    pass


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RoutingCLIError("routing output is invalid")
        result[key] = value
    return result


def _socket_identity(path: Path) -> tuple[int, int]:
    if not path.is_absolute() or path.is_symlink():
        raise RoutingCLIError("routing socket is unavailable")
    try:
        value = path.lstat()
    except OSError as error:
        raise RoutingCLIError("routing socket is unavailable") from error
    if (
        not stat.S_ISSOCK(value.st_mode)
        or value.st_uid != os.getuid()
        or stat.S_IMODE(value.st_mode) != 0o600
    ):
        raise RoutingCLIError("routing socket is unavailable")
    return value.st_dev, value.st_ino


def _read_framed(peer: socket.socket) -> tuple[int, object]:
    limit = MAX_HEADER_BYTES + MAX_OUTPUT_BYTES + 4
    data = bytearray()
    while len(data) <= limit:
        chunk = peer.recv(min(64 * 1024, limit + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    if len(data) > limit or b"\r\n\r\n" not in data:
        raise RoutingCLIError("routing output is invalid")
    raw_head, raw_body = bytes(data).split(b"\r\n\r\n", 1)
    if len(raw_head) > MAX_HEADER_BYTES:
        raise RoutingCLIError("routing output is invalid")
    try:
        lines = raw_head.decode("ascii").split("\r\n")
        protocol, status_raw, _reason = lines[0].split(" ", 2)
        status = int(status_raw)
        pairs = [line.split(":", 1) for line in lines[1:]]
    except (UnicodeError, ValueError) as error:
        raise RoutingCLIError("routing output is invalid") from error
    if protocol != "HTTP/1.1" or not 100 <= status <= 599:
        raise RoutingCLIError("routing output is invalid")
    headers: dict[str, list[str]] = {}
    for pair in pairs:
        if len(pair) != 2:
            raise RoutingCLIError("routing output is invalid")
        name, value = pair
        headers.setdefault(name.lower(), []).append(value.strip())
    lengths = headers.get("content-length", [])
    content_types = headers.get("content-type", [])
    if (
        len(lengths) != 1
        or len(content_types) != 1
        or headers.get("transfer-encoding")
        or content_types[0] != "application/json; charset=utf-8"
    ):
        raise RoutingCLIError("routing output is invalid")
    try:
        length = int(lengths[0])
    except ValueError as error:
        raise RoutingCLIError("routing output is invalid") from error
    if str(length) != lengths[0] or not 0 < length <= MAX_OUTPUT_BYTES:
        raise RoutingCLIError("routing output is invalid")
    if len(raw_body) != length:
        raise RoutingCLIError("routing output is invalid")
    try:
        text = raw_body.decode("utf-8")
        decoder = json.JSONDecoder(object_pairs_hook=_strict_object)
        payload, offset = decoder.raw_decode(text)
    except (UnicodeError, json.JSONDecodeError, RoutingCLIError) as error:
        raise RoutingCLIError("routing output is invalid") from error
    if text[offset:].strip() or not isinstance(payload, dict):
        raise RoutingCLIError("routing output is invalid")
    return status, payload


def request_routing_json(
    socket_path: str | Path,
    method: str,
    route: str,
    *,
    body: bytes | None = None,
    timeout: float = 5.0,
) -> tuple[int, object]:
    path = Path(socket_path).expanduser()
    identity = _socket_identity(path)
    if method not in {"GET", "POST"} or not route.startswith("/v1/"):
        raise RoutingCLIInputError("routing input is invalid")
    if body is not None and (not body or len(body) > MAX_REQUEST_BYTES):
        raise RoutingCLIInputError("routing input is invalid")
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.settimeout(timeout)
    try:
        peer.connect(str(path))
        if _socket_identity(path) != identity:
            raise RoutingCLIError("routing socket changed during connect")
        headers = [
            f"{method} {route} HTTP/1.1",
            "Host: localhost",
            "Connection: close",
        ]
        if body is not None:
            headers.extend(
                ["Content-Type: application/json", f"Content-Length: {len(body)}"]
            )
        framed = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + (body or b"")
        try:
            peer.sendall(framed)
        except BrokenPipeError:
            # A saturated server may frame ``router_busy`` and close before the
            # client finishes sending. The already-buffered bounded reply is
            # still authoritative and must be read below.
            pass
        try:
            peer.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        return _read_framed(peer)
    except (OSError, TimeoutError) as error:
        raise RoutingCLIError("routing socket is unavailable") from error
    finally:
        peer.close()


def _read_input(stdin: TextIO) -> bytes:
    value = stdin.read(MAX_REQUEST_BYTES + 1)
    if not isinstance(value, str):
        raise RoutingCLIInputError("routing input is invalid")
    encoded = value.encode("utf-8")
    if not encoded or len(encoded) > MAX_REQUEST_BYTES:
        raise RoutingCLIInputError("routing input is invalid")
    return encoded


def _write_json(stdout: TextIO, value: object) -> None:
    stdout.write(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


def run_route_command(args, *, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    try:
        socket_path = Path(args.socket).expanduser()
        if args.route_command in {"decide", "simulate", "shadow", "replay"}:
            body = _read_input(stdin)
            route = {
                "decide": "/v1/decisions",
                "simulate": "/v1/simulations",
                "shadow": "/v1/shadow-decisions",
                "replay": "/v1/replays",
            }[args.route_command]
            status, payload = request_routing_json(
                socket_path, "POST", route, body=body
            )
        elif args.route_command in {"history", "shadow-history"}:
            if isinstance(args.limit, bool) or not 1 <= args.limit <= 100:
                raise RoutingCLIInputError("routing input is invalid")
            base = (
                "/v1/decisions"
                if args.route_command == "history"
                else "/v1/shadow-decisions"
            )
            route = base + "?limit=" + str(args.limit)
            if args.before is not None:
                route += "&before=" + quote(args.before, safe="._-")
            status, payload = request_routing_json(socket_path, "GET", route)
        else:
            raise RoutingCLIInputError("routing input is invalid")
        _write_json(stdout, payload)
        if 200 <= status < 300:
            return 0
        if status == 400:
            return 2
        if status == 409:
            return 3
        return 1
    except RoutingCLIInputError:
        stderr.write("invalid routing input\n")
        return 2
    except RoutingCLIError:
        stderr.write("routing unavailable\n")
        return 1
    except Exception:
        stderr.write("routing unavailable\n")
        return 1
