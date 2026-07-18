#!/usr/bin/env python3
"""Small private Unix-socket health probe used by install and rollback gates."""

from __future__ import annotations

import argparse
import json
import math
import socket
import time
from pathlib import Path


def get(socket_path: Path, route: str) -> dict[str, object]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(3)
    try:
        client.connect(str(socket_path))
        client.sendall(
            f"GET {route} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
        )
        chunks = []
        while chunk := client.recv(65536):
            chunks.append(chunk)
    finally:
        client.close()
    head, body = b"".join(chunks).split(b"\r\n\r\n", 1)
    if b" 200 " not in head.split(b"\r\n", 1)[0]:
        raise RuntimeError("local API health unavailable")
    return json.loads(body)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify the OpenUsage local API v1 contract.")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("socket", type=Path)
    arguments = parser.parse_args()
    if not math.isfinite(arguments.timeout) or not 0.1 <= arguments.timeout <= 20:
        parser.error("timeout must be between 0.1 and 20 seconds")
    if not arguments.socket.is_absolute() or "\x00" in str(arguments.socket):
        parser.error("socket path must be absolute")
    return arguments


def main() -> int:
    arguments = _arguments()
    path = arguments.socket
    deadline = time.monotonic() + arguments.timeout
    while time.monotonic() < deadline:
        try:
            for route in ("/v1/health", "/v1/schema", "/v1/summary"):
                payload = get(path, route)
                if payload.get("schemaVersion") != "1.0":
                    raise RuntimeError("unexpected local API schema")
                if route == "/v1/health" and payload.get("health") != {
                    "ok": True, "status": "ok"
                }:
                    raise RuntimeError("local API health contract unavailable")
                if route == "/v1/schema" and not isinstance(payload.get("routes"), list):
                    raise RuntimeError("local API route contract unavailable")
                if route == "/v1/summary" and "todayTokens" not in payload:
                    raise RuntimeError("local API summary contract unavailable")
            return 0
        except (OSError, ValueError, RuntimeError):
            time.sleep(0.2)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
