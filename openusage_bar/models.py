from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Category(StrEnum):
    SUBSCRIPTION = "subscription"
    API = "api"
    LOCAL = "local"


PRODUCT_CATEGORIES = {
    "codex": Category.SUBSCRIPTION,
    "cursor": Category.SUBSCRIPTION,
    "kiro_cli": Category.SUBSCRIPTION,
    "hermes": Category.LOCAL,
    "openclaw": Category.LOCAL,
}


def canonical_category(provider_id: str, reported: Category) -> Category:
    return PRODUCT_CATEGORIES.get(provider_id, reported)


class ProviderStatus(StrEnum):
    OK = "OK"
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"
    AUTH = "AUTH"
    RATE_LIMITED = "RATE_LIMITED"
    ERROR = "ERROR"


ATTENTION_STATUSES = {
    ProviderStatus.AUTH,
    ProviderStatus.RATE_LIMITED,
    ProviderStatus.ERROR,
}


@dataclass(frozen=True)
class ProviderCard:
    provider_id: str
    name: str
    category: Category
    status: ProviderStatus
    primary: str | None
    detail: str | None
    remaining_percent: float | None
    resets_at: datetime | None
    source: str
    refreshed_at: datetime
    stale: bool = False
    last_error: str | None = None
    # Runtime instance identity is separate from its canonical provider family.
    # Optional during the Phase 5 propagation so existing adapters remain valid.
    family_id: str | None = None
    # Catalog source facts used only to publish the sanitized instance ledger.
    credential_source: str | None = None
    source_kind: str | None = None


@dataclass(frozen=True)
class Overview:
    cards: list[ProviderCard]

    @property
    def title(self) -> str:
        attention = sum(card.status in ATTENTION_STATUSES for card in self.cards)
        if attention:
            return f"OU ⚠ {attention}"

        remaining = [
            card.remaining_percent
            for card in self.cards
            if card.category == Category.SUBSCRIPTION
            and card.remaining_percent is not None
            and not card.stale
        ]
        if remaining:
            return f"OU {round(min(remaining))}%"

        if self.cards:
            healthy = sum(card.status == ProviderStatus.OK for card in self.cards)
            return f"OU {healthy}/{len(self.cards)}"
        return "OU --"
