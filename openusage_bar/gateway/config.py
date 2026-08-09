"""Secret-free configuration for the opt-in local Gateway."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import GatewayMode


_MAX_CONFIG_BYTES = 64 * 1024
_CONFIG_FIELDS = {
    "enabled",
    "mode",
    "host",
    "port",
    "proxy_enabled",
    "cache_enabled",
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
    )
