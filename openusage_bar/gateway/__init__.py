"""Opt-in local Gateway contracts and configuration."""

from .config import GatewayConfig, load_gateway_config
from .contracts import (
    Decision,
    GatewayMode,
    ShouldSendDecision,
    ShouldSendRequest,
)

__all__ = [
    "Decision",
    "GatewayConfig",
    "GatewayMode",
    "ShouldSendDecision",
    "ShouldSendRequest",
    "load_gateway_config",
]
