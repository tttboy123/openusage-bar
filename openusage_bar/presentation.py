from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .models import ATTENTION_STATUSES, Category, Overview, ProviderCard, ProviderStatus


class QuotaSeverity(StrEnum):
    NORMAL = "normal"
    LOW = "low"
    CRITICAL = "critical"


@dataclass(frozen=True)
class AttentionSummary:
    provider_id: str
    message: str
    issue_count: int
    status: ProviderStatus


@dataclass(frozen=True)
class ProviderRow:
    provider_id: str
    name: str
    primary: str
    secondary: str
    reset_label: str | None
    expanded_detail: str
    source_label: str
    status: ProviderStatus
    status_icon: str | None
    status_text: str | None
    quota_fraction: float | None
    quota_severity: QuotaSeverity | None


_STATUS_PRIORITY = {
    ProviderStatus.AUTH: 0,
    ProviderStatus.ERROR: 1,
    ProviderStatus.RATE_LIMITED: 2,
    ProviderStatus.STALE: 3,
    ProviderStatus.UNKNOWN: 4,
    ProviderStatus.OK: 5,
}

_STATUS_ICONS = {
    ProviderStatus.AUTH: "exclamationmark.lock.fill",
    ProviderStatus.ERROR: "xmark.octagon.fill",
    ProviderStatus.RATE_LIMITED: "hourglass.circle.fill",
    ProviderStatus.STALE: "exclamationmark.triangle.fill",
    ProviderStatus.UNKNOWN: "questionmark.circle.fill",
}

_STATUS_TEXT = {
    ProviderStatus.AUTH: "Authentication required",
    ProviderStatus.ERROR: "Refresh failed",
    ProviderStatus.RATE_LIMITED: "Rate limited",
    ProviderStatus.STALE: "Cached data",
    ProviderStatus.UNKNOWN: "Status unknown",
}

_STATUS_PRIMARY = {
    ProviderStatus.AUTH: "Auth required",
    ProviderStatus.ERROR: "Unavailable",
    ProviderStatus.RATE_LIMITED: "Rate limited",
    ProviderStatus.STALE: "Cached data",
    ProviderStatus.UNKNOWN: "Unknown",
}


def effective_status(card: ProviderCard) -> ProviderStatus:
    if card.status in ATTENTION_STATUSES:
        return card.status
    if card.stale or card.status == ProviderStatus.STALE:
        return ProviderStatus.STALE
    return card.status


def is_attention(card: ProviderCard) -> bool:
    return effective_status(card) != ProviderStatus.OK


def _attention_issue_key(card: ProviderCard) -> str:
    # OpenUsage refreshes all of its providers as one batch. When that shared
    # refresh fails, the root error and every retained cached row represent a
    # single actionable problem rather than independent provider failures.
    if card.source == "OpenUsage":
        return "openusage"
    return card.provider_id


def _attention_message(card: ProviderCard, status: ProviderStatus) -> str:
    messages = {
        ProviderStatus.AUTH: f"{card.name} authentication required",
        ProviderStatus.ERROR: f"{card.name} could not refresh",
        ProviderStatus.RATE_LIMITED: f"{card.name} is rate limited",
        ProviderStatus.STALE: f"{card.name} is showing cached data",
        ProviderStatus.UNKNOWN: f"{card.name} status is unknown",
    }
    return messages[status]


def build_attention_summary(overview: Overview) -> AttentionSummary | None:
    issues = [card for card in overview.cards if is_attention(card)]
    if not issues:
        return None
    selected = min(
        issues,
        key=lambda card: (_STATUS_PRIORITY[effective_status(card)], card.name.lower()),
    )
    status = effective_status(selected)
    return AttentionSummary(
        provider_id=selected.provider_id,
        message=_attention_message(selected, status),
        issue_count=len({_attention_issue_key(card) for card in issues}),
        status=status,
    )


def humanize_refresh_age(updated_at: datetime | None, now: datetime) -> str:
    if updated_at is None:
        return "Loading…"
    seconds = max(0, int((now - updated_at).total_seconds()))
    if seconds < 60:
        return "Updated just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"Updated {minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"Updated {hours}h ago"
    return f"Updated {hours // 24}d ago"


def format_reset_label(resets_at: datetime | None, now: datetime) -> str | None:
    if resets_at is None:
        return None
    display_reset = resets_at.astimezone(now.tzinfo) if now.tzinfo else resets_at
    if display_reset.date() == now.date():
        return display_reset.strftime("%H:%M")
    if display_reset.year == now.year:
        return display_reset.strftime("%b %d").replace(" 0", " ")
    return display_reset.strftime("%Y-%m-%d")


def format_reset_detail(resets_at: datetime | None) -> str | None:
    if resets_at is None:
        return None
    date = resets_at.strftime("%b %d, %Y").replace(" 0", " ")
    return f"Resets {date} at {resets_at.strftime('%H:%M')}"


def _quota(card: ProviderCard) -> tuple[float | None, QuotaSeverity | None]:
    if card.category != Category.SUBSCRIPTION or card.remaining_percent is None:
        return None, None
    percent = min(100.0, max(0.0, card.remaining_percent))
    severity = (
        QuotaSeverity.CRITICAL
        if percent < 10
        else QuotaSeverity.LOW
        if percent < 25
        else QuotaSeverity.NORMAL
    )
    return percent / 100.0, severity


def present_row(card: ProviderCard) -> ProviderRow:
    status = effective_status(card)
    status_text = _STATUS_TEXT.get(status)
    primary = card.primary or _STATUS_PRIMARY.get(status, card.status.value)
    secondary = card.detail or status_text or "No additional details"
    if status == ProviderStatus.STALE and "Cached data" not in secondary:
        secondary = f"Cached data · {secondary}"

    local_reset = card.resets_at.astimezone() if card.resets_at else None
    reset_label = format_reset_label(local_reset, datetime.now().astimezone())
    details = [card.detail, status_text, card.last_error, format_reset_detail(local_reset)]
    expanded_detail = " · ".join(dict.fromkeys(part for part in details if part))
    quota_fraction, quota_severity = _quota(card)
    return ProviderRow(
        provider_id=card.provider_id,
        name=card.name,
        primary=primary,
        secondary=secondary,
        reset_label=reset_label,
        expanded_detail=expanded_detail or "No additional details",
        source_label=f"Source: {card.source}",
        status=status,
        status_icon=_STATUS_ICONS.get(status),
        status_text=status_text,
        quota_fraction=quota_fraction,
        quota_severity=quota_severity,
    )
