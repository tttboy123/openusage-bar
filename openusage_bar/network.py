from __future__ import annotations

import ipaddress
import json
import socket
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


def validate_endpoint(
    endpoint: str,
    resolver: Callable[[str], list[str]] = resolve_public_addresses,
    allowed_reserved_hosts: frozenset[str] = frozenset(),
) -> str:
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
    return endpoint


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BoundedHTTPClient:
    def __init__(
        self,
        resolver: Callable[[str], list[str]] = resolve_public_addresses,
        opener: Any | None = None,
        timeout: float = 15.0,
        max_bytes: int = 1024 * 1024,
        allowed_reserved_hosts: set[str] | frozenset[str] = frozenset(),
        allowed_redirect_hosts: set[str] | frozenset[str] | None = None,
    ) -> None:
        self.resolver = resolver
        self.opener = opener or urllib.request.build_opener(_NoRedirectHandler())
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.allowed_reserved_hosts = frozenset(host.casefold() for host in allowed_reserved_hosts)
        self.allowed_redirect_hosts = (
            None
            if allowed_redirect_hosts is None
            else frozenset(host.casefold() for host in allowed_redirect_hosts)
        )

    def get_json(self, endpoint: str, headers: dict[str, str]) -> dict[str, Any]:
        return self._request_json("GET", endpoint, headers, None)

    def post_json(
        self,
        endpoint: str,
        headers: dict[str, str],
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request_json("POST", endpoint, headers, body)

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
            validate_endpoint(current, self.resolver, self.allowed_reserved_hosts)
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
                with self.opener.open(request, timeout=self.timeout) as response:
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
                raise NetworkError(f"Provider returned HTTP {error.code}") from error
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
