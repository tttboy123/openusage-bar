"""Immutable public contracts for the optional Gateway."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class GatewayMode(str, Enum):
    OBSERVE = "observe"
    ADVISE = "advise"
    GATEWAY = "gateway"


class Decision(str, Enum):
    YES = "yes"
    NO = "no"
    DEFER = "defer"


@dataclass(frozen=True)
class ShouldSendRequest:
    provider: str
    model: str
    estimated_tokens: int
    window: str


@dataclass(frozen=True)
class ShouldSendDecision:
    decision: Decision
    confidence: float
    reason: str
    defer_until: str | None
    quota_remaining: float | None
    burn_rate_per_minute: float | None
    predicted_exhaustion_minutes: float | None
