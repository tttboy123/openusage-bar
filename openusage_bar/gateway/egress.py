"""Credential-isolated egress boundary for Gateway provider calls."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
import os
import secrets
from threading import Lock
from typing import Any

from ..keychain import default_keychain
from .accounts import ProviderAccountRef
from .providers import (
    GatewayProvider,
    GatewayProviderError,
    ProviderEventStream,
    ProviderResult,
    default_gateway_providers,
    validate_provider_endpoint,
)


_PROVIDER_CREDENTIAL_ACCOUNTS: dict[str, str | None] = {
    "anthropic": "anthropic.gateway-api-key",
    "deepseek": "deepseek.gateway-api-key",
    "ollama": None,
    "openai": "openai.gateway-api-key",
    "openrouter": "openrouter.gateway-api-key",
}
_MAX_ATTEMPT_COUNT = (1 << 64) - 1


@dataclass(frozen=True, repr=False)
class GatewayEgressAttemptCounters:
    """Closed, dimension-free counters for Gateway private-boundary attempts."""

    process_epoch_sha256: str
    provider_network_attempts: int
    provider_credential_read_attempts: int

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or not 0 <= value <= _MAX_ATTEMPT_COUNT
            for value in (
                self.provider_network_attempts,
                self.provider_credential_read_attempts,
            )
        ):
            raise ValueError("invalid Gateway egress counters")
        if (
            type(self.process_epoch_sha256) is not str
            or len(self.process_epoch_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.process_epoch_sha256
            )
        ):
            raise ValueError("invalid Gateway egress counters")

    def __repr__(self) -> str:
        return "<GatewayEgressAttemptCounters closed>"


_ATTEMPT_COUNTER_LOCK = Lock()
_PROCESS_EPOCH_SEED = secrets.token_bytes(32)
_provider_network_attempts = 0
_provider_credential_read_attempts = 0


def gateway_egress_attempt_counters() -> GatewayEgressAttemptCounters:
    """Return one closed snapshot without exposing provider or account identity."""

    with _ATTEMPT_COUNTER_LOCK:
        return GatewayEgressAttemptCounters(
            hashlib.sha256(
                _PROCESS_EPOCH_SEED + str(os.getpid()).encode("ascii")
            ).hexdigest(),
            _provider_network_attempts,
            _provider_credential_read_attempts,
        )


def _record_gateway_egress_attempt(*, credential_read: bool) -> None:
    global _provider_network_attempts, _provider_credential_read_attempts
    with _ATTEMPT_COUNTER_LOCK:
        if credential_read:
            if _provider_credential_read_attempts >= _MAX_ATTEMPT_COUNT:
                raise RuntimeError("Gateway egress counter unavailable")
            _provider_credential_read_attempts += 1
        else:
            if _provider_network_attempts >= _MAX_ATTEMPT_COUNT:
                raise RuntimeError("Gateway egress counter unavailable")
            _provider_network_attempts += 1


def _provider_error(code: str, retryable: bool = False) -> GatewayProviderError:
    return GatewayProviderError(code, retryable)


def _valid_stored_credential(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value.isascii()
        and len(value.encode("ascii")) <= 64 * 1024
        and all(0x21 <= ord(character) <= 0x7E for character in value)
    )


def supports_provider_account_credentials(provider_id: object) -> bool:
    return (
        type(provider_id) is str
        and provider_id in _PROVIDER_CREDENTIAL_ACCOUNTS
        and _PROVIDER_CREDENTIAL_ACCOUNTS[provider_id] is not None
    )


def execute_provider_call(
    provider_id: str,
    request_body: bytes,
    *,
    providers: Iterable[GatewayProvider] | None = None,
    keychain: Any | None = None,
    account: ProviderAccountRef | None = None,
) -> ProviderResult | ProviderEventStream:
    """Read one credential inside Python, then execute one bounded adapter call."""

    if type(provider_id) is not str or provider_id not in _PROVIDER_CREDENTIAL_ACCOUNTS:
        raise _provider_error("unsupported_provider")
    if type(request_body) is not bytes:
        raise _provider_error("invalid_request")
    if account is not None and (
        type(account) is not ProviderAccountRef
        or account.provider_id != provider_id
        or _PROVIDER_CREDENTIAL_ACCOUNTS[provider_id] is None
    ):
        raise _provider_error("unsupported_provider")
    try:
        available = tuple(
            providers if providers is not None else default_gateway_providers()
        )
    except Exception:
        raise _provider_error("unsupported_provider") from None

    provider_map: dict[str, GatewayProvider] = {}
    for provider in available:
        try:
            candidate_id = getattr(provider, "provider_id", None)
            candidate_account = getattr(provider, "credential_account", None)
        except Exception:
            raise _provider_error("unsupported_provider") from None
        if (
            type(candidate_id) is not str
            or candidate_id not in _PROVIDER_CREDENTIAL_ACCOUNTS
            or (
                candidate_account is not None
                and type(candidate_account) is not str
            )
            or candidate_account != _PROVIDER_CREDENTIAL_ACCOUNTS[candidate_id]
            or candidate_id in provider_map
            or len(provider_map) >= 5
        ):
            raise _provider_error("unsupported_provider")
        provider_map[candidate_id] = provider
    provider = provider_map.get(provider_id)
    if provider is None:
        raise _provider_error("unsupported_provider")

    credential = ""
    credential_account = (
        account.credential_account
        if account is not None
        else _PROVIDER_CREDENTIAL_ACCOUNTS[provider_id]
    )
    if credential_account is not None:
        if type(credential_account) is not str or not credential_account:
            raise _provider_error("credential_unavailable")
        try:
            boundary = keychain if keychain is not None else default_keychain()
            _record_gateway_egress_attempt(credential_read=True)
            value = boundary.get(credential_account)
        except Exception:
            raise _provider_error("credential_unavailable") from None
        if not _valid_stored_credential(value):
            raise _provider_error("credential_unavailable")
        credential = value

    try:
        _record_gateway_egress_attempt(credential_read=False)
        return provider.call(request_body, credential=credential)
    except GatewayProviderError:
        raise
    except Exception:
        raise _provider_error("upstream_unavailable", True) from None


__all__ = [
    "GatewayEgressAttemptCounters",
    "execute_provider_call",
    "gateway_egress_attempt_counters",
    "supports_provider_account_credentials",
    "validate_provider_endpoint",
]
