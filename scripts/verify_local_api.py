#!/usr/bin/env python3
"""Small private Unix-socket health probe used by install and rollback gates."""

from __future__ import annotations

import json
import socket
import sys
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


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    path = Path(sys.argv[1])
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            for route in ("/v1/health", "/v1/schema", "/v1/summary"):
                payload = get(path, route)
                if payload.get("schemaVersion") != "1.0":
                    raise RuntimeError("unexpected local API schema")
            return 0
        except (OSError, ValueError, RuntimeError):
            time.sleep(0.2)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
