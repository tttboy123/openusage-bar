from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .codex_app_server import read_codex_app_server_rate_limits
from .models import Category, Overview, ProviderCard, ProviderStatus
from .providers.contracts import QuotaFetchFailure, QuotaFetchSuccess
from .providers.quota import percent_observation


DEFAULT_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
RateLimitsReader = Callable[[], dict[str, Any] | None]


@dataclass(frozen=True)
class RateWindow:
    used_percent: float
    window_minutes: int
    resets_at: datetime

    @property
    def remaining_percent(self) -> float:
        return min(100.0, max(0.0, 100.0 - self.used_percent))


def _reverse_lines(path: Path, block_size: int = 64 * 1024) -> Iterator[str]:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        remainder = b""
        while position > 0:
            read_size = min(block_size, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size) + remainder
            lines = chunk.split(b"\n")
            remainder = lines[0]
            for raw in reversed(lines[1:]):
                if raw:
                    yield raw.decode("utf-8", errors="replace")
        if remainder:
            yield remainder.decode("utf-8", errors="replace")


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _normalize_app_server_window(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    return {
        "used_percent": raw.get("usedPercent"),
        "window_minutes": raw.get("windowDurationMins"),
        "resets_at": raw.get("resetsAt"),
    }


def _app_server_codex_rate_limits(response: Any) -> dict[str, Any] | None:
    if not isinstance(response, dict):
        return None
    by_limit_id = response.get("rateLimitsByLimitId")
    selected = by_limit_id.get("codex") if isinstance(by_limit_id, dict) else None
    if not isinstance(selected, dict):
        legacy = response.get("rateLimits")
        if not isinstance(legacy, dict) or legacy.get("limitId") not in {None, "codex"}:
            return None
        selected = legacy
    return {
        "limit_id": "codex",
        "limit_name": selected.get("limitName"),
        "primary": _normalize_app_server_window(selected.get("primary")),
        "secondary": _normalize_app_server_window(selected.get("secondary")),
        "credits": selected.get("credits"),
        "individual_limit": selected.get("individualLimit"),
        "plan_type": selected.get("planType"),
        "rate_limit_reached_type": selected.get("rateLimitReachedType"),
    }


def _has_rate_limit_window(rate_limits: dict[str, Any]) -> bool:
    """Return whether an event contains at least one parseable window shape."""

    for key in ("primary", "secondary"):
        raw = rate_limits.get(key)
        if not isinstance(raw, dict):
            continue
        used = raw.get("used_percent")
        minutes = raw.get("window_minutes")
        resets_at = raw.get("resets_at")
        if (
            isinstance(used, (int, float))
            and not isinstance(used, bool)
            and isinstance(minutes, (int, float))
            and not isinstance(minutes, bool)
            and isinstance(resets_at, (int, float))
            and not isinstance(resets_at, bool)
        ):
            return True
    return False


def _last_rate_limit_event(path: Path) -> tuple[dict[str, Any], datetime] | None:
    try:
        lines = _reverse_lines(path)
        codex_fallback: tuple[dict[str, Any], datetime] | None = None
        legacy_usable: tuple[dict[str, Any], datetime] | None = None
        legacy_fallback: tuple[dict[str, Any], datetime] | None = None
        for line in lines:
            if '"rate_limits"' not in line:
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            payload = event.get("payload") if isinstance(event, dict) else None
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            rate_limits = payload.get("rate_limits")
            observed_at = _parse_timestamp(event.get("timestamp"))
            if isinstance(rate_limits, dict) and observed_at is not None:
                candidate = rate_limits, observed_at
                limit_id = rate_limits.get("limit_id")
                if limit_id == "codex":
                    if codex_fallback is None:
                        codex_fallback = candidate
                    if _has_rate_limit_window(rate_limits):
                        return candidate
                    continue
                if isinstance(limit_id, str) and limit_id.startswith("codex_"):
                    continue
                if legacy_fallback is None:
                    legacy_fallback = candidate
                if legacy_usable is None and _has_rate_limit_window(rate_limits):
                    legacy_usable = candidate
        return codex_fallback or legacy_usable or legacy_fallback
    except OSError:
        return None


def latest_rate_limit_event(
    sessions_root: Path = DEFAULT_SESSIONS_ROOT,
    max_files: int = 100,
) -> tuple[dict[str, Any], datetime] | None:
    try:
        candidates = sorted(
            sessions_root.rglob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:max_files]
    except OSError:
        return None

    latest_codex_usable: tuple[dict[str, Any], datetime] | None = None
    latest_codex: tuple[dict[str, Any], datetime] | None = None
    latest_other_usable: tuple[dict[str, Any], datetime] | None = None
    latest_other: tuple[dict[str, Any], datetime] | None = None
    for path in candidates:
        event = _last_rate_limit_event(path)
        if event is None:
            continue
        rate_limits, observed_at = event
        is_codex_subscription = rate_limits.get("limit_id") == "codex"
        usable = _has_rate_limit_window(rate_limits)
        if is_codex_subscription:
            if latest_codex is None or observed_at > latest_codex[1]:
                latest_codex = event
            if usable and (
                latest_codex_usable is None or observed_at > latest_codex_usable[1]
            ):
                latest_codex_usable = event
        else:
            if latest_other is None or observed_at > latest_other[1]:
                latest_other = event
            if usable and (
                latest_other_usable is None or observed_at > latest_other_usable[1]
            ):
                latest_other_usable = event
    return (
        latest_codex_usable
        or latest_codex
        or latest_other_usable
        or latest_other
    )


def _parse_window(raw: Any, now: datetime) -> RateWindow | None:
    if not isinstance(raw, dict):
        return None
    used = raw.get("used_percent")
    minutes = raw.get("window_minutes")
    reset = raw.get("resets_at")
    if (
        isinstance(used, bool)
        or not isinstance(used, (int, float))
        or isinstance(minutes, bool)
        or not isinstance(minutes, (int, float))
        or isinstance(reset, bool)
        or not isinstance(reset, (int, float))
    ):
        return None
    resets_at = datetime.fromtimestamp(reset, tz=timezone.utc)
    if resets_at <= now.astimezone(timezone.utc):
        return None
    return RateWindow(float(used), int(minutes), resets_at)


def _window_label(minutes: int) -> str:
    if minutes == 10080:
        return "Weekly"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _percent_text(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.1f}"


def _plan_label(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().lower().replace("_", "-")
    aliases = {
        "prolite": "Pro Lite",
        "pro-lite": "Pro Lite",
        "pro": "Pro",
        "plus": "Plus",
        "team": "Team",
    }
    return aliases.get(normalized, normalized.replace("-", " ").title())


def parse_rate_limit_card(
    rate_limits: dict[str, Any],
    observed_at: datetime,
    now: datetime,
) -> ProviderCard | None:
    windows = [
        window
        for window in (
            _parse_window(rate_limits.get("primary"), now),
            _parse_window(rate_limits.get("secondary"), now),
        )
        if window is not None
    ]
    if not windows:
        return None
    windows.sort(key=lambda window: window.window_minutes)
    primary_window = windows[0]
    remaining = primary_window.remaining_percent
    window_label = _window_label(primary_window.window_minutes)
    reached = bool(rate_limits.get("rate_limit_reached_type")) or remaining <= 0
    primary = (
        f"{window_label} limit reached"
        if reached
        else f"{window_label} {_percent_text(remaining)}% remaining"
    )
    detail_parts = []
    if plan := _plan_label(rate_limits.get("plan_type")):
        detail_parts.append(plan)
    for secondary in windows[1:]:
        detail_parts.append(
            f"{_window_label(secondary.window_minutes)} "
            f"{_percent_text(secondary.remaining_percent)}% remaining"
        )
    detail = " · ".join(detail_parts)
    if len(windows) == 1 and detail:
        detail += " plan"

    return ProviderCard(
        provider_id="codex",
        name="Codex",
        category=Category.SUBSCRIPTION,
        status=ProviderStatus.RATE_LIMITED if reached else ProviderStatus.OK,
        primary=primary,
        detail=detail or "Subscription quota",
        remaining_percent=remaining,
        resets_at=primary_window.resets_at,
        source="Codex local rate limits",
        refreshed_at=observed_at,
        family_id="codex",
        credential_source="codex_local_log",
        source_kind="local_log",
    )


def parse_rate_limit_observations(
    rate_limits: dict[str, Any], observed_at: datetime, now: datetime
) -> QuotaFetchSuccess | QuotaFetchFailure:
    windows = [
        window
        for window in (
            _parse_window(rate_limits.get("primary"), now),
            _parse_window(rate_limits.get("secondary"), now),
        )
        if window is not None
    ]
    if not windows:
        return QuotaFetchFailure("quota_unavailable")

    def window_id(minutes: int) -> str:
        if minutes == 300:
            return "five_hour"
        if minutes == 10080:
            return "weekly"
        if minutes in {40320, 43200, 44640}:
            return "monthly"
        return f"minutes_{minutes}"

    observations = tuple(
        percent_observation(
            provider_id="codex", source_id="codex.local_rate_limits",
            quota_name=f"{_window_label(window.window_minutes)} Subscription",
            quota_window=window_id(window.window_minutes),
            remaining_percent=window.remaining_percent,
            resets_at=window.resets_at, observed_at=observed_at,
            applies_to_kind="subscription",
        )
        for window in sorted(windows, key=lambda value: value.window_minutes)
    )
    return QuotaFetchSuccess(observations)


class CodexSubscriptionAdapter:
    def __init__(
        self,
        sessions_root: Path = DEFAULT_SESSIONS_ROOT,
        clock=None,
        max_files: int = 100,
        rate_limits_reader: RateLimitsReader | None = read_codex_app_server_rate_limits,
    ) -> None:
        self.sessions_root = sessions_root
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_files = max_files
        self.rate_limits_reader = rate_limits_reader
        self.last_quota_result = QuotaFetchFailure("not_collected")

    def fetch(self) -> Overview:
        now = self.clock()
        official = None
        if self.rate_limits_reader is not None:
            try:
                official = _app_server_codex_rate_limits(self.rate_limits_reader())
            except Exception:
                official = None
        event = (
            (official, now)
            if official is not None and _has_rate_limit_window(official)
            else latest_rate_limit_event(self.sessions_root, self.max_files)
        )
        if event is None:
            self.last_quota_result = QuotaFetchFailure("quota_unavailable")
            return Overview([])
        rate_limits, observed_at = event
        self.last_quota_result = parse_rate_limit_observations(
            rate_limits, observed_at, now
        )
        card = parse_rate_limit_card(rate_limits, observed_at, now)
        return Overview([card] if card else [])
