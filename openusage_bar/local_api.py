"""Version 1 read-only local API for scheduler and native UI consumers.

The API deliberately exposes the same camelCase envelopes as ``QueryService``.
Unix-domain HTTP is the default transport. TCP is an explicit loopback-only
opt-in protected by a high-entropy bearer token. There are no write, refresh,
configuration, credential, remote-bind, CORS-wildcard, or TLS endpoints.
"""

from __future__ import annotations

import errno
import ctypes
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import socket
import socketserver
import stat
import struct
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Collection
from urllib.parse import parse_qsl, urlsplit

from .capabilities import registry as default_registry
from .config import ID_PATTERN
from .provider_catalog import catalog as default_catalog
from .query import MAX_LIMIT, SCHEMA_VERSION, QueryService, to_wire


MAX_REQUEST_LINE = 8_192
MAX_BODY_BYTES = 0
MAX_QUERY_BYTES = 4_096
MAX_QUERY_FIELDS = 16
MAX_ID_COUNT = 50
MAX_ID_LENGTH = 128
MAX_CURSOR = 2**63 - 1
DEFAULT_MAX_THREADS = 32
DEFAULT_CLIENT_TIMEOUT = 5.0
DEFAULT_REQUEST_DEADLINE = 15.0
DEFAULT_RATE_LIMIT_CAPACITY = 120
DEFAULT_RATE_LIMIT_REFILL_PER_SECOND = 2.0
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class APIProblem(Exception):
    status: HTTPStatus
    code: str
    message: str
    retry_after: int | None = None


class TokenBucket:
    """One bounded, thread-safe bucket for one local TCP server bearer."""

    def __init__(
        self,
        capacity: int,
        refill_per_second: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= 10_000:
            raise ValueError("rate limit capacity must be between 1 and 10000")
        if (
            isinstance(refill_per_second, bool)
            or not isinstance(refill_per_second, (int, float))
            or not math.isfinite(refill_per_second)
            or not 0 < refill_per_second <= 10_000
        ):
            raise ValueError("rate limit refill must be positive and bounded")
        self.capacity = capacity
        self.refill_per_second = float(refill_per_second)
        self.monotonic = monotonic
        self._tokens = float(capacity)
        self._updated = float(monotonic())
        self._lock = threading.Lock()

    def consume(self) -> tuple[bool, int]:
        with self._lock:
            current = float(self.monotonic())
            if not math.isfinite(current) or current < self._updated:
                current = self._updated
            elapsed = current - self._updated
            self._tokens = min(
                float(self.capacity),
                self._tokens + elapsed * self.refill_per_second,
            )
            self._updated = current
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True, 0
            wait = max(1, math.ceil((1.0 - self._tokens) / self.refill_per_second))
            return False, wait


def _compact(payload: Any) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _error(
    status: HTTPStatus,
    code: str,
    message: str,
    *,
    retry_after: int | None = None,
) -> APIProblem:
    return APIProblem(status, code, message, retry_after)


def _day(value: str, name: str) -> date:
    if len(value) != 10:
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Invalid {name}.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Invalid {name}.") from error
    if parsed.isoformat() != value:
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Invalid {name}.")
    return parsed


def _integer(value: str, name: str, *, minimum: int, maximum: int) -> int:
    if not value or len(value) > 19 or not value.isascii() or not value.isdecimal():
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Invalid {name}.")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Invalid {name}.")
    return parsed


def _ids(value: str, name: str) -> tuple[str, ...]:
    if not value:
        return ()
    values = tuple(value.split(","))
    if len(values) > MAX_ID_COUNT or any(
        not item
        or len(item) > MAX_ID_LENGTH
        or ID_PATTERN.fullmatch(item) is None
        for item in values
    ):
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Invalid {name}.")
    return values


def _timestamp(value: str, name: str) -> str:
    if len(value) > 40 or _CONTROL.search(value):
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Invalid {name}.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Invalid {name}.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", f"Invalid {name}.")
    return value


def _parameters(raw_query: str, allowed: Collection[str]) -> dict[str, str]:
    if len(raw_query.encode("utf-8")) > MAX_QUERY_BYTES:
        raise _error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_too_large", "Request is too large.")
    if _CONTROL.search(raw_query) or _BAD_PERCENT.search(raw_query):
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_query", "Invalid query string.")
    try:
        pairs = parse_qsl(
            raw_query, keep_blank_values=True, strict_parsing=True,
            encoding="utf-8", errors="strict", max_num_fields=MAX_QUERY_FIELDS,
        ) if raw_query else []
    except (UnicodeError, ValueError) as error:
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_query", "Invalid query string.") from error
    result: dict[str, str] = {}
    for name, value in pairs:
        if name not in allowed or name in result or _CONTROL.search(name) or _CONTROL.search(value):
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_query", "Invalid query string.")
        result[name] = value
    return result


def _entity_tags(value: str) -> tuple[str, ...] | None:
    """Parse If-None-Match using RFC entity-tag list grammar.

    ``None`` represents the wildcard. Returned values are opaque tags without
    their weak/strong marker so callers can perform the required weak compare.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_QUERY_BYTES:
        raise _error(HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header.")
    stripped = value.strip(" \t")
    if stripped == "*":
        return None

    tags: list[str] = []
    index = 0
    length = len(value)
    while True:
        while index < length and value[index] in " \t":
            index += 1
        if index >= length:
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header.")
        if value.startswith("W/", index):
            index += 2
        if index >= length or value[index] != '"':
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header.")
        index += 1
        start = index
        while index < length and value[index] != '"':
            codepoint = ord(value[index])
            if not (
                codepoint == 0x21
                or 0x23 <= codepoint <= 0x7E
                or 0x80 <= codepoint <= 0xFF
            ):
                raise _error(
                    HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header."
                )
            index += 1
        if index >= length:
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header.")
        tags.append(value[start:index])
        index += 1
        while index < length and value[index] in " \t":
            index += 1
        if index == length:
            return tuple(tags)
        if value[index] != ",":
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header.")
        index += 1


def _if_none_match(value: str, current_etag: str) -> bool:
    candidates = _entity_tags(value)
    if candidates is None:
        return True
    current = _entity_tags(current_etag)
    if current is None or len(current) != 1:  # pragma: no cover - trusted server tag
        raise RuntimeError("server ETag is invalid")
    return any(hmac.compare_digest(candidate, current[0]) for candidate in candidates)


class LocalAPIRouter:
    """Pure request router with injected query, clock, registry, and verifier."""

    ROUTES = (
        "/v1/health", "/v1/schema", "/v1/summary", "/v1/snapshot",
        "/v1/capabilities",
        "/v1/providers", "/v1/capacity", "/v1/activity/daily",
        "/v1/costs/daily",
        "/v1/quotas/history",
        "/v1/sources/status", "/v1/changes",
    )

    def __init__(
        self,
        query: QueryService,
        *,
        clock: Callable[[], datetime] | None = None,
        provider_registry: Any = default_registry,
        bearer_verifier: Callable[[str], bool] | None = None,
        rate_limiter: TokenBucket | None = None,
        allowed_origins: Collection[str] = (),
        tcp_port: int | None = None,
    ) -> None:
        self.query = query
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.provider_registry = provider_registry
        self.bearer_verifier = bearer_verifier
        self.rate_limiter = rate_limiter
        origins = frozenset(allowed_origins)
        if any(
            not isinstance(origin, str)
            or not origin
            or origin == "*"
            or len(origin) > 512
            or _CONTROL.search(origin)
            for origin in origins
        ):
            raise ValueError("allowed origins must be explicit safe values")
        self.allowed_origins = origins
        self.tcp_port = tcp_port

    def handle(self, handler: "ReadOnlyHandler", *, include_body: bool) -> None:
        try:
            self._validate_headers(handler)
            self._authorize(handler)
            origin = self._origin(handler)
            self._reject_body(handler)
            route, params = self._target(handler.path)
            payload = self._payload(route, params)
            etag = self._etag(payload)
            headers = self._headers(origin, etag)
            validators = handler.headers.get_all("If-None-Match", failobj=[])
            if len(validators) > 1:
                raise _error(HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header.")
            if validators and _if_none_match(validators[0], etag):
                handler._send(
                    HTTPStatus.NOT_MODIFIED, b"", headers,
                    include_body=False, include_length=False,
                )
                return
            body = _compact(payload)
            handler._send(HTTPStatus.OK, body, headers, include_body=include_body)
        except APIProblem as problem:
            handler._problem(problem, include_body=include_body)
        except Exception:
            handler._problem(
                _error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "Request could not be completed."),
                include_body=include_body,
            )

    @staticmethod
    def _validate_headers(handler: "ReadOnlyHandler") -> None:
        for name, value in handler.headers.raw_items():
            if (
                not name
                or _CONTROL.search(name)
                or _CONTROL.search(value)
                or "\r" in value
                or "\n" in value
            ):
                raise _error(HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header.")
        hosts = handler.headers.get_all("Host", failobj=[])
        authorizations = handler.headers.get_all("Authorization", failobj=[])
        lengths = handler.headers.get_all("Content-Length", failobj=[])
        if len(hosts) != 1 or not hosts[0].strip() or len(authorizations) > 1 or len(lengths) > 1:
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header.")
        if lengths and (not lengths[0].isascii() or not lengths[0].isdecimal()):
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_header", "Invalid request header.")

    def _authorize(self, handler: "ReadOnlyHandler") -> None:
        if self.tcp_port is not None:
            hosts = handler.headers.get_all("Host", failobj=[])
            expected = f"127.0.0.1:{self.tcp_port}"
            if len(hosts) != 1 or not hmac.compare_digest(hosts[0].strip(), expected):
                raise _error(HTTPStatus.FORBIDDEN, "forbidden_host", "Host is not allowed.")
            values = handler.headers.get_all("Authorization", failobj=[])
            if len(values) != 1 or not values[0].startswith("Bearer "):
                raise _error(HTTPStatus.UNAUTHORIZED, "authentication_required", "Authentication is required.")
            token = values[0][7:]
            if self.bearer_verifier is None or not self.bearer_verifier(token):
                raise _error(HTTPStatus.UNAUTHORIZED, "authentication_required", "Authentication is required.")
            if self.rate_limiter is not None:
                allowed, retry_after = self.rate_limiter.consume()
                if not allowed:
                    raise _error(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        "rate_limited",
                        "Request rate limit exceeded.",
                        retry_after=retry_after,
                    )

    def _origin(self, handler: "ReadOnlyHandler") -> str | None:
        values = handler.headers.get_all("Origin", failobj=[])
        if len(values) > 1:
            raise _error(HTTPStatus.FORBIDDEN, "forbidden_origin", "Origin is not allowed.")
        if not values:
            return None
        origin = values[0]
        if origin not in self.allowed_origins:
            raise _error(HTTPStatus.FORBIDDEN, "forbidden_origin", "Origin is not allowed.")
        return origin

    @staticmethod
    def _reject_body(handler: "ReadOnlyHandler") -> None:
        if handler.headers.get("Transfer-Encoding") is not None:
            raise _error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_body_not_allowed", "Request bodies are not allowed.")
        lengths = handler.headers.get_all("Content-Length", failobj=[])
        if lengths:
            length = int(lengths[0])
            if length > MAX_BODY_BYTES:
                raise _error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_body_not_allowed", "Request bodies are not allowed.")

    @staticmethod
    def _target(target: str) -> tuple[str, dict[str, str]]:
        if _CONTROL.search(target) or _BAD_PERCENT.search(target):
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_target", "Invalid request target.")
        split = urlsplit(target)
        if split.scheme or split.netloc or split.fragment or "%" in split.path:
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_target", "Invalid request target.")
        route = "/v1/schema" if split.path == "/schema" else split.path
        allowed = {
            "/v1/health": (),
            "/v1/schema": (),
            "/v1/summary": ("today",),
            "/v1/snapshot": ("today",),
            "/v1/capabilities": (),
            "/v1/providers": ("providerIds",),
            "/v1/capacity": ("limit",),
            "/v1/activity/daily": ("from", "to", "providerIds", "modelIds"),
            "/v1/costs/daily": ("from", "to", "providerIds", "currencies"),
            "/v1/quotas/history": ("providerId", "accountRef", "from", "to", "limit"),
            "/v1/sources/status": (),
            "/v1/changes": ("after", "limit"),
        }
        if route not in allowed:
            raise _error(HTTPStatus.NOT_FOUND, "not_found", "Route was not found.")
        parameters = _parameters(split.query, allowed[route])
        if route == "/v1/providers" and "providerIds" in parameters:
            identifiers = _ids(parameters["providerIds"], "providerIds")
            if identifiers:
                parameters["providerIds"] = ",".join(sorted(set(identifiers)))
            else:
                parameters.pop("providerIds")
        return route, parameters

    def _payload(self, route: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            if route == "/v1/summary":
                now = self.clock()
                selected = _day(params["today"], "today") if "today" in params else now.astimezone().date()
                return to_wire(self.query.summary(selected))
            if route == "/v1/snapshot":
                now = self.clock()
                selected = (
                    _day(params["today"], "today")
                    if "today" in params else now.astimezone().date()
                )
                return to_wire(self.query.resource_snapshot(selected))
            if route == "/v1/capacity":
                limit = _integer(params["limit"], "limit", minimum=1, maximum=MAX_LIMIT) if "limit" in params else None
                return to_wire(self.query.capacity(limit))
            if route == "/v1/activity/daily":
                if "from" not in params or "to" not in params:
                    raise _error(HTTPStatus.BAD_REQUEST, "missing_parameter", "Required parameter is missing.")
                return to_wire(self.query.activity(
                    _day(params["from"], "from"), _day(params["to"], "to"),
                    _ids(params.get("providerIds", ""), "providerIds"),
                    _ids(params.get("modelIds", ""), "modelIds"),
                ))
            if route == "/v1/costs/daily":
                if "from" not in params or "to" not in params:
                    raise _error(
                        HTTPStatus.BAD_REQUEST, "missing_parameter",
                        "Required parameter is missing.",
                    )
                return to_wire(self.query.costs(
                    _day(params["from"], "from"),
                    _day(params["to"], "to"),
                    _ids(params.get("providerIds", ""), "providerIds"),
                    _ids(params.get("currencies", ""), "currencies"),
                ))
            if route == "/v1/quotas/history":
                provider_id = params.get("providerId")
                account_ref = params.get("accountRef")
                if provider_id is not None:
                    parsed_provider_ids = _ids(provider_id, "providerId")
                    if len(parsed_provider_ids) != 1:
                        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", "Invalid providerId.")
                    provider_id = parsed_provider_ids[0]
                if account_ref is not None:
                    parsed_account_refs = _ids(account_ref, "accountRef")
                    if len(parsed_account_refs) != 1:
                        raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", "Invalid accountRef.")
                    account_ref = parsed_account_refs[0]
                start = _timestamp(params["from"], "from") if "from" in params else None
                end = _timestamp(params["to"], "to") if "to" in params else None
                if (start is None) != (end is None):
                    raise _error(HTTPStatus.BAD_REQUEST, "missing_parameter", "Both time bounds are required.")
                limit = _integer(params.get("limit", "1000"), "limit", minimum=1, maximum=MAX_LIMIT)
                return to_wire(self.query.quota_history(
                    provider_id=provider_id, account_ref=account_ref,
                    from_time=start, to_time=end, limit=limit,
                ))
            if route == "/v1/sources/status":
                return to_wire(self.query.source_status())
            if route == "/v1/providers":
                return to_wire(self.query.provider_instances(
                    _ids(params.get("providerIds", ""), "providerIds")
                ))
            if route == "/v1/changes":
                after = _integer(params.get("after", "0"), "after", minimum=0, maximum=MAX_CURSOR)
                limit = _integer(params.get("limit", "100"), "limit", minimum=1, maximum=MAX_LIMIT)
                return to_wire(self.query.changes(after, limit))
            status = to_wire(self.query.source_status())
            if route == "/v1/health":
                status.update({"health": {"ok": True, "status": "ok"}})
                return status
            if route == "/v1/schema":
                return {
                    "schemaVersion": SCHEMA_VERSION,
                    "dataRevision": status["dataRevision"],
                    "generatedAt": status["generatedAt"],
                    "routes": list(self.ROUTES),
                    "errorShape": {"error": {"code": "string", "message": "string"}},
                }
            if route == "/v1/capabilities":
                descriptors = self.provider_registry.descriptors
                providers = [{
                    "providerId": item.provider_id,
                    "familyId": item.provider_id,
                    "displayName": item.display_name,
                    "category": item.category,
                    "metricFamilies": sorted(value.value for value in item.metric_families),
                    "regions": sorted(item.regions),
                    "supportsAccounts": item.supports_accounts,
                    "capabilities": {
                        "quotaWindows": {
                            "state": item.capabilities.quota_windows.state.value,
                            "values": [
                                value.value
                                for value in item.capabilities.quota_windows.values
                            ],
                        },
                        "tokenHistory": item.capabilities.token_history.value,
                        "modelBreakdown": item.capabilities.model_breakdown.value,
                        "resetTimestamps": item.capabilities.reset_timestamps.value,
                        "billing": item.capabilities.billing.value,
                        "credits": item.capabilities.credits.value,
                        "balance": item.capabilities.balance.value,
                        "cost": item.capabilities.cost.value,
                        "rateLimits": item.capabilities.rate_limits.value,
                        "serviceStatus": item.capabilities.service_status.value,
                    },
                    "sources": [{
                        "sourceId": source.source_id,
                        "kind": source.kind.value,
                        "timeoutSeconds": source.timeout_seconds,
                        "freshnessSeconds": source.freshness_seconds,
                        "credentialType": source.credential_type.value,
                        "requiresCredential": source.credential_scope is not None,
                        "operatingSystems": sorted(
                            value.value for value in source.operating_systems
                        ),
                        "stability": source.stability.value,
                        "provenance": source.provenance.value,
                    } for source in item.sources],
                } for item in descriptors]
                return {
                    "schemaVersion": SCHEMA_VERSION,
                    "dataRevision": status["dataRevision"],
                    "generatedAt": status["generatedAt"],
                    "upstream": {
                        "name": "openusage",
                        "version": default_catalog.upstream_version,
                        "revision": default_catalog.upstream_revision,
                        "familyCount": len(default_catalog.upstream_family_ids),
                    },
                    "providers": providers,
                }
        except APIProblem:
            raise
        except ValueError as error:
            raise _error(HTTPStatus.BAD_REQUEST, "invalid_parameter", "Invalid request parameter.") from error
        raise _error(HTTPStatus.NOT_FOUND, "not_found", "Route was not found.")

    @staticmethod
    def _etag(payload: dict[str, Any]) -> str:
        # generatedAt is representation metadata, not a semantic resource
        # change. A weak validator over every other public field remains valid
        # across clock-only renders while changing for catalog or ledger facts.
        semantic_payload = {
            key: value for key, value in payload.items() if key != "generatedAt"
        }
        material = _compact(semantic_payload)
        return 'W/"' + hashlib.sha256(material).hexdigest() + '"'

    @staticmethod
    def _headers(origin: str | None, etag: str) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "private, no-cache",
            "X-Content-Type-Options": "nosniff",
            "ETag": etag,
        }
        if origin is not None:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"
        return headers


class ReadOnlyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OpenUsageLocalAPI/1"
    sys_version = ""

    def handle_one_request(self) -> None:
        try:
            self.raw_requestline = self.rfile.readline(MAX_REQUEST_LINE + 1)
            if len(self.raw_requestline) > MAX_REQUEST_LINE:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self._problem(
                    _error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_too_large", "Request is too large."),
                    include_body=not self._request_is_head(),
                )
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            if self.request_version != "HTTP/1.1":
                self.request_version = "HTTP/1.1"
                self.close_connection = True
                self._problem(
                    _error(HTTPStatus.BAD_REQUEST, "invalid_request", "HTTP/1.1 is required."),
                    include_body=not self._request_is_head(),
                )
                return
            method = getattr(self, "do_" + self.command, None)
            if method is None:
                self._method_not_allowed(include_body=not self._request_is_head())
                return
            method()
            self.wfile.flush()
        except TimeoutError:
            self.close_connection = True

    def do_GET(self) -> None:
        self.server.router.handle(self, include_body=True)

    def do_HEAD(self) -> None:
        self.server.router.handle(self, include_body=False)

    def _method_not_allowed(self, *, include_body: bool = True) -> None:
        # Never drain a mutation body; close after the 405 so its bytes cannot
        # be interpreted as a second request on the persistent connection.
        self.close_connection = True
        problem = _error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Only GET and HEAD are allowed.")
        self._problem(problem, include_body=include_body, extra_headers={"Allow": "GET, HEAD"})

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_OPTIONS = _method_not_allowed

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Replace stdlib HTML/parser-detail errors with the stable JSON shape."""
        del message, explain
        status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE if code in {414, 431} else HTTPStatus.BAD_REQUEST
        problem = _error(
            status,
            "request_too_large" if status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE else "invalid_request",
            "Request is too large." if status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE else "Invalid HTTP request.",
        )
        self.close_connection = True
        # ``parse_request`` temporarily uses HTTP/0.9 for malformed versions;
        # force a framed response so the stable JSON error remains parseable.
        self.request_version = "HTTP/1.1"
        self._problem(problem, include_body=not self._request_is_head())

    def _request_is_head(self) -> bool:
        if getattr(self, "command", None) == "HEAD":
            return True
        raw_requestline = getattr(self, "raw_requestline", b"")
        raw_method = raw_requestline.split(b" ", 1)[0] if raw_requestline else b""
        return raw_method == b"HEAD"

    def _problem(
        self,
        problem: APIProblem,
        *,
        include_body: bool,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = _compact({"error": {"code": problem.code, "message": problem.message}})
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            **(extra_headers or {}),
        }
        if problem.status == HTTPStatus.UNAUTHORIZED:
            headers["WWW-Authenticate"] = "Bearer"
        if problem.retry_after is not None:
            headers["Retry-After"] = str(problem.retry_after)
        self._send(problem.status, body, headers, include_body=include_body)

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        headers: dict[str, str],
        *,
        include_body: bool,
        include_length: bool = True,
    ) -> None:
        self.close_connection = True
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Connection", "close")
        if include_length:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body and body:
            self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


class _BoundedThreads:
    daemon_threads = True
    block_on_close = False

    def _configure_threads(self, maximum: int, timeout: float, deadline: float) -> None:
        self._thread_slots = threading.BoundedSemaphore(maximum)
        self._client_timeout = timeout
        self._request_deadline = deadline
        self._deadline_lock = threading.Lock()
        self._deadline_timers: dict[socket.socket, threading.Timer] = {}
        self._closing = False

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(self._client_timeout)
        return request, address

    def handle_error(self, request: socket.socket, client_address: Any) -> None:
        # Deadline disconnects and malformed peers must never emit request,
        # authorization, path, or traceback material to process logs.
        return

    def process_request(self, request, client_address) -> None:
        if not self._thread_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._thread_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        timer = threading.Timer(
            self._request_deadline,
            self._expire_request,
            args=(request,),
        )
        timer.daemon = True
        with self._deadline_lock:
            if self._closing:
                reject = True
            else:
                self._deadline_timers[request] = timer
                timer.start()
                reject = False
        if reject:
            self.shutdown_request(request)
            self._thread_slots.release()
            return
        try:
            super().process_request_thread(request, client_address)
        finally:
            timer.cancel()
            if timer is not threading.current_thread():
                timer.join()
            with self._deadline_lock:
                self._deadline_timers.pop(request, None)
            self._thread_slots.release()

    @staticmethod
    def _expire_request(request: socket.socket) -> None:
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    @property
    def active_deadline_count(self) -> int:
        with self._deadline_lock:
            return len(self._deadline_timers)

    def _abort_active_requests(self) -> None:
        with self._deadline_lock:
            self._closing = True
            active = tuple(self._deadline_timers.items())
        for request, timer in active:
            timer.cancel()
            self._expire_request(request)
        for _, timer in active:
            if timer is not threading.current_thread():
                timer.join()
        with self._deadline_lock:
            for request, timer in active:
                if self._deadline_timers.get(request) is timer:
                    self._deadline_timers.pop(request, None)

    def server_close(self) -> None:
        self._abort_active_requests()
        super().server_close()


class UnixHTTPServer(_BoundedThreads, socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    allow_reuse_address = False
    request_queue_size = DEFAULT_MAX_THREADS

    def __init__(
        self,
        path: Path,
        router: LocalAPIRouter,
        *,
        max_threads: int,
        client_timeout: float,
        request_deadline: float,
        cleanup_hook: Callable[[Path], None] | None,
    ) -> None:
        self.path = path
        self.router = router
        self._created_identity: tuple[int, int] | None = None
        self._cleanup_hook = cleanup_hook
        _prepare_socket_path(path)
        try:
            super().__init__(str(path), ReadOnlyHandler)
            current = path.lstat()
            self._created_identity = (current.st_dev, current.st_ino)
            os.chmod(path, 0o600, follow_symlinks=False)
            self._configure_threads(max_threads, client_timeout, request_deadline)
        except Exception:
            if self._created_identity is not None:
                _unlink_socket_if(path, self._created_identity)
            raise

    def verify_request(self, request: socket.socket, client_address: Any) -> bool:
        return _peer_is_current_user(request)

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            _unlink_socket_if(
                self.path,
                self._created_identity,
                after_quarantine=self._cleanup_hook,
            )


class LoopbackHTTPServer(_BoundedThreads, ThreadingHTTPServer):
    allow_reuse_address = False
    request_queue_size = DEFAULT_MAX_THREADS

    def __init__(
        self,
        router: LocalAPIRouter,
        port: int,
        *,
        max_threads: int,
        client_timeout: float,
        request_deadline: float,
    ) -> None:
        super().__init__(("127.0.0.1", port), ReadOnlyHandler)
        self.router = router
        self._configure_threads(max_threads, client_timeout, request_deadline)


def _prepare_socket_path(path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise OSError("socket parent must be a directory")
    if path.parent.stat().st_uid != os.getuid():
        raise OSError("socket parent must be owned by the current user")
    os.chmod(path.parent, 0o700, follow_symlinks=False)
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(current.st_mode):
        raise OSError("socket path already exists and is not a socket")
    stale_identity = (current.st_dev, current.st_ino)
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.1)
    try:
        probe.connect(str(path))
    except OSError as error:
        if error.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
            raise OSError("socket path cannot be safely replaced") from error
    else:
        raise OSError("socket path is already in use")
    finally:
        probe.close()
    current = path.lstat()
    if not stat.S_ISSOCK(current.st_mode) or (current.st_dev, current.st_ino) != stale_identity:
        raise OSError("socket path changed during stale-socket check")
    _unlink_socket_if(path, stale_identity)


def _rename_exclusive(parent_fd: int, source: str, destination: str) -> None:
    """Atomically rename within one open directory without replacing a name."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx = getattr(libc, "renameatx_np", None)
    if renameatx is None:
        raise OSError(errno.ENOTSUP, "exclusive rename is unavailable")
    renameatx.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameatx.restype = ctypes.c_int
    result = renameatx(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(destination),
        0x00000004,  # RENAME_EXCL from sys/stdio.h.
    )
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), destination)


def _unlink_socket_if(
    path: Path,
    identity: tuple[int, int] | None,
    *,
    after_quarantine: Callable[[Path], None] | None = None,
) -> None:
    if identity is None:
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    parent_fd = os.open(path.parent, flags)
    quarantine = f".{path.name}.quarantine-{secrets.token_hex(16)}"
    try:
        parent = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid():
            raise OSError("socket parent changed or is not user-owned")
        try:
            _rename_exclusive(parent_fd, path.name, quarantine)
        except FileNotFoundError:
            return
        if after_quarantine is not None:
            after_quarantine(path)
        current = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISSOCK(current.st_mode) and (current.st_dev, current.st_ino) == identity:
            os.unlink(quarantine, dir_fd=parent_fd)
            return
        try:
            _rename_exclusive(parent_fd, quarantine, path.name)
        except OSError as error:
            raise OSError(
                f"socket cleanup preserved an unexpected node at {path.parent / quarantine}"
            ) from error
        raise OSError("socket cleanup restored an unexpected replacement and aborted")
    finally:
        os.close(parent_fd)


def _peer_is_current_user(peer: socket.socket) -> bool:
    if hasattr(peer, "getpeereid"):
        uid, _ = peer.getpeereid()
        return uid == os.getuid()
    if hasattr(socket, "SO_PEERCRED"):
        raw = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _, uid, _ = struct.unpack("3i", raw)
        return uid == os.getuid()
    return True  # Socket mode 0600 remains the platform capability boundary.


def _validate_token(token: str) -> str:
    if (
        not isinstance(token, str)
        or len(token) < 43
        or len(token) > 256
        or not token.isascii()
        or any(character.isspace() or ord(character) < 33 for character in token)
    ):
        raise ValueError("bearer token must be a high-entropy ASCII value")
    return token


def _prepare_private_parent(path: Path, purpose: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise OSError(f"{purpose} parent must be a directory")
    if path.parent.stat().st_uid != os.getuid():
        raise OSError(f"{purpose} parent must be owned by the current user")
    os.chmod(path.parent, 0o700, follow_symlinks=False)


def _read_token(path: Path) -> str:
    read_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        read_flags |= os.O_NOFOLLOW
    descriptor = os.open(path, read_flags)
    try:
        current = os.fstat(descriptor)
        content = os.read(descriptor, 257)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.getuid()
            or stat.S_IMODE(current.st_mode) != 0o600
        ):
            raise OSError("existing token file is unsafe")
    finally:
        os.close(descriptor)
    try:
        return _validate_token(content.decode("ascii"))
    except (UnicodeError, ValueError) as error:
        raise OSError("existing token file is unsafe") from error


def _create_token(path: Path, token: str) -> bool:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    try:
        os.fchmod(descriptor, 0o600)
        content = memoryview(token.encode("ascii"))
        while content:
            written = os.write(descriptor, content)
            if written <= 0:
                raise OSError("bearer token could not be persisted")
            content = content[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def _load_or_create_token(path: Path, supplied: str | None) -> str:
    _prepare_private_parent(path, "token")
    if supplied is not None:
        token = _validate_token(supplied)
        if _create_token(path, token):
            return token
        if not hmac.compare_digest(_read_token(path), token):
            raise OSError("existing token file is unsafe or does not match")
        return token
    try:
        return _read_token(path)
    except FileNotFoundError:
        generated = _validate_token(secrets.token_urlsafe(32))
        if _create_token(path, generated):
            return generated
        return _read_token(path)


def create_unix_server(
    socket_path: str | Path,
    query: QueryService,
    *,
    allowed_origins: Collection[str] = (),
    clock: Callable[[], datetime] | None = None,
    provider_registry: Any = default_registry,
    max_threads: int = DEFAULT_MAX_THREADS,
    client_timeout: float = DEFAULT_CLIENT_TIMEOUT,
    request_deadline: float = DEFAULT_REQUEST_DEADLINE,
    cleanup_hook: Callable[[Path], None] | None = None,
) -> UnixHTTPServer:
    if isinstance(max_threads, bool) or not isinstance(max_threads, int) or not 1 <= max_threads <= 256:
        raise ValueError("max_threads must be between 1 and 256")
    if isinstance(client_timeout, bool) or not isinstance(client_timeout, (int, float)) or not 0.1 <= client_timeout <= 60:
        raise ValueError("client_timeout must be between 0.1 and 60 seconds")
    if isinstance(request_deadline, bool) or not isinstance(request_deadline, (int, float)) or not 0.05 <= request_deadline <= 300:
        raise ValueError("request_deadline must be between 0.05 and 300 seconds")
    router = LocalAPIRouter(
        query, clock=clock, provider_registry=provider_registry,
        allowed_origins=allowed_origins,
    )
    return UnixHTTPServer(
        Path(socket_path), router, max_threads=max_threads,
        client_timeout=client_timeout,
        request_deadline=float(request_deadline), cleanup_hook=cleanup_hook,
    )


def create_tcp_server(
    query: QueryService,
    *,
    port: int = 0,
    bearer_token: str | None = None,
    token_path: str | Path | None = None,
    allowed_origins: Collection[str] = (),
    clock: Callable[[], datetime] | None = None,
    provider_registry: Any = default_registry,
    max_threads: int = DEFAULT_MAX_THREADS,
    client_timeout: float = DEFAULT_CLIENT_TIMEOUT,
    request_deadline: float = DEFAULT_REQUEST_DEADLINE,
    rate_limit_capacity: int = DEFAULT_RATE_LIMIT_CAPACITY,
    rate_limit_refill_per_second: float = DEFAULT_RATE_LIMIT_REFILL_PER_SECOND,
    monotonic: Callable[[], float] = time.monotonic,
) -> LoopbackHTTPServer:
    """Create an explicitly opted-in IPv4 loopback server.

    The returned ``bearer_token`` attribute is for the owning process only; it
    is never logged or included in an HTTP response.
    """
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    if bearer_token is None and token_path is None:
        raise ValueError("token_path is required when generating a bearer token")
    if isinstance(max_threads, bool) or not isinstance(max_threads, int) or not 1 <= max_threads <= 256:
        raise ValueError("max_threads must be between 1 and 256")
    if isinstance(client_timeout, bool) or not isinstance(client_timeout, (int, float)) or not 0.1 <= client_timeout <= 60:
        raise ValueError("client_timeout must be between 0.1 and 60 seconds")
    if isinstance(request_deadline, bool) or not isinstance(request_deadline, (int, float)) or not 0.05 <= request_deadline <= 300:
        raise ValueError("request_deadline must be between 0.05 and 300 seconds")
    if token_path is None:
        token = _validate_token(bearer_token or "")
    else:
        token = _load_or_create_token(Path(token_path), bearer_token)
    verifier = lambda candidate: hmac.compare_digest(token, candidate)
    rate_limiter = TokenBucket(
        rate_limit_capacity,
        rate_limit_refill_per_second,
        monotonic=monotonic,
    )
    router = LocalAPIRouter(
        query, clock=clock, provider_registry=provider_registry,
        bearer_verifier=verifier, rate_limiter=rate_limiter,
        allowed_origins=allowed_origins,
    )
    server = LoopbackHTTPServer(
        router, port, max_threads=max_threads, client_timeout=client_timeout,
        request_deadline=float(request_deadline),
    )
    router.tcp_port = server.server_address[1]
    server.bearer_token = token
    return server
