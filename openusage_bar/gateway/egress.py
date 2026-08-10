"""Credential-isolated egress boundary for Gateway provider calls."""

from __future__ import annotations

from collections.abc import Iterable
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
            value = boundary.get(credential_account)
        except Exception:
            raise _provider_error("credential_unavailable") from None
        if not _valid_stored_credential(value):
            raise _provider_error("credential_unavailable")
        credential = value

    try:
        return provider.call(request_body, credential=credential)
    except GatewayProviderError:
        raise
    except Exception:
        raise _provider_error("upstream_unavailable", True) from None


__all__ = [
    "execute_provider_call",
    "supports_provider_account_credentials",
    "validate_provider_endpoint",
]
