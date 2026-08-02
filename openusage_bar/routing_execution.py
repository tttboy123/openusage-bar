"""Private execution connections and installed route adapter registry."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib.parse import urlsplit

from .routing_contract import EXECUTION_CLASSES, MAX_COUNTER, RouteTarget


SCHEMA_VERSION = 1
MAX_CONNECTIONS = 128
MAX_MODELS = 256
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_URL_BYTES = 2_048
MAX_CHAT_BODY_BYTES = 2 * 1024 * 1024
MAX_SECRET_BYTES = 64 * 1024

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TOP_LEVEL_KEYS = frozenset({"schemaVersion", "revision", "connections"})
_CONNECTION_KEYS = frozenset({
    "connectionRef",
    "providerId",
    "accountRef",
    "executionClass",
    "executionAdapterId",
    "baseURL",
    "enabled",
    "models",
})


class ExecutionConnectionConfigError(ValueError):
    """The non-secret execution connection document is invalid."""


class ExecutionResolutionError(LookupError):
    """A target has no exact enabled execution connection and adapter."""


class ExecutionAuthenticationError(RuntimeError):
    """A configured execution connection has no readable credential."""


class ExecutionTransientError(RuntimeError):
    """A sanitized Provider outcome that may move to the next ranked target."""

    def __init__(self, status_class: str, reason_code: str):
        if status_class not in {
            "transport", "timeout", "http_408", "http_429", "http_5xx",
        }:
            raise ValueError("invalid transient execution status")
        if reason_code not in {
            "transport_error", "provider_timeout", "provider_rate_limited",
            "provider_unavailable",
        }:
            raise ValueError("invalid transient execution reason")
        super().__init__("transient execution failure")
        self.status_class = status_class
        self.reason_code = reason_code


class ExecutionAdapter(Protocol):
    adapter_id: str
    execution_class: str

    def execute_chat(
        self,
        connection: "ExecutionConnection",
        model_id: str,
        body: dict[str, object],
    ) -> dict[str, object]: ...


class ExecutionKeychain(Protocol):
    def get(self, account: str) -> str | None: ...


class ExecutionHTTPClient(Protocol):
    def post_json(
        self,
        endpoint: str,
        headers: dict[str, str],
        body: dict[str, object],
    ) -> dict[str, object]: ...

    def post_stream(
        self,
        endpoint: str,
        headers: dict[str, str],
        body: dict[str, object],
    ) -> Iterable[bytes]: ...


def _invalid() -> ExecutionConnectionConfigError:
    return ExecutionConnectionConfigError("invalid execution connection configuration")


def _stable_id(value: object) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise _invalid()
    return value


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _exact_mapping(value: object, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        raise _invalid()
    return value


def _integer(value: object, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= MAX_COUNTER
    ):
        raise _invalid()
    return value


def _base_url(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > MAX_URL_BYTES
        or any(ord(character) < 0x20 for character in value)
    ):
        raise _invalid()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise _invalid() from error
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path and not parsed.path.startswith("/")
        or port is not None and not 1 <= port <= 65_535
    ):
        raise _invalid()
    return value.rstrip("/")


def credential_account(connection_ref: str) -> str:
    """Return the private Keychain account for one opaque execution connection."""
    return "routing." + _stable_id(connection_ref)


def _models(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= MAX_MODELS:
        raise _invalid()
    models = tuple(_stable_id(item) for item in value)
    if len(models) != len(set(models)):
        raise _invalid()
    return tuple(sorted(models))


@dataclass(frozen=True)
class ExecutionConnection:
    connection_ref: str
    provider_id: str
    account_ref: str
    execution_class: str
    execution_adapter_id: str
    base_url: str
    enabled: bool
    models: tuple[str, ...]

    def __post_init__(self) -> None:
        for value in (
            self.connection_ref,
            self.provider_id,
            self.account_ref,
            self.execution_adapter_id,
        ):
            _stable_id(value)
        if self.execution_class not in EXECUTION_CLASSES:
            raise _invalid()
        if not isinstance(self.enabled, bool):
            raise _invalid()
        object.__setattr__(self, "base_url", _base_url(self.base_url))
        object.__setattr__(self, "models", _models(self.models))


@dataclass(frozen=True)
class ExecutionConnectionConfiguration:
    schema_version: int
    revision: int
    connections: tuple[ExecutionConnection, ...]


def _decode_connection(value: object) -> ExecutionConnection:
    raw = _exact_mapping(value, _CONNECTION_KEYS)
    try:
        return ExecutionConnection(
            connection_ref=raw["connectionRef"],
            provider_id=raw["providerId"],
            account_ref=raw["accountRef"],
            execution_class=raw["executionClass"],
            execution_adapter_id=raw["executionAdapterId"],
            base_url=raw["baseURL"],
            enabled=raw["enabled"],
            models=_models(raw["models"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, ExecutionConnectionConfigError):
            raise
        raise _invalid() from error


def _connection_wire(value: ExecutionConnection) -> dict[str, object]:
    return {
        "connectionRef": value.connection_ref,
        "providerId": value.provider_id,
        "accountRef": value.account_ref,
        "executionClass": value.execution_class,
        "executionAdapterId": value.execution_adapter_id,
        "baseURL": value.base_url,
        "enabled": value.enabled,
        "models": list(value.models),
    }


class ExecutionConnectionStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _safe_stat(self) -> os.stat_result:
        try:
            value = self.path.lstat()
        except OSError as error:
            raise _invalid() from error
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != os.getuid()
            or stat.S_IMODE(value.st_mode) != 0o600
            or value.st_size > MAX_DOCUMENT_BYTES
        ):
            raise _invalid()
        return value

    def _read(self) -> bytes:
        before = self._safe_stat()
        descriptor = -1
        try:
            descriptor = os.open(
                self.path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            after = os.fstat(descriptor)
            if (
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or not stat.S_ISREG(after.st_mode)
                or after.st_uid != os.getuid()
                or stat.S_IMODE(after.st_mode) != 0o600
                or after.st_size > MAX_DOCUMENT_BYTES
            ):
                raise _invalid()
            chunks: list[bytes] = []
            remaining = after.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    raise _invalid()
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise _invalid()
            return b"".join(chunks)
        except OSError as error:
            raise _invalid() from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def load(self) -> ExecutionConnectionConfiguration:
        if not os.path.lexists(self.path):
            return ExecutionConnectionConfiguration(SCHEMA_VERSION, 0, ())
        try:
            payload = json.loads(
                self._read().decode("utf-8"),
                object_pairs_hook=_strict_object,
            )
            raw = _exact_mapping(payload, _TOP_LEVEL_KEYS)
            if _integer(raw["schemaVersion"], minimum=1) != SCHEMA_VERSION:
                raise _invalid()
            revision = _integer(raw["revision"])
            values = raw["connections"]
            if not isinstance(values, list) or len(values) > MAX_CONNECTIONS:
                raise _invalid()
            connections = tuple(_decode_connection(value) for value in values)
            refs = [value.connection_ref for value in connections]
            if len(refs) != len(set(refs)):
                raise _invalid()
            return ExecutionConnectionConfiguration(
                SCHEMA_VERSION,
                revision,
                tuple(sorted(connections, key=lambda value: value.connection_ref)),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            if isinstance(error, ExecutionConnectionConfigError):
                raise
            raise _invalid() from error

    def _prepare_parent(self) -> Path:
        parent = self.path.parent
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            value = parent.lstat()
        except OSError as error:
            raise _invalid() from error
        if (
            not stat.S_ISDIR(value.st_mode)
            or value.st_uid != os.getuid()
            or stat.S_IMODE(value.st_mode) & 0o077
        ):
            raise _invalid()
        return parent

    def save(
        self,
        connections: tuple[ExecutionConnection, ...],
        *,
        revision: int,
    ) -> None:
        try:
            revision = _integer(revision)
            values = tuple(connections)
            if (
                len(values) > MAX_CONNECTIONS
                or any(not isinstance(value, ExecutionConnection) for value in values)
            ):
                raise _invalid()
            refs = [value.connection_ref for value in values]
            if len(refs) != len(set(refs)):
                raise _invalid()
            if os.path.lexists(self.path) and revision <= self.load().revision:
                raise _invalid()
            payload = {
                "schemaVersion": SCHEMA_VERSION,
                "revision": revision,
                "connections": [
                    _connection_wire(value)
                    for value in sorted(values, key=lambda item: item.connection_ref)
                ],
            }
            encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
            if len(encoded) > MAX_DOCUMENT_BYTES:
                raise _invalid()
            parent = self._prepare_parent()
            descriptor, temporary = tempfile.mkstemp(
                prefix="execution-connections.", suffix=".json", dir=parent
            )
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                os.chmod(self.path, 0o600)
                directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.lexists(temporary):
                    os.unlink(temporary)
        except (OSError, TypeError, ValueError) as error:
            if isinstance(error, ExecutionConnectionConfigError):
                raise
            raise _invalid() from error


class OpenAICompatibleExecutionAdapter:
    """Execute bounded, non-streaming Chat Completions without persisting content."""

    adapter_id = "openai_compatible.direct"
    execution_class = "openai_compatible"

    def __init__(
        self,
        *,
        keychain: ExecutionKeychain | None = None,
        client: ExecutionHTTPClient | None = None,
    ) -> None:
        if keychain is None:
            from .keychain import BoundedMacOSKeychain

            keychain = BoundedMacOSKeychain(timeout_seconds=5)
        if client is None:
            from .network import BoundedHTTPClient

            client = BoundedHTTPClient(
                timeout=120.0,
                max_bytes=4 * 1024 * 1024,
                allowed_redirect_hosts=frozenset(),
            )
        self.keychain = keychain
        self.client = client

    def _prepared_request(
        self,
        connection: ExecutionConnection,
        model_id: str,
        body: dict[str, object],
        *,
        stream: bool,
    ) -> tuple[dict[str, object], str]:
        if (
            not isinstance(connection, ExecutionConnection)
            or connection.execution_adapter_id != self.adapter_id
            or connection.execution_class != self.execution_class
            or not connection.enabled
            or _stable_id(model_id) not in connection.models
            or not isinstance(body, dict)
            or body.get("stream", False) is not stream
        ):
            raise ValueError("invalid chat execution request")
        request_body = dict(body)
        request_body["model"] = model_id
        try:
            encoded = json.dumps(
                request_body,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as error:
            raise ValueError("invalid chat execution request") from error
        if len(encoded) > MAX_CHAT_BODY_BYTES:
            raise ValueError("invalid chat execution request")
        secret = self.keychain.get(credential_account(connection.connection_ref))
        if (
            not isinstance(secret, str)
            or not secret
            or len(secret.encode("utf-8")) > MAX_SECRET_BYTES
            or "\r" in secret
            or "\n" in secret
        ):
            raise ExecutionAuthenticationError("execution credential unavailable")
        return request_body, secret

    @staticmethod
    def _raise_network(error: BaseException) -> None:
        from .network import (
            AuthenticationRequired,
            HTTPStatusError,
            NetworkError,
            RateLimited,
        )

        if isinstance(error, AuthenticationRequired):
            raise ExecutionAuthenticationError(
                "execution credential rejected"
            ) from error
        if isinstance(error, RateLimited):
            raise ExecutionTransientError(
                "http_429", "provider_rate_limited"
            ) from error
        if isinstance(error, HTTPStatusError):
            if error.status == 408:
                raise ExecutionTransientError(
                    "http_408", "provider_timeout"
                ) from error
            if error.status in {500, 502, 503, 504}:
                raise ExecutionTransientError(
                    "http_5xx", "provider_unavailable"
                ) from error
            raise ValueError("provider rejected execution request") from error
        if isinstance(error, NetworkError):
            raise ExecutionTransientError(
                "transport", "transport_error"
            ) from error
        raise error

    def execute_chat(
        self,
        connection: ExecutionConnection,
        model_id: str,
        body: dict[str, object],
    ) -> dict[str, object]:
        request_body, secret = self._prepared_request(
            connection, model_id, body, stream=False
        )
        from .network import AuthenticationRequired, NetworkError, RateLimited

        try:
            response = self.client.post_json(
                connection.base_url + "/chat/completions",
                {"Authorization": "Bearer " + secret},
                request_body,
            )
        except (AuthenticationRequired, RateLimited, NetworkError) as error:
            self._raise_network(error)
        if not isinstance(response, dict):
            raise ValueError("invalid chat execution response")
        return response

    def execute_chat_stream(
        self,
        connection: ExecutionConnection,
        model_id: str,
        body: dict[str, object],
    ) -> Iterable[bytes]:
        request_body, secret = self._prepared_request(
            connection, model_id, body, stream=True
        )
        from .network import AuthenticationRequired, NetworkError, RateLimited

        try:
            response = self.client.post_stream(
                connection.base_url + "/chat/completions",
                {"Authorization": "Bearer " + secret},
                request_body,
            )
        except (AuthenticationRequired, RateLimited, NetworkError) as error:
            self._raise_network(error)
        if not hasattr(response, "__iter__"):
            raise ValueError("invalid chat execution stream")
        return response


def installed_execution_adapters() -> tuple[ExecutionAdapter, ...]:
    """Return only execution-capable adapters shipped in this distribution."""
    return (OpenAICompatibleExecutionAdapter(),)


class ExecutionRegistry:
    def __init__(
        self,
        *,
        adapters: Iterable[ExecutionAdapter],
        connections: Iterable[ExecutionConnection],
    ) -> None:
        adapter_values = tuple(adapters)
        connection_values = tuple(connections)
        if any(
            _ID.fullmatch(getattr(value, "adapter_id", "")) is None
            or getattr(value, "execution_class", None) not in EXECUTION_CLASSES
            or not callable(getattr(value, "execute_chat", None))
            for value in adapter_values
        ):
            raise ValueError("invalid execution adapter")
        adapter_ids = [value.adapter_id for value in adapter_values]
        if len(adapter_ids) != len(set(adapter_ids)):
            raise ValueError("duplicate execution adapter")
        if any(not isinstance(value, ExecutionConnection) for value in connection_values):
            raise ValueError("invalid execution connection")
        connection_refs = [value.connection_ref for value in connection_values]
        if len(connection_refs) != len(set(connection_refs)):
            raise ValueError("duplicate execution connection")
        self._adapters = {
            value.adapter_id: value
            for value in sorted(adapter_values, key=lambda item: item.adapter_id)
        }
        self._connections = {
            value.connection_ref: value
            for value in sorted(connection_values, key=lambda item: item.connection_ref)
        }

    def available_adapter_ids(self) -> tuple[str, ...]:
        return tuple(self._adapters)

    def available_connection_refs(self) -> tuple[str, ...]:
        return tuple(
            value.connection_ref
            for value in self._connections.values()
            if value.enabled
            and value.execution_adapter_id in self._adapters
            and self._adapters[value.execution_adapter_id].execution_class
            == value.execution_class
        )

    def resolve(
        self,
        target: RouteTarget,
    ) -> tuple[ExecutionAdapter, ExecutionConnection]:
        if not isinstance(target, RouteTarget) or not target.enabled:
            raise ExecutionResolutionError("execution target is unavailable")
        connection = self._connections.get(target.connection_ref)
        adapter = self._adapters.get(target.execution_adapter_id)
        if (
            connection is None
            or adapter is None
            or not connection.enabled
            or not target.adapter_available
            or connection.provider_id != target.provider_id
            or connection.account_ref != target.account_ref
            or connection.execution_class != target.execution_class
            or connection.execution_adapter_id != target.execution_adapter_id
            or adapter.execution_class != target.execution_class
            or target.model_id not in connection.models
        ):
            raise ExecutionResolutionError("execution target is unavailable")
        return adapter, connection
