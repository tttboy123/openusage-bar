from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import Category, Overview, ProviderCard, ProviderStatus


DEFAULT_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"


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


def _last_rate_limit_event(path: Path) -> tuple[dict[str, Any], datetime] | None:
    try:
        lines = _reverse_lines(path)
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
                return rate_limits, observed_at
    except OSError:
        return None
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

    latest_codex: tuple[dict[str, Any], datetime] | None = None
    latest_other: tuple[dict[str, Any], datetime] | None = None
    for path in candidates:
        event = _last_rate_limit_event(path)
        if event is None:
            continue
        rate_limits, observed_at = event
        target = latest_codex if rate_limits.get("limit_id") == "codex" else latest_other
        if target is None or observed_at > target[1]:
            if rate_limits.get("limit_id") == "codex":
                latest_codex = event
            else:
                latest_other = event
    return latest_codex or latest_other


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


class CodexSubscriptionAdapter:
    def __init__(
        self,
        sessions_root: Path = DEFAULT_SESSIONS_ROOT,
        clock=None,
        max_files: int = 100,
    ) -> None:
        self.sessions_root = sessions_root
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_files = max_files

    def fetch(self) -> Overview:
        event = latest_rate_limit_event(self.sessions_root, self.max_files)
        if event is None:
            return Overview([])
        rate_limits, observed_at = event
        card = parse_rate_limit_card(rate_limits, observed_at, self.clock())
        return Overview([card] if card else [])
