"""Bounded IPv4 loopback transport for the optional Gateway API.

This listener deliberately has no dependency on the read-only Local API.  It
owns a distinct bearer token and forwards authenticated requests only to the
Gateway router supplied by the daemon.
"""

from __future__ import annotations

import hmac
import json
import math
import os
import secrets
import socket
import stat
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from ..windows_file_security import native_windows_file_security


MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_REQUEST_LINE_BYTES = 8 * 1024
MAX_HEADER_COUNT = 64
MAX_HEADER_BYTES = 64 * 1024
DEFAULT_RATE_LIMIT_CAPACITY = 120
DEFAULT_RATE_LIMIT_REFILL_PER_SECOND = 2.0
DEFAULT_MAX_THREADS = 32
DEFAULT_CLIENT_TIMEOUT = 5.0
DEFAULT_REQUEST_DEADLINE = 15.0
RUNTIME_CAPABILITY_MEDIA_TYPE = (
    "application/vnd.openusage.runtime-capability+json"
)
_CONTROL = frozenset(range(0x20)) | {0x7F}
_HEADER_NAME_BYTES = frozenset(
    b"!#$%&'*+-.0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ^_`abcdefghijklmnopqrstuvwxyz|~"
)
_WINDOWS_FILE_SECURITY = native_windows_file_security()


class _Router(Protocol):
    def dispatch(
        self, method: str, path: str, body: bytes
    ) -> tuple[int, dict[str, object]]: ...

    def dispatch_sse(
        self, method: str, path: str, body: bytes
    ) -> tuple[int, bytes]: ...

    def dispatch_events(
        self, method: str, path: str, body: bytes
    ) -> tuple[
        int,
        Iterable[dict[str, object]] | dict[str, object],
    ]: ...

    def dispatch_runtime_capability(
        self,
        method: str,
        path: str,
        body: bytes,
        *,
        listener_operational: str | None = None,
    ) -> tuple[int, dict[str, object]]: ...


class _TokenBucket:
    """One thread-safe admission bucket owned by one Gateway listener."""

    def __init__(
        self,
        capacity: int,
        refill_per_second: float,
        *,
        monotonic: Callable[[], float],
    ) -> None:
        if (
            isinstance(capacity, bool)
            or not isinstance(capacity, int)
            or not 1 <= capacity <= 10_000
        ):
            raise ValueError("rate limit capacity must be between 1 and 10000")
        if (
            isinstance(refill_per_second, bool)
            or not isinstance(refill_per_second, (int, float))
            or not math.isfinite(refill_per_second)
            or not 0 < refill_per_second <= 10_000
        ):
            raise ValueError("rate limit refill must be positive and bounded")
        initial = float(monotonic())
        if not math.isfinite(initial):
            raise ValueError("monotonic clock must return a finite value")
        self.capacity = capacity
        self.refill_per_second = float(refill_per_second)
        self._monotonic = monotonic
        self._tokens = float(capacity)
        self._updated = initial
        self._lock = threading.Lock()

    def consume(self) -> tuple[bool, int]:
        with self._lock:
            current = float(self._monotonic())
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
            retry_after = max(
                1,
                math.ceil((1.0 - self._tokens) / self.refill_per_second),
            )
            return False, retry_after


def _validate_token(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 43 <= len(value) <= 256
        or not value.isascii()
        or any(ord(character) < 0x21 or character.isspace() for character in value)
    ):
        raise ValueError("bearer token must be a high-entropy ASCII value")
    return value


def _prepare_private_parent(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Gateway token path must be absolute")
    try:
        path.parent.lstat()
        existed = True
    except FileNotFoundError:
        existed = False
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise OSError("Gateway token parent is unsafe")
    if hasattr(os, "getuid"):
        if parent.st_uid != os.getuid():
            raise OSError("Gateway token parent is unsafe")
        if existed:
            if stat.S_IMODE(parent.st_mode) != 0o700:
                raise OSError("Gateway token parent is unsafe")
        else:
            try:
                os.chmod(path.parent, 0o700, follow_symlinks=False)
            except (NotImplementedError, TypeError):
                os.chmod(path.parent, 0o700)
            secured = path.parent.lstat()
            if (
                stat.S_ISLNK(secured.st_mode)
                or not stat.S_ISDIR(secured.st_mode)
                or secured.st_uid != os.getuid()
                or stat.S_IMODE(secured.st_mode) != 0o700
            ):
                raise OSError("Gateway token parent is unsafe")
    if _WINDOWS_FILE_SECURITY is not None:
        try:
            _WINDOWS_FILE_SECURITY.harden_directory(path.parent)
        except Exception:
            raise OSError("Gateway token parent is unsafe") from None


def _read_token(path: Path) -> str:
    # Inspect before opening so a pre-existing FIFO cannot block the daemon
    # during startup.  The descriptor metadata is checked again below to
    # close the lstat/open race.
    unsafe = "existing Gateway token file is unsafe"
    try:
        existing = path.lstat()
    except FileNotFoundError:
        raise
    except OSError:
        raise OSError(unsafe) from None
    if (
        stat.S_ISLNK(existing.st_mode)
        or not stat.S_ISREG(existing.st_mode)
        or existing.st_nlink != 1
    ):
        raise OSError(unsafe)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError:
        raise OSError(unsafe) from None
    raw = b""
    read_error: OSError | None = None
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino)
            != (existing.st_dev, existing.st_ino)
        ):
            raise OSError(unsafe)
        if hasattr(os, "getuid") and (
            current.st_uid != os.getuid()
            or stat.S_IMODE(current.st_mode) != 0o600
        ):
            raise OSError(unsafe)
        if _WINDOWS_FILE_SECURITY is not None:
            try:
                _WINDOWS_FILE_SECURITY.harden_file(descriptor)
            except Exception:
                raise OSError(unsafe) from None
        raw = os.read(descriptor, 257)
    except OSError:
        read_error = OSError(unsafe)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            if read_error is None:
                read_error = OSError(unsafe)
    if read_error is not None:
        raise read_error from None
    try:
        return _validate_token(raw.decode("ascii"))
    except (UnicodeError, ValueError) as error:
        raise OSError(unsafe) from error


def _unlink_owned_token_node(
    path: Path,
    identity: tuple[int, int] | None,
) -> None:
    if identity is None:
        return
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISREG(current.st_mode)
        and (current.st_dev, current.st_ino) == identity
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _link_token_without_replacement(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination, follow_symlinks=False)
    except (NotImplementedError, TypeError):
        os.link(source, destination)


def _fsync_token_parent(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path.parent, flags)
    sync_error: OSError | None = None
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISDIR(current.st_mode):
            raise OSError("Gateway token parent is unsafe")
        os.fsync(descriptor)
    except OSError as error:
        sync_error = error
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            if sync_error is None:
                sync_error = error
    if sync_error is not None:
        raise sync_error


def _create_token(path: Path, token: str) -> bool:
    """Publish a complete private token atomically without replacement."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY

    descriptor: int | None = None
    temporary: Path | None = None
    identity: tuple[int, int] | None = None
    published = False
    primary_error_active = False
    try:
        for _ in range(16):
            candidate = path.parent / (
                f".{path.name}.tmp-{secrets.token_hex(16)}"
            )
            try:
                descriptor = os.open(candidate, flags, 0o600)
            except FileExistsError:
                continue
            temporary = candidate
            if hasattr(os, "fchmod"):
                try:
                    os.fchmod(descriptor, 0o600)
                except (NotImplementedError, OSError):
                    # Never chmod a pathname before fstat establishes the
                    # identity of the random node created with O_EXCL.
                    pass
            opened = os.fstat(descriptor)
            identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (
                    hasattr(os, "getuid")
                    and (
                        opened.st_uid != os.getuid()
                        or stat.S_IMODE(opened.st_mode) != 0o600
                    )
                )
            ):
                raise OSError("temporary Gateway token file is unsafe")
            if _WINDOWS_FILE_SECURITY is not None:
                try:
                    _WINDOWS_FILE_SECURITY.harden_file(descriptor)
                except Exception:
                    raise OSError(
                        "temporary Gateway token file is unsafe"
                    ) from None
            break
        else:
            raise OSError("temporary Gateway token file could not be created")

        assert descriptor is not None and temporary is not None
        content = memoryview(token.encode("ascii"))
        while content:
            written = os.write(descriptor, content)
            if written <= 0:
                raise OSError("Gateway token could not be persisted")
            content = content[written:]
        os.fsync(descriptor)
        completed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(completed.st_mode)
            or completed.st_nlink != 1
            or (
                hasattr(os, "getuid")
                and (
                    completed.st_uid != os.getuid()
                    or stat.S_IMODE(completed.st_mode) != 0o600
                )
            )
        ):
            raise OSError("temporary Gateway token file is unsafe")
        closing_descriptor = descriptor
        descriptor = None
        os.close(closing_descriptor)

        try:
            _link_token_without_replacement(temporary, path)
        except FileExistsError:
            return False
        published = True
        temporary.unlink()
        temporary = None

        final = path.lstat()
        if (
            not stat.S_ISREG(final.st_mode)
            or (final.st_dev, final.st_ino) != identity
            or final.st_nlink != 1
            or (
                hasattr(os, "getuid")
                and (
                    final.st_uid != os.getuid()
                    or stat.S_IMODE(final.st_mode) != 0o600
                )
            )
        ):
            raise OSError("published Gateway token file is unsafe")
        _fsync_token_parent(path)
        return True
    except BaseException:
        primary_error_active = True
        if published:
            try:
                _unlink_owned_token_node(path, identity)
            except BaseException:
                pass
        raise
    finally:
        close_error: BaseException | None = None
        if descriptor is not None:
            closing_descriptor = descriptor
            descriptor = None
            try:
                os.close(closing_descriptor)
            except BaseException as error:
                close_error = error
        cleanup_error: BaseException | None = None
        if temporary is not None:
            try:
                _unlink_owned_token_node(temporary, identity)
            except BaseException as error:
                cleanup_error = error
        if not primary_error_active:
            if close_error is not None:
                raise close_error
            if cleanup_error is not None:
                raise cleanup_error


def _load_or_create_token(path: Path, supplied: str | None) -> str:
    if not path.name or path.name in {".", ".."}:
        raise ValueError("Gateway token path is invalid")
    token = _validate_token(supplied) if supplied is not None else None
    _prepare_private_parent(path)
    if token is not None:
        if _create_token(path, token):
            return token
        if not hmac.compare_digest(_read_token(path), token):
            raise OSError("existing Gateway token file does not match")
        return token
    try:
        return _read_token(path)
    except FileNotFoundError:
        token = _validate_token(secrets.token_urlsafe(32))
        if _create_token(path, token):
            return token
        return _read_token(path)


def _problem(code: str, message: str, retryable: bool) -> dict[str, object]:
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        }
    }


def _sse_event(event: dict[str, object]) -> bytes:
    event_type = event.get("type")
    if type(event_type) is not str:
        raise ValueError("invalid Gateway SSE event")
    encoded = json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return b"event: " + event_type.encode("ascii") + b"\ndata: " + encoded + b"\n\n"


class _GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    default_request_version = "HTTP/1.1"
    server_version = "OpenUsageGateway/1"
    sys_version = ""

    def log_message(self, _format: str, *args: object) -> None:
        # Request targets and authorization failures are intentionally absent
        # from default process logs.
        return

    def handle_one_request(self) -> None:
        try:
            self.raw_requestline = self.rfile.readline(MAX_REQUEST_LINE_BYTES + 1)
            if len(self.raw_requestline) > MAX_REQUEST_LINE_BYTES:
                self.requestline = ""
                self.request_version = "HTTP/1.1"
                self.command = ""
                self._send_problem(
                    HTTPStatus.REQUEST_URI_TOO_LONG,
                    "invalid_request",
                    "Invalid request.",
                    False,
                )
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            self._handle_gateway_request()
            self.wfile.flush()
        except (TimeoutError, socket.timeout):
            self.close_connection = True
        except Exception:
            if not self.wfile.closed:
                self._send_problem(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "internal_error",
                    "Request could not be completed.",
                    True,
                )

    def handle_expect_100(self) -> bool:
        self._send_problem(
            HTTPStatus.EXPECTATION_FAILED,
            "invalid_header",
            "Invalid request header.",
            False,
        )
        return False

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        if len(self.requestline.split()) != 3 or self.request_version != "HTTP/1.1":
            self.request_version = "HTTP/1.1"
            self._send_problem(
                HTTPStatus.HTTP_VERSION_NOT_SUPPORTED,
                "invalid_request",
                "Invalid request.",
                False,
            )
            return False
        return True

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        del message, explain
        stable_code = (
            "invalid_header"
            if code == HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE
            else "invalid_request"
        )
        stable_message = (
            "Invalid request header."
            if stable_code == "invalid_header"
            else "Invalid request."
        )
        self._send_problem(code, stable_code, stable_message, False)

    def _handle_gateway_request(self) -> None:
        self.close_connection = True
        if not self._headers_are_bounded():
            self._send_problem(
                HTTPStatus.BAD_REQUEST,
                "invalid_header",
                "Invalid request header.",
                False,
            )
            return
        server = self.server
        assert isinstance(server, GatewayHTTPServer)
        expected_host = f"127.0.0.1:{server.server_address[1]}"
        hosts = self.headers.get_all("Host", failobj=[])
        if len(hosts) != 1 or not hmac.compare_digest(hosts[0], expected_host):
            self._send_problem(
                HTTPStatus.FORBIDDEN,
                "forbidden_host",
                "Host is not allowed.",
                False,
            )
            return
        authorizations = self.headers.get_all("Authorization", failobj=[])
        if len(authorizations) != 1 or not authorizations[0].startswith("Bearer "):
            self._authentication_required()
            return
        candidate = authorizations[0][7:]
        if not hmac.compare_digest(candidate, server.bearer_token):
            self._authentication_required()
            return

        lengths = self.headers.get_all("Content-Length", failobj=[])
        encodings = self.headers.get_all("Transfer-Encoding", failobj=[])
        if len(lengths) > 1:
            self._send_problem(
                HTTPStatus.BAD_REQUEST,
                "invalid_framing",
                "Invalid request framing.",
                False,
            )
            return
        if lengths and (
            not lengths[0].isascii()
            or not lengths[0].isdecimal()
            or len(lengths[0]) > 10
        ):
            self._send_problem(
                HTTPStatus.BAD_REQUEST,
                "invalid_framing",
                "Invalid request framing.",
                False,
            )
            return
        length = int(lengths[0]) if lengths else 0
        if self.command == "GET" and (length or encodings):
            self._send_problem(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request_body_not_allowed",
                "Request bodies are not allowed.",
                False,
            )
            return
        if encodings:
            self._send_problem(
                HTTPStatus.BAD_REQUEST,
                "invalid_framing",
                "Invalid request framing.",
                False,
            )
            return
        if length > MAX_BODY_BYTES:
            self._send_problem(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request_too_large",
                "Request is too large.",
                False,
            )
            return
        if self.command == "POST" and self.path == "/gateway/v1/should-send":
            allowed, retry_after = server.rate_limiter.consume()
            if not allowed:
                self._send_problem(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    "rate_limited",
                    "Request rate limit exceeded.",
                    True,
                    retry_after=retry_after,
                )
                return

        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            self._send_problem(
                HTTPStatus.BAD_REQUEST,
                "invalid_framing",
                "Invalid request framing.",
                False,
            )
            return
        try:
            if (
                self.command == "GET"
                and self.path == "/gateway/v1/health"
                and self._accepts_runtime_capability()
            ):
                capability_dispatch = getattr(
                    server.router,
                    "dispatch_runtime_capability",
                    None,
                )
                if callable(capability_dispatch):
                    status, payload = capability_dispatch(
                        self.command,
                        self.path,
                        body,
                        listener_operational="ready",
                    )
                else:
                    status, payload = server.router.dispatch(
                        self.command,
                        self.path,
                        body,
                    )
            elif (
                self.command == "POST"
                and self.path == "/gateway/v1/responses"
                and self._accepts_gateway_sse()
            ):
                status, delivery = server.router.dispatch_events(
                    self.command, self.path, body
                )
                if type(delivery) is not dict:
                    try:
                        self._send_sse(status, delivery)
                    except Exception:
                        # Headers may already be on the wire.  Close the
                        # connection instead of attempting a second JSON
                        # response after an encode/write/flush failure.
                        self.close_connection = True
                    return
                payload = delivery
            else:
                status, payload = server.router.dispatch(
                    self.command,
                    self.path,
                    body,
                )
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except Exception:
            self._send_problem(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "Request could not be completed.",
                True,
            )
            return
        self._send_json(status, encoded)

    def _accepts_runtime_capability(self) -> bool:
        values = self.headers.get_all("Accept", failobj=[])
        return (
            len(values) == 1
            and type(values[0]) is str
            and values[0].strip().casefold() == RUNTIME_CAPABILITY_MEDIA_TYPE
        )

    def _accepts_gateway_sse(self) -> bool:
        for value in self.headers.get_all("Accept", failobj=[]):
            if type(value) is str and any(
                item.split(";", 1)[0].strip().casefold() == "text/event-stream"
                for item in value.split(",")
            ):
                return True
        return False

    def _headers_are_bounded(self) -> bool:
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
            if (
                not raw_name
                or any(byte not in _HEADER_NAME_BYTES for byte in raw_name)
                or any(byte in _CONTROL for byte in raw_value)
            ):
                return False
            total += len(raw_name) + len(raw_value) + 4
            if total > MAX_HEADER_BYTES:
                return False
        return True

    def _authentication_required(self) -> None:
        self._send_problem(
            HTTPStatus.UNAUTHORIZED,
            "authentication_required",
            "Authentication is required.",
            False,
        )

    def _send_problem(
        self,
        status: int,
        code: str,
        message: str,
        retryable: bool,
        *,
        retry_after: int | None = None,
    ) -> None:
        encoded = json.dumps(
            _problem(code, message, retryable),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
        self._send_json(status, encoded, headers=headers)

    def _send_json(
        self,
        status: int,
        body: bytes,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.close_connection = True
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        if (
            (self.command, self.path)
            in {
                ("GET", "/gateway/v1/health"),
                ("POST", "/gateway/v1/responses"),
            }
        ):
            self.send_header("Vary", "Accept")
        if status == HTTPStatus.UNAUTHORIZED:
            self.send_header("WWW-Authenticate", "Bearer")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(
        self,
        status: int,
        events: Iterable[dict[str, object]],
    ) -> None:
        iterator: Iterable[dict[str, object]] | None = None
        try:
            iterator = iter(events)
            self.close_connection = True
            self.send_response(int(status))
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.send_header("Vary", "Accept")
            self.end_headers()
            for event in iterator:
                self.wfile.write(_sse_event(event))
                self.wfile.flush()
        finally:
            close_target = iterator if iterator is not None else events
            close = getattr(close_target, "close", None)
            if not callable(close) and close_target is not events:
                close = getattr(events, "close", None)
            if callable(close):
                close()


class _BoundedThreads:
    """Bound connection work and terminate active sockets at their deadline."""

    daemon_threads = True
    block_on_close = False

    def _configure_threads(
        self,
        maximum: int,
        client_timeout: float,
        request_deadline: float,
    ) -> None:
        self._thread_slots = threading.BoundedSemaphore(maximum)
        self._client_timeout = client_timeout
        self._request_deadline = request_deadline
        self._deadline_condition = threading.Condition()
        self._deadline_entries: dict[socket.socket, tuple[float, int]] = {}
        self._deadline_sequence = 0
        self._closing = False
        self._deadline_watchdog = threading.Thread(
            target=self._watch_request_deadlines,
            name="openusage-gateway-deadline",
            daemon=True,
        )
        self._deadline_watchdog.start()

    def get_request(self) -> tuple[socket.socket, Any]:
        request, address = super().get_request()  # type: ignore[misc]
        request.settimeout(self._client_timeout)
        return request, address

    def handle_error(self, request: socket.socket, client_address: Any) -> None:
        del request, client_address

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._thread_slots.acquire(blocking=False):
            self.shutdown_request(request)  # type: ignore[attr-defined]
            return
        try:
            super().process_request(request, client_address)  # type: ignore[misc]
        except Exception:
            self._thread_slots.release()
            raise

    def process_request_thread(
        self,
        request: socket.socket,
        client_address: Any,
    ) -> None:
        registration: tuple[float, int] | None = None
        with self._deadline_condition:
            if self._closing:
                reject = True
            else:
                self._deadline_sequence += 1
                registration = (
                    time.monotonic() + self._request_deadline,
                    self._deadline_sequence,
                )
                self._deadline_entries[request] = registration
                self._deadline_condition.notify()
                reject = False
        if reject:
            self.shutdown_request(request)  # type: ignore[attr-defined]
            self._thread_slots.release()
            return
        try:
            super().process_request_thread(request, client_address)  # type: ignore[misc]
        finally:
            with self._deadline_condition:
                if self._deadline_entries.get(request) == registration:
                    self._deadline_entries.pop(request, None)
                    self._deadline_condition.notify()
            self._thread_slots.release()

    def _watch_request_deadlines(self) -> None:
        while True:
            with self._deadline_condition:
                while True:
                    if self._closing:
                        return
                    if not self._deadline_entries:
                        self._deadline_condition.wait()
                        continue

                    request, registration = min(
                        self._deadline_entries.items(),
                        key=lambda item: item[1],
                    )
                    remaining = registration[0] - time.monotonic()
                    if remaining > 0:
                        self._deadline_condition.wait(remaining)
                        continue
                    if self._deadline_entries.get(request) != registration:
                        continue
                    self._deadline_entries.pop(request, None)
                    break
            self._expire_request(request)

    @staticmethod
    def _expire_request(request: socket.socket) -> None:
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    @property
    def active_deadline_count(self) -> int:
        with self._deadline_condition:
            return len(self._deadline_entries)

    def _abort_active_requests(self) -> None:
        with self._deadline_condition:
            self._closing = True
            active = tuple(self._deadline_entries)
            self._deadline_entries.clear()
            self._deadline_condition.notify_all()
        for request in active:
            self._expire_request(request)
        if self._deadline_watchdog is not threading.current_thread():
            self._deadline_watchdog.join()

    def server_close(self) -> None:
        self._abort_active_requests()
        try:
            super().server_close()  # type: ignore[misc]
        finally:
            close = getattr(self.router, "close", None)
            if callable(close):
                close()


class GatewayHTTPServer(_BoundedThreads, ThreadingHTTPServer):
    """One separately authenticated Gateway listener."""

    address_family = socket.AF_INET
    allow_reuse_address = False
    daemon_threads = True

    def __init__(
        self,
        router: _Router,
        *,
        host: str,
        port: int,
        bearer_token: str,
        rate_limiter: _TokenBucket,
        max_threads: int,
        client_timeout: float,
        request_deadline: float,
    ) -> None:
        self.router = router
        self.bearer_token = bearer_token
        self.rate_limiter = rate_limiter
        super().__init__((host, port), _GatewayHandler)
        self._configure_threads(max_threads, client_timeout, request_deadline)


def create_gateway_server(
    router: _Router,
    *,
    host: str = "127.0.0.1",
    port: int = 17823,
    bearer_token: str | None = None,
    token_path: str | Path,
    rate_limit_capacity: int = DEFAULT_RATE_LIMIT_CAPACITY,
    rate_limit_refill_per_second: float = DEFAULT_RATE_LIMIT_REFILL_PER_SECOND,
    monotonic: Callable[[], float] = time.monotonic,
    max_threads: int = DEFAULT_MAX_THREADS,
    client_timeout: float = DEFAULT_CLIENT_TIMEOUT,
    request_deadline: float = DEFAULT_REQUEST_DEADLINE,
) -> GatewayHTTPServer:
    """Create the authenticated, independently started Gateway listener."""

    if host != "127.0.0.1":
        raise ValueError("Gateway host must be 127.0.0.1")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("Gateway port must be between 0 and 65535")
    if (
        isinstance(max_threads, bool)
        or not isinstance(max_threads, int)
        or not 1 <= max_threads <= 256
    ):
        raise ValueError("max_threads must be between 1 and 256")
    if (
        isinstance(client_timeout, bool)
        or not isinstance(client_timeout, (int, float))
        or not math.isfinite(client_timeout)
        or not 0.1 <= client_timeout <= 60
    ):
        raise ValueError("client_timeout must be between 0.1 and 60 seconds")
    if (
        isinstance(request_deadline, bool)
        or not isinstance(request_deadline, (int, float))
        or not math.isfinite(request_deadline)
        or not 0.05 <= request_deadline <= 300
    ):
        raise ValueError("request_deadline must be between 0.05 and 300 seconds")
    limiter = _TokenBucket(
        rate_limit_capacity,
        rate_limit_refill_per_second,
        monotonic=monotonic,
    )
    token = _load_or_create_token(Path(token_path), bearer_token)
    return GatewayHTTPServer(
        router,
        host=host,
        port=port,
        bearer_token=token,
        rate_limiter=limiter,
        max_threads=max_threads,
        client_timeout=float(client_timeout),
        request_deadline=float(request_deadline),
    )


__all__ = ["GatewayHTTPServer", "create_gateway_server"]
