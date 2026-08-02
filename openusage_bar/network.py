from __future__ import annotations

import ipaddress
import http.client
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any


class NetworkError(RuntimeError):
    pass


class UnsafeEndpoint(NetworkError):
    pass


class AuthenticationRequired(NetworkError):
    pass


class RateLimited(NetworkError):
    pass


class HTTPStatusError(NetworkError):
    def __init__(self, status: int):
        if isinstance(status, bool) or not isinstance(status, int) or not 400 <= status <= 599:
            raise ValueError("HTTP status is invalid")
        super().__init__("Provider returned an unsuccessful HTTP status")
        self.status = status


class ResponseTooLarge(NetworkError):
    pass


class MalformedResponse(NetworkError):
    pass


def resolve_public_addresses(host: str) -> list[str]:
    results = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({result[4][0] for result in results})


def _is_unsafe(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


def _validated_endpoint_addresses(
    endpoint: str,
    resolver: Callable[[str], list[str]] = resolve_public_addresses,
    allowed_reserved_hosts: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme.lower() != "https":
        raise UnsafeEndpoint("Only HTTPS endpoints are allowed")
    if not parsed.hostname:
        raise UnsafeEndpoint("Endpoint hostname is required")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeEndpoint("Embedded endpoint credentials are not allowed")
    if parsed.fragment:
        raise UnsafeEndpoint("Endpoint fragments are not allowed")
    try:
        addresses = resolver(parsed.hostname)
    except (OSError, socket.gaierror) as error:
        raise UnsafeEndpoint("Endpoint hostname could not be resolved") from error
    if not addresses:
        raise UnsafeEndpoint("Endpoint hostname resolved to no addresses")
    try:
        unsafe = any(_is_unsafe(address) for address in addresses)
    except ValueError as error:
        raise UnsafeEndpoint("Endpoint resolved to an invalid address") from error
    if unsafe and parsed.hostname.casefold() not in allowed_reserved_hosts:
        raise UnsafeEndpoint("Endpoint resolves to a non-public address")
    return tuple(addresses)


def validate_endpoint(
    endpoint: str,
    resolver: Callable[[str], list[str]] = resolve_public_addresses,
    allowed_reserved_hosts: frozenset[str] = frozenset(),
) -> str:
    _validated_endpoint_addresses(endpoint, resolver, allowed_reserved_hosts)
    return endpoint


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to one numeric address while authenticating the original host."""

    def __init__(
        self,
        host: str,
        port: int,
        address: str,
        timeout: float,
    ) -> None:
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._pinned_address = address

    def connect(self) -> None:
        address = ipaddress.ip_address(self._pinned_address)
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        peer: tuple[Any, ...] = (
            (self._pinned_address, self.port, 0, 0)
            if family == socket.AF_INET6
            else (self._pinned_address, self.port)
        )
        plain = socket.socket(family, socket.SOCK_STREAM)
        try:
            plain.settimeout(self.timeout)
            plain.connect(peer)
            self.sock = self._context.wrap_socket(
                plain,
                server_hostname=self.host,
            )
        except Exception:
            plain.close()
            raise


class _PinnedResponse:
    def __init__(self, response: Any, connection: Any) -> None:
        self.response = response
        self.connection = connection

    def read(self, amount: int = -1) -> bytes:
        return self.response.read(amount)

    def close(self) -> None:
        try:
            self.response.close()
        finally:
            self.connection.close()

    def __enter__(self) -> "_PinnedResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class PinnedHTTPSOpener:
    """Open a direct HTTPS request without resolving its hostname again."""

    def __init__(self, connection_factory: Callable[..., Any] | None = None) -> None:
        self.connection_factory = connection_factory or _PinnedHTTPSConnection

    def open_pinned(
        self,
        request: urllib.request.Request,
        timeout: float,
        addresses: tuple[str, ...],
    ) -> _PinnedResponse:
        parsed = urllib.parse.urlsplit(request.full_url)
        if (
            parsed.scheme.lower() != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not addresses
        ):
            raise UnsafeEndpoint("Pinned HTTPS request is invalid")
        try:
            if any(_is_unsafe(address) for address in addresses):
                raise UnsafeEndpoint("Pinned HTTPS address is not public")
            port = parsed.port or 443
        except ValueError as error:
            raise UnsafeEndpoint("Pinned HTTPS request is invalid") from error

        connection = None
        last_error: OSError | None = None
        for address in addresses:
            candidate = self.connection_factory(
                parsed.hostname, port, address, timeout
            )
            try:
                candidate.connect()
            except OSError as error:
                candidate.close()
                last_error = error
                continue
            connection = candidate
            break
        if connection is None:
            if last_error is not None:
                raise last_error
            raise OSError("Provider connection failed")

        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        try:
            request_headers = {
                key: value
                for key, value in request.header_items()
                if key.casefold() != "host"
            }
            connection.request(
                request.get_method(),
                target,
                body=request.data,
                headers=request_headers,
            )
            response = connection.getresponse()
            if not 200 <= response.status <= 299:
                headers = response.headers
                status = response.status
                reason = response.reason
                response.close()
                connection.close()
                raise urllib.error.HTTPError(
                    request.full_url, status, reason, headers, None
                )
            return _PinnedResponse(response, connection)
        except Exception:
            connection.close()
            raise


class BoundedHTTPClient:
    def __init__(
        self,
        resolver: Callable[[str], list[str]] = resolve_public_addresses,
        opener: Any | None = None,
        timeout: float = 15.0,
        max_bytes: int = 1024 * 1024,
        allowed_reserved_hosts: set[str] | frozenset[str] = frozenset(),
        allowed_redirect_hosts: set[str] | frozenset[str] | None = None,
        pin_resolved_address: bool = False,
    ) -> None:
        if not isinstance(pin_resolved_address, bool):
            raise ValueError("pin_resolved_address must be a boolean")
        self.resolver = resolver
        self.opener = opener or (
            PinnedHTTPSOpener()
            if pin_resolved_address
            else urllib.request.build_opener(_NoRedirectHandler())
        )
        if pin_resolved_address and not callable(
            getattr(self.opener, "open_pinned", None)
        ):
            raise ValueError("Pinned HTTPS opener is required")
        self.pin_resolved_address = pin_resolved_address
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.allowed_reserved_hosts = frozenset(host.casefold() for host in allowed_reserved_hosts)
        self.allowed_redirect_hosts = (
            None
            if allowed_redirect_hosts is None
            else frozenset(host.casefold() for host in allowed_redirect_hosts)
        )

    def _open(self, endpoint: str, request: urllib.request.Request):
        addresses = _validated_endpoint_addresses(
            endpoint, self.resolver, self.allowed_reserved_hosts
        )
        if self.pin_resolved_address:
            return self.opener.open_pinned(request, self.timeout, addresses)
        return self.opener.open(request, timeout=self.timeout)

    def get_json(self, endpoint: str, headers: dict[str, str]) -> dict[str, Any]:
        return self._request_json("GET", endpoint, headers, None)

    def post_json(
        self,
        endpoint: str,
        headers: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request_json("POST", endpoint, headers, body)

    def post_stream(
        self,
        endpoint: str,
        headers: dict[str, str],
        body: dict[str, Any],
    ):
        try:
            encoded = json.dumps(
                body, ensure_ascii=False, allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as error:
            raise MalformedResponse("Provider request JSON is invalid") from error
        request = urllib.request.Request(
            endpoint,
            data=encoded,
            headers={
                "Accept": "text/event-stream",
                "Content-Type": "application/json",
                **headers,
            },
            method="POST",
        )
        try:
            response = self._open(endpoint, request)
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise AuthenticationRequired(
                    "Provider rejected the credential"
                ) from error
            if error.code == 429:
                raise RateLimited("Provider rate limit reached") from error
            if error.code in {301, 302, 303, 307, 308}:
                raise NetworkError("Provider redirect is not allowed") from error
            raise HTTPStatusError(error.code) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise NetworkError("Provider request failed") from error

        def chunks():
            total = 0
            try:
                with response:
                    while True:
                        chunk = response.read(min(64 * 1024, self.max_bytes + 1 - total))
                        if not chunk:
                            return
                        total += len(chunk)
                        if total > self.max_bytes:
                            raise ResponseTooLarge(
                                "Provider response exceeded the size limit"
                            )
                        yield chunk
            except (ResponseTooLarge, NetworkError):
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                raise NetworkError("Provider request failed") from error

        return chunks()

    def _request_json(
        self,
        method: str,
        endpoint: str,
        headers: dict[str, str],
        body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        current = endpoint
        encoded_body = None if body is None else json.dumps(body).encode("utf-8")
        for redirect_count in range(4):
            request_headers = {"Accept": "application/json", **headers}
            if encoded_body is not None:
                request_headers.setdefault("Content-Type", "application/json")
            request = urllib.request.Request(
                current,
                data=encoded_body,
                headers=request_headers,
                method=method,
            )
            try:
                with self._open(current, request) as response:
                    body = response.read(self.max_bytes + 1)
            except urllib.error.HTTPError as error:
                if error.code in {301, 302, 303, 307, 308}:
                    if redirect_count == 3:
                        raise NetworkError("Too many redirects") from error
                    location = error.headers.get("Location")
                    if not location:
                        raise NetworkError("Redirect did not include a location") from error
                    target = urllib.parse.urljoin(current, location)
                    target_host = urllib.parse.urlsplit(target).hostname
                    if self.allowed_redirect_hosts is not None and (
                        target_host is None
                        or target_host.casefold() not in self.allowed_redirect_hosts
                    ):
                        raise NetworkError("Redirect target host is not allowed") from error
                    current = target
                    continue
                if error.code in {401, 403}:
                    raise AuthenticationRequired("Provider rejected the credential") from error
                if error.code == 429:
                    raise RateLimited("Provider rate limit reached") from error
                raise HTTPStatusError(error.code) from error
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                raise NetworkError("Provider request failed") from error

            if len(body) > self.max_bytes:
                raise ResponseTooLarge("Provider response exceeded the size limit")
            try:
                payload = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise MalformedResponse("Provider returned invalid JSON") from error
            if not isinstance(payload, dict):
                raise MalformedResponse("Provider JSON root must be an object")
            return payload
        raise NetworkError("Too many redirects")
