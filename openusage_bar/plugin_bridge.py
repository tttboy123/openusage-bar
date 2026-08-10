"""Bounded stdio bridge for independently authenticated Plugin API clients."""

from __future__ import annotations

import http.client
import io
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import BinaryIO, Callable

from openusage_bar.windows_file_security import native_windows_file_security
from openusage_bar.plugin.contracts import (
    CAPABILITY_IDS,
    ContractError,
    sanitize_response as sanitize_plugin_response,
)


BRIDGE_MODES = ("loom-stdio", "codex-stdio", "claude-code-stdio")

_PRINCIPAL_BY_MODE = {
    "loom-stdio": "loom",
    "codex-stdio": "codex",
    "claude-code-stdio": "claude_code",
}
_HOST = "127.0.0.1"
_PORT = 17_824
_MAX_REQUEST_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_HEADER_BYTES = 16 * 1024
_DEADLINE_SECONDS = 5.0
_GET_ROUTES = {
    "/plugin/v1/capabilities",
    "/plugin/v1/schema",
}
_POST_ROUTES = {
    "/plugin/v1/health/query",
    "/plugin/v1/outcomes",
    "/plugin/v1/quotas/query",
    "/plugin/v1/route-advice",
    "/plugin/v1/usage/query",
}
_IDEMPOTENT_ROUTES = {
    "/plugin/v1/outcomes",
    "/plugin/v1/route-advice",
}
_REQUEST_ID = re.compile(r"request_[0-9a-f]{32}\Z")
_IDEMPOTENCY_KEY = re.compile(r"idem_[0-9a-f]{32}\Z")
_DECISION_ROUTE = re.compile(
    r"/plugin/v1/decisions/decision_[0-9a-f]{32}\Z"
)


class _BridgeFailure(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        parsed = _parse_arguments(arguments)
        if parsed[1] == "self-test":
            _synthetic_self_test(parsed[0])
            _write_public_json(
                sys.stdout,
                {
                    "apiVersion": "plugin-self-test/v1",
                    "object": "plugin.bridge_self_test",
                    "ok": True,
                    "synthetic": True,
                    "mode": parsed[0],
                },
            )
            return 0
        if parsed[1] == "dry-run":
            _write_public_json(
                sys.stdout,
                {
                    "apiVersion": "plugin-bridge-config.openusage/v1",
                    "object": "plugin.bridge_config_snippet",
                    "mode": parsed[0],
                    "command": "openusage-plugin-bridge",
                    "args": [parsed[0]],
                    "installed": False,
                    "applied": False,
                },
            )
            return 0
        status = _serve_stdio(
            parsed[0],
            stdin=sys.stdin.buffer,
            stdout=sys.stdout.buffer,
        )
        if status != 0:
            raise _BridgeFailure
        return 0
    except _BridgeFailure:
        sys.stderr.write("plugin bridge failed\n")
        return 1
    except Exception:
        sys.stderr.write("plugin bridge invocation failed\n")
        return 2


def _parse_arguments(arguments: list[str]) -> tuple[str, str]:
    if not arguments or arguments[0] not in BRIDGE_MODES:
        raise ValueError
    if arguments[1:] == ["--self-test", "--format", "json"]:
        return arguments[0], "self-test"
    if arguments[1:] == ["--dry-run", "--format", "json"]:
        return arguments[0], "dry-run"
    if len(arguments) == 1:
        return arguments[0], "serve"
    raise ValueError


def _synthetic_self_test(mode: str) -> None:
    request_id = "request_" + "0" * 32
    submitted = _canonical_json_bytes({
        "requestId": request_id,
        "method": "GET",
        "route": "/plugin/v1/capabilities",
        "body": None,
        "idempotencyKey": None,
    }) + b"\n"
    output = io.BytesIO()

    def synthetic_request(
        principal: str,
        _method: str,
        _route: str,
        _body: dict[str, object] | None,
        _idempotency_key: str | None,
    ) -> tuple[int, dict[str, object]]:
        return 200, {
            "apiVersion": "plugin.openusage/v1",
            "object": "plugin.capabilities",
            "principal": principal,
            "capabilities": list(CAPABILITY_IDS),
        }

    if _serve_stdio(
        mode,
        stdin=io.BytesIO(submitted),
        stdout=output,
        request=synthetic_request,
    ) != 0:
        raise _BridgeFailure
    actual = _load_exact_json(
        output.getvalue().rstrip(b"\n"),
        maximum_bytes=_MAX_RESPONSE_BYTES,
    )
    expected = {
        "apiVersion": "plugin-bridge.openusage/v1",
        "object": "plugin.bridge_response",
        "requestId": request_id,
        "status": 200,
        "body": synthetic_request(_PRINCIPAL_BY_MODE[mode], "", "", None, None)[1],
    }
    if actual != expected:
        raise _BridgeFailure


def _serve_stdio(
    mode: str,
    *,
    stdin: BinaryIO,
    stdout: BinaryIO,
    request: Callable[
        [str, str, str, dict[str, object] | None, str | None],
        tuple[int, dict[str, object]],
    ] | None = None,
) -> int:
    principal = _PRINCIPAL_BY_MODE.get(mode)
    if principal is None or not hasattr(stdin, "readline") or not hasattr(stdout, "write"):
        return 1
    transport = _request_plugin if request is None else request
    while True:
        try:
            raw = stdin.readline(_MAX_REQUEST_BYTES + 2)
        except Exception:
            return 1
        if raw == b"":
            return 0
        if (
            not isinstance(raw, bytes)
            or len(raw) > _MAX_REQUEST_BYTES + 1
            or not raw.endswith(b"\n")
            or len(raw) <= 1
        ):
            return 1
        try:
            submitted = _load_exact_json(raw[:-1])
            validated = _validate_request(submitted)
            status, body = transport(principal, *validated[1:])
            if (
                type(status) is not int
                or not 100 <= status <= 599
                or type(body) is not dict
            ):
                raise _BridgeFailure
            body = _sanitize_response(
                validated[2],
                status,
                body,
                expected_principal=principal,
            )
            response = {
                "apiVersion": "plugin-bridge.openusage/v1",
                "object": "plugin.bridge_response",
                "requestId": validated[0],
                "status": status,
                "body": body,
            }
            encoded = _canonical_json_bytes(response) + b"\n"
            if len(encoded) > _MAX_RESPONSE_BYTES:
                raise _BridgeFailure
            stdout.write(encoded)
            flush = getattr(stdout, "flush", None)
            if callable(flush):
                flush()
        except Exception:
            return 1


def _validate_request(
    submitted: object,
) -> tuple[str, str, str, dict[str, object] | None, str | None]:
    if type(submitted) is not dict or set(submitted) != {
        "requestId",
        "method",
        "route",
        "body",
        "idempotencyKey",
    }:
        raise _BridgeFailure
    request_id = submitted["requestId"]
    method = submitted["method"]
    route = submitted["route"]
    body = submitted["body"]
    idempotency_key = submitted["idempotencyKey"]
    if type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None:
        raise _BridgeFailure
    if type(method) is not str or type(route) is not str:
        raise _BridgeFailure
    if method == "GET":
        if route not in _GET_ROUTES and _DECISION_ROUTE.fullmatch(route) is None:
            raise _BridgeFailure
        if body is not None or idempotency_key is not None:
            raise _BridgeFailure
    elif method == "POST":
        if route not in _POST_ROUTES or type(body) is not dict:
            raise _BridgeFailure
        if route in _IDEMPOTENT_ROUTES:
            if (
                type(idempotency_key) is not str
                or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None
            ):
                raise _BridgeFailure
        elif idempotency_key is not None:
            raise _BridgeFailure
    else:
        raise _BridgeFailure
    return request_id, method, route, body, idempotency_key


def _request_plugin(
    principal: str,
    method: str,
    route: str,
    body: dict[str, object] | None,
    idempotency_key: str | None,
) -> tuple[int, dict[str, object]]:
    token = _read_principal_token(principal)
    payload = b"" if body is None else _canonical_json_bytes(body)
    if len(payload) > _MAX_REQUEST_BYTES:
        raise _BridgeFailure
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(payload))
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    connection = http.client.HTTPConnection(
        _HOST,
        _PORT,
        timeout=_DEADLINE_SECONDS,
    )
    try:
        connection.request(method, route, body=payload or None, headers=headers)
        response = connection.getresponse()
        response_headers = response.getheaders()
        received = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(received) > _MAX_RESPONSE_BYTES:
            raise _BridgeFailure
        _validate_response_framing(response.status, response_headers, received)
        decoded = _load_exact_json(received, maximum_bytes=_MAX_RESPONSE_BYTES)
        if type(decoded) is not dict:
            raise _BridgeFailure
        return response.status, decoded
    except Exception:
        raise _BridgeFailure from None
    finally:
        connection.close()


def _validate_response_framing(
    status: int,
    headers: list[tuple[str, str]],
    body: bytes,
) -> None:
    if type(status) is not int or status not in {
        200, 400, 401, 403, 404, 405, 409, 413, 422, 429, 500, 503
    }:
        raise _BridgeFailure
    if type(headers) is not list or len(headers) > 64 or type(body) is not bytes:
        raise _BridgeFailure
    normalized: dict[str, str] = {}
    total = 0
    for item in headers:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not str
        ):
            raise _BridgeFailure
        name, value = item
        lowered = name.casefold()
        try:
            encoded_name = name.encode("ascii", "strict")
            encoded_value = value.encode("ascii", "strict")
        except UnicodeError:
            raise _BridgeFailure from None
        if (
            re.fullmatch(r"[A-Za-z0-9-]{1,128}", name) is None
            or any(byte < 0x20 or byte == 0x7F for byte in encoded_value)
            or len(encoded_value) > 8 * 1024
            or lowered in normalized
        ):
            raise _BridgeFailure
        total += len(encoded_name) + len(encoded_value)
        if total > _MAX_HEADER_BYTES:
            raise _BridgeFailure
        normalized[lowered] = value
    if "transfer-encoding" in normalized or "content-encoding" in normalized:
        raise _BridgeFailure
    if normalized.get("content-type") not in {
        "application/json",
        "application/json; charset=utf-8",
    }:
        raise _BridgeFailure
    declared = normalized.get("content-length")
    if declared is not None:
        if re.fullmatch(r"(?:0|[1-9][0-9]{0,6})", declared) is None:
            raise _BridgeFailure
        if int(declared) != len(body) or int(declared) > _MAX_RESPONSE_BYTES:
            raise _BridgeFailure


def _sanitize_response(
    route: str,
    status: int,
    payload: dict[str, object],
    *,
    expected_principal: str,
) -> dict[str, object]:
    try:
        sanitized = sanitize_plugin_response(route, status, payload)
    except ContractError:
        raise _BridgeFailure from None
    if (
        route == "/plugin/v1/capabilities"
        and sanitized.get("principal") != expected_principal
    ):
        raise _BridgeFailure
    if status >= 400:
        expected_status = {
            "invalid_request": 400,
            "invalid_target": 400,
            "invalid_header": 400,
            "invalid_idempotency_key": 400,
            "authentication_required": 401,
            "forbidden_host": 403,
            "insufficient_scope": 403,
            "not_found": 404,
            "decision_not_found": 404,
            "method_not_allowed": 405,
            "idempotency_conflict": 409,
            "idempotency_in_progress": 409,
            "decision_outcome_conflict": 409,
            "request_body_not_allowed": 413,
            "request_too_large": 413,
            "result_too_large": 422,
            "rate_limited": 429,
            "internal_error": 500,
            "dependency_unavailable": 503,
            "idempotency_capacity_exceeded": 503,
        }.get(sanitized["error"]["code"])
        if expected_status != status:
            raise _BridgeFailure
    return sanitized


def _read_principal_token(
    principal: str,
    *,
    platform_name: str | None = None,
    windows_security: object | None = None,
    token_path: Path | None = None,
) -> str:
    if principal not in {"loom", "codex", "claude_code"}:
        raise _BridgeFailure
    effective_platform = os.name if platform_name is None else platform_name
    if effective_platform not in {"nt", "posix"}:
        raise _BridgeFailure
    token_path = (
        _principal_token_path(principal, platform_name=effective_platform)
        if token_path is None
        else token_path
    )
    if not isinstance(token_path, Path) or not token_path.is_absolute():
        raise _BridgeFailure
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    descriptor: int | None = None
    try:
        parent = token_path.parent.lstat()
        existing = token_path.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or not stat.S_ISREG(existing.st_mode)
            or stat.S_ISLNK(existing.st_mode)
            or existing.st_nlink != 1
            or os.path.normcase(os.path.realpath(token_path.parent))
            != os.path.normcase(os.path.abspath(token_path.parent))
        ):
            raise _BridgeFailure
        descriptor = os.open(token_path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (existing.st_dev, existing.st_ino)
        ):
            raise _BridgeFailure
        if effective_platform == "nt":
            security = windows_security or native_windows_file_security()
            if security is None:
                raise _BridgeFailure
            security.verify_file(descriptor)
        else:
            current_uid = os.getuid()
            if (
                parent.st_uid != current_uid
                or stat.S_IMODE(parent.st_mode) != 0o700
                or opened.st_uid != current_uid
                or stat.S_IMODE(opened.st_mode) != 0o600
            ):
                raise _BridgeFailure
        raw = os.read(descriptor, 257)
        after = os.fstat(descriptor)
        current = token_path.lstat()
        if (
            (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            or current.st_nlink != 1
        ):
            raise _BridgeFailure
        token = raw.decode("ascii", "strict")
        if not 43 <= len(raw) <= 256 or not all(0x21 <= byte <= 0x7E for byte in raw):
            raise _BridgeFailure
        return token
    except Exception:
        raise _BridgeFailure from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _principal_token_path(
    principal: str,
    *,
    platform_name: str | None = None,
) -> Path:
    effective_platform = os.name if platform_name is None else platform_name
    if effective_platform == "nt":
        root = os.environ.get("LOCALAPPDATA")
        if not root:
            raise _BridgeFailure
        state = Path(root) / "openusage-bar"
    elif effective_platform == "posix":
        state = Path.home() / ".local" / "state" / "openusage-bar"
    else:
        raise _BridgeFailure
    return state / "plugin" / f"{principal}.token"


def _load_exact_json(
    raw: bytes,
    *,
    maximum_bytes: int = _MAX_REQUEST_BYTES,
) -> object:
    if (
        not isinstance(raw, bytes)
        or type(maximum_bytes) is not int
        or maximum_bytes not in {_MAX_REQUEST_BYTES, _MAX_RESPONSE_BYTES}
        or len(raw) > maximum_bytes
        or b"\x00" in raw
    ):
        raise _BridgeFailure

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise _BridgeFailure
            result[key] = value
        return result

    try:
        return json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(_BridgeFailure()),
        )
    except Exception:
        raise _BridgeFailure from None


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except Exception:
        raise _BridgeFailure from None


def _write_public_json(stream: object, value: object) -> None:
    encoded = _canonical_json_bytes(value).decode("ascii")
    if len(encoded) > _MAX_RESPONSE_BYTES:
        raise _BridgeFailure
    stream.write(encoded + "\n")


__all__ = ["BRIDGE_MODES", "main"]
