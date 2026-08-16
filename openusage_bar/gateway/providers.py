"""Bounded, provider-native adapters for the optional Gateway."""

from __future__ import annotations

import json
import math
import socket
import time
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..network import UnsafeEndpoint, resolve_public_addresses, validate_endpoint


MAX_PROVIDER_REQUEST_BYTES = 4 * 1024 * 1024
MAX_PROVIDER_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_PROVIDER_RESPONSE_HEADER_COUNT = 64
MAX_PROVIDER_RESPONSE_HEADER_BYTES = 64 * 1024
MAX_PROVIDER_RESPONSE_CHUNKS = 4096
PROVIDER_TIMEOUT_SECONDS = 15.0
_READ_CHUNK_BYTES = 64 * 1024

_ENDPOINTS = {
    "openai": "https://api.openai.com/v1/responses",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "deepseek": "https://api.deepseek.com/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "ollama": "http://127.0.0.1:11434/api/chat",
}
_ERROR_CODES = frozenset(
    {
        "credential_unavailable",
        "invalid_request",
        "invalid_upstream_response",
        "unsafe_endpoint",
        "unsupported_provider",
        "upstream_authentication_failed",
        "upstream_client_error",
        "upstream_rate_limited",
        "upstream_server_error",
        "upstream_timeout",
        "upstream_unavailable",
    }
)
_FORBIDDEN_BODY_FIELDS = frozenset(
    {
        "apikey",
        "accesstoken",
        "authorization",
        "clientsecret",
        "cookie",
        "credential",
        "endpoint",
        "header",
        "headers",
        "password",
        "proxyauthorization",
        "refreshtoken",
        "secret",
        "sessioncookie",
        "token",
        "xapikey",
    }
)


class GatewayProviderError(RuntimeError):
    """One stable, sanitized error crossing the provider boundary."""

    def __init__(self, code: str, retryable: bool) -> None:
        if code not in _ERROR_CODES or type(retryable) is not bool:
            raise ValueError("invalid Gateway provider error")
        self.code = code
        self.retryable = retryable
        super().__init__("provider request failed")

    def __str__(self) -> str:
        return "provider request failed"

    def __repr__(self) -> str:
        return (
            "GatewayProviderError("
            f"code={self.code!r}, retryable={self.retryable!r})"
        )


@dataclass(frozen=True, repr=False)
class ProviderResult:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body_chunks: tuple[bytes, ...]

    def __repr__(self) -> str:
        return (
            "ProviderResult("
            f"status_code={self.status_code!r}, "
            f"header_count={len(self.headers)!r}, "
            f"body_chunk_count={len(self.body_chunks)!r})"
        )


class ProviderEventStream(Iterator[bytes]):
    """Closeable, pull-driven ownership boundary for one upstream body.

    The public representation intentionally reports only bounded structural
    metadata.  Provider headers and body fragments can contain credentials,
    account-correlated request IDs, or response content and are never
    represented.
    """

    __slots__ = (
        "_before_pull",
        "_chunk_count",
        "_chunks",
        "_close_upstream",
        "_closed",
        "_deadline",
        "_max_bytes",
        "_max_chunks",
        "_monotonic",
        "_total_bytes",
        "headers",
        "status_code",
    )

    def __init__(
        self,
        *,
        status_code: int,
        headers: tuple[tuple[str, str], ...],
        chunks: Iterable[bytes],
        close_upstream: Callable[[], None] | None = None,
        max_bytes: int = MAX_PROVIDER_RESPONSE_BYTES,
        max_chunks: int = MAX_PROVIDER_RESPONSE_CHUNKS,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        before_pull: Callable[[float | None], None] | None = None,
    ) -> None:
        if type(status_code) is not int or not 100 <= status_code <= 599:
            raise ValueError("invalid provider event stream")
        if type(headers) is not tuple:
            raise ValueError("invalid provider event stream")
        if len(headers) > MAX_PROVIDER_RESPONSE_HEADER_COUNT:
            raise ValueError("invalid provider event stream")
        try:
            header_bytes = _response_header_bytes(headers)
        except _InvalidTransportResponse:
            raise ValueError("invalid provider event stream") from None
        if header_bytes > MAX_PROVIDER_RESPONSE_HEADER_BYTES:
            raise ValueError("invalid provider event stream")
        for name, value in headers:
            if any(character in name or character in value for character in "\x00\r\n"):
                raise ValueError("invalid provider event stream")
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_PROVIDER_RESPONSE_BYTES:
            raise ValueError("invalid provider event stream")
        if type(max_chunks) is not int or not 1 <= max_chunks <= MAX_PROVIDER_RESPONSE_CHUNKS:
            raise ValueError("invalid provider event stream")
        if deadline is not None:
            if (
                isinstance(deadline, bool)
                or not isinstance(deadline, (int, float))
                or not math.isfinite(float(deadline))
            ):
                raise ValueError("invalid provider event stream")
        if not callable(monotonic):
            raise ValueError("invalid provider event stream")
        if close_upstream is not None and not callable(close_upstream):
            raise ValueError("invalid provider event stream")
        if before_pull is not None and not callable(before_pull):
            raise ValueError("invalid provider event stream")
        try:
            source = iter(chunks)
        except Exception:
            raise ValueError("invalid provider event stream") from None

        self.status_code = status_code
        self.headers = headers
        self._chunks = source
        self._close_upstream = close_upstream
        self._max_bytes = max_bytes
        self._max_chunks = max_chunks
        self._deadline = None if deadline is None else float(deadline)
        self._monotonic = monotonic
        self._before_pull = before_pull
        self._total_bytes = 0
        self._chunk_count = 0
        self._closed = False

    def __iter__(self) -> ProviderEventStream:
        return self

    def __next__(self) -> bytes:
        if self._closed:
            raise StopIteration
        remaining = self._remaining()
        if remaining is not None and remaining <= 0:
            self.close()
            raise GatewayProviderError("upstream_timeout", True)
        try:
            if self._before_pull is not None:
                self._before_pull(remaining)
            chunk = next(self._chunks)
        except StopIteration:
            expired = self._expired()
            self.close()
            if expired:
                raise GatewayProviderError("upstream_timeout", True) from None
            raise
        except GatewayProviderError:
            self.close()
            raise
        except (TimeoutError, socket.timeout):
            self.close()
            raise GatewayProviderError("upstream_timeout", True) from None
        except urllib.error.URLError as error:
            self.close()
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise GatewayProviderError("upstream_timeout", True) from None
            raise GatewayProviderError("upstream_unavailable", True) from None
        except _InvalidTransportResponse:
            self.close()
            raise GatewayProviderError("invalid_upstream_response", False) from None
        except OSError:
            self.close()
            raise GatewayProviderError("upstream_unavailable", True) from None
        except Exception:
            self.close()
            raise GatewayProviderError("upstream_unavailable", True) from None

        if self._expired():
            self.close()
            raise GatewayProviderError("upstream_timeout", True)
        if type(chunk) is not bytes or not chunk:
            self.close()
            raise GatewayProviderError("invalid_upstream_response", False)
        self._chunk_count += 1
        self._total_bytes += len(chunk)
        if (
            self._chunk_count > self._max_chunks
            or self._total_bytes > self._max_bytes
        ):
            self.close()
            raise GatewayProviderError("invalid_upstream_response", False)
        return chunk

    def _remaining(self) -> float | None:
        if self._deadline is None:
            return None
        try:
            remaining = self._deadline - float(self._monotonic())
        except Exception:
            return 0.0
        return remaining if math.isfinite(remaining) else 0.0

    def _expired(self) -> bool:
        remaining = self._remaining()
        return remaining is not None and remaining <= 0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        source_close = getattr(self._chunks, "close", None)
        if callable(source_close):
            try:
                source_close()
            except Exception:
                pass
        if self._close_upstream is not None:
            try:
                self._close_upstream()
            except Exception:
                pass

    def __repr__(self) -> str:
        return "ProviderEventStream(<sanitized>)"


class ProviderTransport(Protocol):
    def send(
        self,
        *,
        method: str,
        endpoint: str,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
        max_response_header_count: int,
        max_response_header_bytes: int,
        max_response_bytes: int,
        max_response_chunks: int,
        allow_redirects: bool,
    ) -> ProviderResult | ProviderEventStream: ...


class GatewayProvider(Protocol):
    provider_id: str
    endpoint: str
    credential_account: str | None

    def public_descriptor(self) -> dict[str, object]: ...

    def call(
        self,
        request_body: bytes,
        *,
        credential: str,
    ) -> ProviderResult | ProviderEventStream: ...


class _InvalidTransportResponse(RuntimeError):
    pass


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


def _response_header_bytes(headers: tuple[tuple[str, str], ...]) -> int:
    total = 0
    for item in headers:
        if type(item) is not tuple or len(item) != 2:
            raise _InvalidTransportResponse()
        name, value = item
        if type(name) is not str or type(value) is not str:
            raise _InvalidTransportResponse()
        try:
            total += len(name.encode("utf-8")) + len(value.encode("utf-8")) + 4
        except UnicodeEncodeError:
            raise _InvalidTransportResponse() from None
    return total


def _set_remaining_socket_timeout(response: object, remaining: float) -> None:
    """Best-effort total-deadline tightening for urllib response sockets."""

    candidates = (
        getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
        getattr(getattr(response, "fp", None), "_sock", None),
    )
    for candidate in candidates:
        if candidate is not None and hasattr(candidate, "settimeout"):
            try:
                candidate.settimeout(max(0.001, remaining))
            except OSError:
                pass
            return


class _UrllibProviderTransport:
    """Raw standard-library HTTP transport with fixed resource limits."""

    def __init__(
        self,
        *,
        opener: Any | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )
        self._monotonic = monotonic

    def send(
        self,
        *,
        method: str,
        endpoint: str,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
        max_response_header_count: int,
        max_response_header_bytes: int,
        max_response_bytes: int,
        max_response_chunks: int,
        allow_redirects: bool,
    ) -> ProviderResult | ProviderEventStream:
        if allow_redirects:
            raise ValueError("provider redirects are disabled")
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers=headers,
            method=method,
        )
        deadline = self._monotonic() + timeout_seconds
        try:
            response = self._opener.open(request, timeout=timeout_seconds)
        except urllib.error.HTTPError as error:
            response = error
        try:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            raw_headers = tuple(response.headers.items())
            if (
                len(raw_headers) > max_response_header_count
                or _response_header_bytes(raw_headers) > max_response_header_bytes
            ):
                raise _InvalidTransportResponse()
        except Exception:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            raise

        accept = headers.get("Accept", "").split(";", 1)[0].strip().casefold()
        if accept in {"text/event-stream", "application/x-ndjson"}:
            def body_chunks() -> Iterator[bytes]:
                while True:
                    chunk = response.read(_READ_CHUNK_BYTES)
                    if not chunk:
                        return
                    yield chunk

            close = getattr(response, "close", None)
            if not callable(close):
                raise _InvalidTransportResponse()
            try:
                return ProviderEventStream(
                    status_code=status,
                    headers=raw_headers,
                    chunks=body_chunks(),
                    close_upstream=close,
                    max_bytes=max_response_bytes,
                    max_chunks=max_response_chunks,
                    deadline=deadline,
                    monotonic=self._monotonic,
                    before_pull=lambda remaining: _set_remaining_socket_timeout(
                        response,
                        timeout_seconds if remaining is None else remaining,
                    ),
                )
            except Exception:
                try:
                    close()
                except Exception:
                    pass
                raise

        with response:
            chunks: list[bytes] = []
            total = 0
            while True:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                _set_remaining_socket_timeout(response, remaining)
                chunk = response.read(
                    min(_READ_CHUNK_BYTES, max_response_bytes + 1 - total)
                )
                if self._monotonic() >= deadline:
                    raise TimeoutError()
                if not chunk:
                    break
                if type(chunk) is not bytes:
                    raise _InvalidTransportResponse()
                chunks.append(chunk)
                total += len(chunk)
                if total > max_response_bytes or len(chunks) > max_response_chunks:
                    raise _InvalidTransportResponse()
            return ProviderResult(
                status_code=status,
                headers=raw_headers,
                body_chunks=tuple(chunks),
            )


def _error(code: str, retryable: bool) -> GatewayProviderError:
    return GatewayProviderError(code, retryable)


def _reject_constant(_value: str) -> None:
    raise ValueError("non-standard JSON value")


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json_object(raw: bytes, *, maximum: int) -> dict[str, Any]:
    if type(raw) is not bytes or not raw or len(raw) > maximum:
        raise ValueError("invalid JSON body")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ValueError("invalid JSON body") from None
    if type(value) is not dict:
        raise ValueError("invalid JSON body")
    return value


def _normalized_field_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _valid_text(value: object, maximum: int) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= maximum
        and value == value.strip()
        and not any(
            unicodedata.category(character).startswith("C") for character in value
        )
    )


def _validate_provider_request(provider_id: str, raw: bytes) -> tuple[dict[str, Any], bool, bool]:
    try:
        payload = _json_object(raw, maximum=MAX_PROVIDER_REQUEST_BYTES)
    except ValueError:
        raise _error("invalid_request", False) from None

    for key in payload:
        if type(key) is not str:
            raise _error("invalid_request", False)
        if _normalized_field_name(key) in _FORBIDDEN_BODY_FIELDS:
            raise _error("invalid_request", False)
    if not _valid_text(payload.get("model"), 256):
        raise _error("invalid_request", False)

    explicit_stream = "stream" in payload
    if explicit_stream and type(payload["stream"]) is not bool:
        raise _error("invalid_request", False)
    stream = payload.get("stream", provider_id == "ollama")

    if provider_id == "openai":
        valid = "input" in payload
    elif provider_id == "anthropic":
        max_tokens = payload.get("max_tokens")
        valid = (
            type(max_tokens) is int
            and 1 <= max_tokens <= 2**31 - 1
            and type(payload.get("messages")) is list
        )
    else:
        valid = type(payload.get("messages")) is list
    if not valid:
        raise _error("invalid_request", False)
    return payload, stream, explicit_stream


def _valid_credential(value: object) -> bool:
    if type(value) is not str or not value or not value.isascii():
        return False
    if len(value.encode("ascii")) > 64 * 1024:
        return False
    return all(0x21 <= ord(character) <= 0x7E for character in value)


def validate_provider_endpoint(
    provider_id: str,
    endpoint: str,
    *,
    resolver: Callable[[str], list[str]] = resolve_public_addresses,
) -> str:
    """Validate a fixed provider endpoint without broadening cloud SSRF rules."""

    expected = _ENDPOINTS.get(provider_id)
    if expected is None or type(endpoint) is not str or endpoint != expected:
        raise _error("unsafe_endpoint", False)
    if provider_id == "ollama":
        return endpoint
    try:
        return validate_endpoint(endpoint, resolver=resolver)
    except (UnsafeEndpoint, OSError, ValueError, TypeError):
        raise _error("unsafe_endpoint", False) from None


def _status_error(status: int) -> GatewayProviderError:
    if status in {401, 403}:
        return _error("upstream_authentication_failed", False)
    if status == 429:
        return _error("upstream_rate_limited", True)
    if 300 <= status < 500:
        return _error("upstream_client_error", False)
    if 500 <= status <= 599:
        return _error("upstream_server_error", True)
    return _error("invalid_upstream_response", False)


def _stream_text(body_chunks: tuple[bytes, ...]) -> str:
    try:
        text = b"".join(body_chunks).decode("utf-8")
    except UnicodeDecodeError:
        raise _error("invalid_upstream_response", False) from None
    if not text or "\x00" in text:
        raise _error("invalid_upstream_response", False)
    return text.removeprefix("\ufeff")


def _validate_sse(body_chunks: tuple[bytes, ...]) -> None:
    text = _stream_text(body_chunks).replace("\r\n", "\n").replace("\r", "\n")
    if not text.endswith("\n\n"):
        raise _error("invalid_upstream_response", False)
    event_count = 0
    for block in text.split("\n\n"):
        if not block:
            continue
        data_lines: list[str] = []
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if not separator or field not in {"data", "event", "id", "retry"}:
                raise _error("invalid_upstream_response", False)
            if field != "data":
                continue
            data_lines.append(value[1:] if value.startswith(" ") else value)
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        if not data:
            raise _error("invalid_upstream_response", False)
        if data != "[DONE]":
            try:
                _json_object(
                    data.encode("utf-8"),
                    maximum=MAX_PROVIDER_RESPONSE_BYTES,
                )
            except (UnicodeEncodeError, ValueError):
                raise _error("invalid_upstream_response", False) from None
        event_count += 1
    if event_count == 0:
        raise _error("invalid_upstream_response", False)


def _validate_ndjson(body_chunks: tuple[bytes, ...]) -> None:
    text = _stream_text(body_chunks)
    object_count = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            _json_object(
                line.encode("utf-8"),
                maximum=MAX_PROVIDER_RESPONSE_BYTES,
            )
        except (UnicodeEncodeError, ValueError):
            raise _error("invalid_upstream_response", False) from None
        object_count += 1
    if object_count == 0:
        raise _error("invalid_upstream_response", False)


def _validate_non_stream_response(
    provider_id: str,
    body_chunks: tuple[bytes, ...],
) -> None:
    try:
        payload = _json_object(
            b"".join(body_chunks),
            maximum=MAX_PROVIDER_RESPONSE_BYTES,
        )
    except ValueError:
        raise _error("invalid_upstream_response", False) from None

    if provider_id == "openai":
        valid = payload.get("object") == "response" and type(payload.get("output")) is list
    elif provider_id == "anthropic":
        valid = payload.get("type") == "message" and type(payload.get("content")) is list
    elif provider_id in {"deepseek", "openrouter"}:
        valid = (
            payload.get("object") == "chat.completion"
            and type(payload.get("choices")) is list
        )
    elif provider_id == "ollama":
        valid = type(payload.get("message")) is dict and type(payload.get("done")) is bool
    else:
        valid = False
    if not valid:
        raise _error("invalid_upstream_response", False)


def _bounded_success_result(
    result: object,
    *,
    provider_id: str,
    stream: bool,
) -> ProviderResult | ProviderEventStream:
    if not isinstance(result, (ProviderResult, ProviderEventStream)):
        raise _error("invalid_upstream_response", False)
    live = isinstance(result, ProviderEventStream)
    try:
        if type(result.status_code) is not int or not 100 <= result.status_code <= 599:
            raise _error("invalid_upstream_response", False)
        if not 200 <= result.status_code <= 299:
            raise _status_error(result.status_code)
        if type(result.headers) is not tuple:
            raise _error("invalid_upstream_response", False)
        if len(result.headers) > MAX_PROVIDER_RESPONSE_HEADER_COUNT:
            raise _error("invalid_upstream_response", False)
        if _response_header_bytes(result.headers) > MAX_PROVIDER_RESPONSE_HEADER_BYTES:
            raise _error("invalid_upstream_response", False)
        content_types: list[str] = []
        for name, value in result.headers:
            if any(character in name or character in value for character in "\x00\r\n"):
                raise _error("invalid_upstream_response", False)
            if name.casefold() == "content-type":
                content_types.append(value)
        if len(content_types) != 1:
            raise _error("invalid_upstream_response", False)

        content_type = content_types[0]
        media_type = content_type.split(";", 1)[0].strip().casefold()
        if stream:
            expected = (
                "application/x-ndjson"
                if provider_id == "ollama"
                else "text/event-stream"
            )
            if media_type != expected:
                raise _error("invalid_upstream_response", False)
            if live:
                return ProviderEventStream(
                    status_code=result.status_code,
                    headers=(("content-type", content_type),),
                    chunks=result,
                )
        elif live:
            raise _error("invalid_upstream_response", False)
        else:
            if media_type != "application/json":
                raise _error("invalid_upstream_response", False)

        if not isinstance(result, ProviderResult) or type(result.body_chunks) is not tuple:
            raise _error("invalid_upstream_response", False)
        if len(result.body_chunks) > MAX_PROVIDER_RESPONSE_CHUNKS:
            raise _error("invalid_upstream_response", False)
        total = 0
        for chunk in result.body_chunks:
            if type(chunk) is not bytes:
                raise _error("invalid_upstream_response", False)
            total += len(chunk)
            if total > MAX_PROVIDER_RESPONSE_BYTES:
                raise _error("invalid_upstream_response", False)
        if not result.body_chunks or total == 0:
            raise _error("invalid_upstream_response", False)

        if stream:
            if provider_id == "ollama":
                _validate_ndjson(result.body_chunks)
            else:
                _validate_sse(result.body_chunks)
        else:
            _validate_non_stream_response(provider_id, result.body_chunks)

        return ProviderResult(
            status_code=result.status_code,
            headers=(("content-type", content_type),),
            body_chunks=result.body_chunks,
        )
    except _InvalidTransportResponse:
        if live:
            result.close()
        raise _error("invalid_upstream_response", False) from None
    except Exception:
        if live:
            result.close()
        raise


@dataclass(frozen=True)
class GatewayProviderAdapter:
    provider_id: str
    display_name: str
    api_family: str
    endpoint: str
    credential_account: str | None
    auth_header: str | None
    auth_scheme: str | None
    local: bool
    transport: ProviderTransport = field(repr=False, compare=False)
    resolver: Callable[[str], list[str]] = field(repr=False, compare=False)

    def public_descriptor(self) -> dict[str, object]:
        return {
            "providerId": self.provider_id,
            "displayName": self.display_name,
            "apiFamily": self.api_family,
            "streaming": True,
            "local": self.local,
        }

    def call(
        self,
        request_body: bytes,
        *,
        credential: str,
    ) -> ProviderResult | ProviderEventStream:
        payload, stream, _explicit_stream = _validate_provider_request(
            self.provider_id, request_body
        )
        del payload
        if self.local:
            if credential != "":
                raise _error("invalid_request", False)
        elif not _valid_credential(credential):
            raise _error("credential_unavailable", False)

        endpoint = validate_provider_endpoint(
            self.provider_id,
            self.endpoint,
            resolver=self.resolver,
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": (
                "application/x-ndjson"
                if self.local and stream
                else "text/event-stream"
                if stream
                else "application/json"
            ),
        }
        if self.auth_header is not None:
            headers[self.auth_header] = (
                f"{self.auth_scheme} {credential}"
                if self.auth_scheme is not None
                else credential
            )
        if self.provider_id == "anthropic":
            headers["anthropic-version"] = "2023-06-01"

        try:
            raw_result = self.transport.send(
                method="POST",
                endpoint=endpoint,
                headers=headers,
                body=request_body,
                timeout_seconds=PROVIDER_TIMEOUT_SECONDS,
                max_response_header_count=MAX_PROVIDER_RESPONSE_HEADER_COUNT,
                max_response_header_bytes=MAX_PROVIDER_RESPONSE_HEADER_BYTES,
                max_response_bytes=MAX_PROVIDER_RESPONSE_BYTES,
                max_response_chunks=MAX_PROVIDER_RESPONSE_CHUNKS,
                allow_redirects=False,
            )
        except GatewayProviderError:
            raise
        except (TimeoutError, socket.timeout):
            raise _error("upstream_timeout", True) from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise _error("upstream_timeout", True) from None
            raise _error("upstream_unavailable", True) from None
        except _InvalidTransportResponse:
            raise _error("invalid_upstream_response", False) from None
        except OSError:
            raise _error("upstream_unavailable", True) from None
        except Exception:
            raise _error("upstream_unavailable", True) from None
        return _bounded_success_result(
            raw_result,
            provider_id=self.provider_id,
            stream=stream,
        )


def default_gateway_providers(
    *,
    transport: ProviderTransport | None = None,
    resolver: Callable[[str], list[str]] = resolve_public_addresses,
) -> tuple[GatewayProviderAdapter, ...]:
    """Return the exact v1 provider set with injectable, offline-safe seams."""

    resolved_transport = transport if transport is not None else _UrllibProviderTransport()
    if not callable(resolver) or not callable(getattr(resolved_transport, "send", None)):
        raise ValueError("invalid Gateway provider dependency")

    common = {"transport": resolved_transport, "resolver": resolver}
    return (
        GatewayProviderAdapter(
            provider_id="openai",
            display_name="OpenAI",
            api_family="responses",
            endpoint=_ENDPOINTS["openai"],
            credential_account="openai.gateway-api-key",
            auth_header="Authorization",
            auth_scheme="Bearer",
            local=False,
            **common,
        ),
        GatewayProviderAdapter(
            provider_id="anthropic",
            display_name="Anthropic",
            api_family="messages",
            endpoint=_ENDPOINTS["anthropic"],
            credential_account="anthropic.gateway-api-key",
            auth_header="x-api-key",
            auth_scheme=None,
            local=False,
            **common,
        ),
        GatewayProviderAdapter(
            provider_id="deepseek",
            display_name="DeepSeek",
            api_family="chat-completions",
            endpoint=_ENDPOINTS["deepseek"],
            credential_account="deepseek.gateway-api-key",
            auth_header="Authorization",
            auth_scheme="Bearer",
            local=False,
            **common,
        ),
        GatewayProviderAdapter(
            provider_id="openrouter",
            display_name="OpenRouter",
            api_family="chat-completions",
            endpoint=_ENDPOINTS["openrouter"],
            credential_account="openrouter.gateway-api-key",
            auth_header="Authorization",
            auth_scheme="Bearer",
            local=False,
            **common,
        ),
        GatewayProviderAdapter(
            provider_id="ollama",
            display_name="Ollama",
            api_family="ollama-chat",
            endpoint=_ENDPOINTS["ollama"],
            credential_account=None,
            auth_header=None,
            auth_scheme=None,
            local=True,
            **common,
        ),
    )


__all__ = [
    "GatewayProvider",
    "GatewayProviderAdapter",
    "GatewayProviderError",
    "MAX_PROVIDER_REQUEST_BYTES",
    "MAX_PROVIDER_RESPONSE_BYTES",
    "ProviderEventStream",
    "ProviderResult",
    "ProviderTransport",
    "default_gateway_providers",
    "validate_provider_endpoint",
]
