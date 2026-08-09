"""Secret-free configuration for the opt-in local Gateway."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .accounts import ProviderAccountRef
from .contracts import GatewayMode
from .pools import AccountPool, PoolMember, PoolStrategy


_MAX_CONFIG_BYTES = 64 * 1024
_CONFIG_FIELDS = {
    "enabled",
    "mode",
    "host",
    "port",
    "proxy_enabled",
    "cache_enabled",
    "accounts",
    "account_pools",
}
_SECRET_FIELDS = {"apikey", "secret", "token", "password", "cookie"}


@dataclass(frozen=True)
class GatewayConfig:
    enabled: bool = False
    mode: GatewayMode = GatewayMode.OBSERVE
    host: str = "127.0.0.1"
    port: int = 17823
    proxy_enabled: bool = False
    cache_enabled: bool = False
    accounts: tuple[ProviderAccountRef, ...] = ()
    account_pools: tuple[AccountPool, ...] = ()

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("Gateway enabled must be a boolean.")
        if not isinstance(self.mode, GatewayMode):
            raise ValueError("Gateway mode is invalid.")
        if self.host != "127.0.0.1":
            raise ValueError("Gateway host must be 127.0.0.1.")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Gateway port is invalid.")
        if type(self.proxy_enabled) is not bool:
            raise ValueError("Gateway proxy setting must be a boolean.")
        if type(self.cache_enabled) is not bool:
            raise ValueError("Gateway cache setting must be a boolean.")
        if type(self.accounts) is not tuple or any(
            type(account) is not ProviderAccountRef for account in self.accounts
        ):
            raise TypeError("Gateway accounts are invalid.")
        if type(self.account_pools) is not tuple or any(
            type(pool) is not AccountPool for pool in self.account_pools
        ):
            raise TypeError("Gateway account pools are invalid.")
        account_ids = tuple(account.account_id for account in self.accounts)
        if len(set(account_ids)) != len(account_ids):
            raise ValueError("Gateway account IDs must be unique.")
        pool_ids = tuple(pool.pool_id for pool in self.account_pools)
        if len(set(pool_ids)) != len(pool_ids):
            raise ValueError("Gateway pool IDs must be unique.")
        known_accounts = set(account_ids)
        if any(
            member.account_id not in known_accounts
            for pool in self.account_pools
            for member in pool.members
        ):
            raise ValueError("Gateway pool references an unknown account.")
        if self.proxy_enabled and (
            not self.enabled or self.mode is not GatewayMode.GATEWAY
        ):
            raise ValueError(
                "Gateway proxy requires gateway mode with enabled configuration."
            )


def _normalized_field_name(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _reject_secret_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if _normalized_field_name(key) in _SECRET_FIELDS:
                raise ValueError(
                    "Secret field is not permitted in Gateway configuration."
                )
            _reject_secret_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_secret_fields(nested)


def _read_config(path: Path) -> bytes | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError("Gateway configuration could not be read.") from None

    if len(raw) > _MAX_CONFIG_BYTES:
        raise ValueError("Gateway configuration is too large.")
    return raw


def _parse_object(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("Gateway configuration is invalid.") from None
    if not isinstance(payload, dict):
        raise ValueError("Gateway configuration must be a JSON object.")
    return payload


def _parse_accounts(value: object) -> tuple[ProviderAccountRef, ...]:
    if type(value) is not list or len(value) > 128:
        raise ValueError("Gateway accounts are invalid.")
    result: list[ProviderAccountRef] = []
    for item in value:
        if type(item) is not dict or set(item) != {
            "provider_id",
            "account_id",
            "alias",
        }:
            raise ValueError("Gateway account is invalid.")
        try:
            provider_id = item["provider_id"]
            account_id = item["account_id"]
            if type(provider_id) is not str or type(account_id) is not str:
                raise ValueError
            result.append(
                ProviderAccountRef(
                    provider_id=provider_id,
                    account_id=account_id,
                    alias=item["alias"],
                    credential_account=(
                        f"{provider_id}.{account_id}.gateway-api-key"
                    ),
                )
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("Gateway account is invalid.") from None
    return tuple(result)


def _parse_pool_members(value: object) -> tuple[PoolMember, ...]:
    if type(value) is not list:
        raise ValueError("Gateway pool members are invalid.")
    members: list[PoolMember] = []
    for item in value:
        if type(item) is not dict or set(item) != {
            "account_id",
            "priority",
            "weight",
        }:
            raise ValueError("Gateway pool member is invalid.")
        try:
            members.append(
                PoolMember(
                    account_id=item["account_id"],
                    priority=item["priority"],
                    weight=item["weight"],
                )
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("Gateway pool member is invalid.") from None
    return tuple(members)


def _parse_account_pools(value: object) -> tuple[AccountPool, ...]:
    if type(value) is not list or len(value) > 64:
        raise ValueError("Gateway account pools are invalid.")
    required = {"pool_id", "revision", "strategy", "members"}
    optional = {
        "cross_provider_fallback",
        "cross_model_fallback",
        "cross_region_fallback",
    }
    result: list[AccountPool] = []
    for item in value:
        if (
            type(item) is not dict
            or not required.issubset(item)
            or any(key not in required | optional for key in item)
        ):
            raise ValueError("Gateway account pool is invalid.")
        try:
            strategy = PoolStrategy(item["strategy"])
            result.append(
                AccountPool(
                    pool_id=item["pool_id"],
                    revision=item["revision"],
                    strategy=strategy,
                    members=_parse_pool_members(item["members"]),
                    cross_provider_fallback=item.get(
                        "cross_provider_fallback", False
                    ),
                    cross_model_fallback=item.get("cross_model_fallback", False),
                    cross_region_fallback=item.get("cross_region_fallback", False),
                )
            )
        except (KeyError, TypeError, ValueError):
            raise ValueError("Gateway account pool is invalid.") from None
    return tuple(result)


def load_gateway_config(path: Path) -> GatewayConfig:
    """Load one bounded, secret-free Gateway JSON configuration."""

    raw = _read_config(path)
    if raw is None:
        return GatewayConfig()

    payload = _parse_object(raw)
    try:
        _reject_secret_fields(payload)
    except RecursionError:
        raise ValueError("Gateway configuration is invalid.") from None

    if any(key not in _CONFIG_FIELDS for key in payload):
        raise ValueError("Gateway configuration contains an unknown field.")

    mode_value = payload.get("mode", GatewayMode.OBSERVE.value)
    if not isinstance(mode_value, str):
        raise ValueError("Gateway mode is invalid.")
    try:
        mode = GatewayMode(mode_value)
    except ValueError:
        raise ValueError("Gateway mode is invalid.") from None

    return GatewayConfig(
        enabled=payload.get("enabled", False),
        mode=mode,
        host=payload.get("host", "127.0.0.1"),
        port=payload.get("port", 17823),
        proxy_enabled=payload.get("proxy_enabled", False),
        cache_enabled=payload.get("cache_enabled", False),
        accounts=_parse_accounts(payload.get("accounts", [])),
        account_pools=_parse_account_pools(payload.get("account_pools", [])),
    )
