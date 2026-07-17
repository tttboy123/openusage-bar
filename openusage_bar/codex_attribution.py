from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

from .codex_subscription import DEFAULT_SESSIONS_ROOT
from .config import ID_PATTERN


MAX_SESSION_FILES = 5000
MAX_SESSION_FILE_BYTES = 256 * 1024 * 1024
MAX_SESSION_LINE_BYTES = 1024 * 1024


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _path_day(path: Path) -> date | None:
    parts = path.parts
    for index in range(len(parts) - 3):
        try:
            candidate = date(
                int(parts[index]), int(parts[index + 1]), int(parts[index + 2])
            )
        except (TypeError, ValueError):
            continue
        if (
            len(parts[index]) == 4
            and len(parts[index + 1]) == 2
            and len(parts[index + 2]) == 2
        ):
            return candidate
    return None


class CodexAttributionResolver:
    """Infer only the model name needed to repair OpenUsage 0.23 Unknown rows.

    The resolver never retains prompts, responses, token payloads, paths, or session
    identifiers. Any incomplete or ambiguous evidence disables repair for the affected
    import instead of guessing.
    """

    def __init__(
        self,
        sessions_root: Path = DEFAULT_SESSIONS_ROOT,
        *,
        local_timezone: tzinfo | None = None,
        max_files: int = MAX_SESSION_FILES,
    ) -> None:
        self.sessions_root = sessions_root
        self.local_timezone = (
            local_timezone or datetime.now().astimezone().tzinfo or timezone.utc
        )
        self.max_files = max_files

    def target_models(self, since: date, until: date) -> dict[str, str]:
        if since > until or self.max_files <= 0:
            return {}
        try:
            candidates = [
                path
                for path in self.sessions_root.rglob("*.jsonl")
                if self._path_may_overlap(path, since, until)
            ]
        except OSError:
            return {}
        if len(candidates) > self.max_files:
            return {}

        models_by_day: dict[str, set[str]] = defaultdict(set)
        ambiguous_days: set[str] = set()
        for path in candidates:
            evidence = self._session_evidence(path, since, until)
            if evidence is None:
                path_day = _path_day(path)
                if path_day is None:
                    return {}
                if since <= path_day <= until:
                    ambiguous_days.add(path_day.isoformat())
                continue
            days, models = evidence
            if not days:
                continue
            if len(models) != 1:
                ambiguous_days.update(days)
                continue
            model = next(iter(models))
            for day in days:
                models_by_day[day].add(model)

        return {
            day: next(iter(models))
            for day, models in models_by_day.items()
            if day not in ambiguous_days and len(models) == 1
        }

    @staticmethod
    def _path_may_overlap(path: Path, since: date, until: date) -> bool:
        path_day = _path_day(path)
        if path_day is None:
            return True
        return since - timedelta(days=1) <= path_day <= until + timedelta(days=1)

    def _session_evidence(
        self, path: Path, since: date, until: date
    ) -> tuple[set[str], set[str]] | None:
        try:
            if path.stat().st_size > MAX_SESSION_FILE_BYTES:
                return None
            models: set[str] = set()
            pre_model_days: set[str] = set()
            saw_model = False
            with path.open("rb") as handle:
                for raw_line in handle:
                    if b'"turn_context"' not in raw_line and b'"token_count"' not in raw_line:
                        continue
                    if len(raw_line) > MAX_SESSION_LINE_BYTES:
                        return None
                    try:
                        event = json.loads(raw_line)
                    except (json.JSONDecodeError, TypeError, UnicodeError):
                        return None
                    if not isinstance(event, dict):
                        return None
                    event_type = event.get("type")
                    if event_type not in {"turn_context", "event_msg"}:
                        continue
                    payload = event.get("payload")
                    if not isinstance(payload, dict):
                        return None
                    if event_type == "turn_context":
                        model = payload.get("model")
                        if not isinstance(model, str) or ID_PATTERN.fullmatch(model) is None:
                            return None
                        models.add(model)
                        saw_model = True
                    elif payload.get("type") == "token_count" and not saw_model:
                        observed_at = _timestamp(event.get("timestamp"))
                        if observed_at is None:
                            return None
                        local_day = observed_at.astimezone(self.local_timezone).date()
                        if since <= local_day <= until:
                            pre_model_days.add(local_day.isoformat())
            return pre_model_days, models
        except OSError:
            return None
