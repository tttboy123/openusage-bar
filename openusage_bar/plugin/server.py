"""Bounded loopback HTTP transport for Plugin API v1."""

from __future__ import annotations

import json
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .api import PluginRouter
from .config import PluginPrincipalRegistry
from .contracts import MAX_BODY_BYTES, problem, sanitize_response


MAX_HEADER_COUNT = 32
MAX_HEADER_BYTES = 16 * 1024
MAX_THREADS = 32
MAX_RESPONSE_BYTES = 1024 * 1024
_ALLOWED_HEADERS = frozenset({
    "host", "accept", "accept-encoding", "authorization", "content-type",
    "content-length", "idempotency-key",
})


class _Handler(BaseHTTPRequestHandler):
    server_version = "OpenUsagePlugin/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_OPTIONS(self) -> None:
        self._handle()

    def do_TRACE(self) -> None:
        self._handle()

    def send_error(self, _code: int, _message: str | None = None, _explain: str | None = None) -> None:
        self._send(405, problem("method_not_allowed", "Method is not allowed.", False), self.path)

    def _handle(self) -> None:
        server: PluginHTTPServer = self.server  # type: ignore[assignment]
        if not self._bounded_headers():
            self._send(400, problem("invalid_header", "Invalid request header.", False), "/plugin/v1/schema")
            return
        hosts = self.headers.get_all("Host", failobj=[])
        expected_host = f"127.0.0.1:{server.server_port}"
        if len(hosts) != 1 or hosts[0].strip() != expected_host:
            self._send(403, problem("forbidden_host", "Host is not allowed.", False), "/plugin/v1/schema")
            return
        if self.headers.get("Origin") is not None or self.headers.get("X-OpenUsage-Plugin-Id") is not None or self.headers.get("X-Plugin-Id") is not None:
            self._send(400, problem("invalid_header", "Invalid request header.", False), "/plugin/v1/schema")
            return
        accepts = self.headers.get_all("Accept", failobj=[])
        if len(accepts) > 1 or (accepts and accepts[0].strip().casefold() not in {"application/json", "*/*"}):
            self._send(400, problem("invalid_header", "Invalid request header.", False), self.path)
            return
        encodings = self.headers.get_all("Accept-Encoding", failobj=[])
        if len(encodings) > 1 or (encodings and encodings[0].strip().casefold() != "identity"):
            self._send(400, problem("invalid_header", "Invalid request header.", False), self.path)
            return
        authorizations = self.headers.get_all("Authorization", failobj=[])
        if len(authorizations) != 1 or not authorizations[0].startswith("Bearer "):
            self._send(401, problem("authentication_required", "Authentication is required.", False), "/plugin/v1/schema")
            return
        principal = server.registry.authenticate(authorizations[0][7:])
        if principal is None:
            self._send(401, problem("authentication_required", "Authentication is required.", False), "/plugin/v1/schema")
            return
        lengths = self.headers.get_all("Content-Length", failobj=[])
        transfers = self.headers.get_all("Transfer-Encoding", failobj=[])
        if len(lengths) > 1 or transfers or (
            lengths and (
                not lengths[0].isascii() or not lengths[0].isdecimal()
                or len(lengths[0]) > 10 or str(int(lengths[0])) != lengths[0]
            )
        ):
            self._send(400, problem("invalid_header", "Invalid request header.", False), self.path)
            return
        length = int(lengths[0]) if lengths else 0
        if self.command == "GET" and length:
            self._send(413, problem("request_body_not_allowed", "Request bodies are not allowed.", False), self.path)
            return
        if length > MAX_BODY_BYTES:
            self._send(413, problem("request_too_large", "Request is too large.", False), self.path)
            return
        if self.command == "POST":
            content_types = self.headers.get_all("Content-Type", failobj=[])
            if len(content_types) != 1 or content_types[0].strip().casefold() not in {
                "application/json", "application/json; charset=utf-8",
            }:
                self._send(400, problem("invalid_header", "Invalid request header.", False), self.path)
                return
        idem_values = self.headers.get_all("Idempotency-Key", failobj=[])
        if len(idem_values) > 1:
            self._send(400, problem("invalid_header", "Invalid request header.", False), self.path)
            return
        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            self._send(400, problem("invalid_request", "Invalid request.", False), self.path)
            return
        try:
            status, payload = server.router.dispatch(
                principal, self.command, self.path, body,
                idempotency_key=idem_values[0] if idem_values else None,
            )
        except Exception:
            status, payload = 500, problem("internal_error", "Request could not be completed.", True)
        self._send(status, payload, self.path)

    def _bounded_headers(self) -> bool:
        items = list(self.headers.raw_items())
        if len(items) > MAX_HEADER_COUNT:
            return False
        total = 0
        for name, value in items:
            try:
                raw_name = name.encode("ascii")
                raw_value = value.encode("latin-1")
            except UnicodeError:
                return False
            if not raw_name or any(byte < 33 or byte > 126 for byte in raw_name) or any(byte < 32 or byte == 127 for byte in raw_value):
                return False
            if name.casefold() not in _ALLOWED_HEADERS:
                return False
            total += len(raw_name) + len(raw_value) + 4
        return total <= MAX_HEADER_BYTES

    def _send(self, status: int, payload: dict[str, object], route: str) -> None:
        try:
            safe = sanitize_response(route, status, payload)
        except Exception:
            status = 500
            safe = problem("internal_error", "Request could not be completed.", True)
        encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_RESPONSE_BYTES:
            status = 422
            safe = problem("result_too_large", "Query result exceeds the public response limit.", False)
            encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)
        self.close_connection = True


class PluginHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, router: PluginRouter, registry: PluginPrincipalRegistry, *, host: str, port: int) -> None:
        self.router = router
        self.registry = registry
        self._slots = threading.BoundedSemaphore(MAX_THREADS)
        super().__init__((host, port), _Handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self.router.close()


def create_plugin_server(
    router: PluginRouter, *, registry: PluginPrincipalRegistry,
    host: str = "127.0.0.1", port: int = 17824,
) -> PluginHTTPServer:
    if type(router) is not PluginRouter or type(registry) is not PluginPrincipalRegistry:
        raise ValueError("invalid Plugin server configuration")
    if host != "127.0.0.1" or type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("invalid Plugin server configuration")
    return PluginHTTPServer(router, registry, host=host, port=port)
