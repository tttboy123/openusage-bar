from __future__ import annotations

import copy
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .activity_store import DailyUsageRow
from .model_ids import InvalidModelID, canonical_model_id
from .providers.contracts import ImportFailure, UsageImportSuccess


MAX_SESSION_FILES = 20_000
MAX_RELEVANT_LINE_BYTES = 1024 * 1024
MAX_TOTAL_SESSION_BYTES = 16 * 1024 * 1024 * 1024
TAIL_BYTES = 256


@dataclass
class _Aggregate:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0

    def add(self, usage: "_Aggregate") -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_creation_tokens += usage.cache_creation_tokens
        self.reasoning_tokens += usage.reasoning_tokens
        self.total_tokens += usage.total_tokens


@dataclass
class _SessionState:
    device: int
    inode: int
    offset: int = 0
    mtime_ns: int = 0
    tail: bytes = b""
    model_id: str = "unknown"
    models_seen: set[str] = field(default_factory=set)
    cumulative: _Aggregate | None = None
    rows: dict[tuple[str, str], _Aggregate] = field(default_factory=dict)
    pending: dict[str, _Aggregate] = field(default_factory=dict)


class CodexLocalDailyImporter:
    """Incrementally aggregate public Token facts from local Codex JSONL sessions."""

    usage_source_id = "codex.local_sessions"
    cost_source_id = None
    account_ref = ""
    eager_local = True

    def __init__(
        self,
        *,
        session_roots: Iterable[Path] | None = None,
        local_timezone=None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        home = Path.home()
        self.session_roots = tuple(session_roots or (
            home / ".codex/sessions",
            home / ".codex/archived_sessions",
        ))
        self.local_timezone = (
            local_timezone or datetime.now().astimezone().tzinfo or timezone.utc
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[str, _SessionState] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _valid_range(since: date, until: date) -> bool:
        return (
            isinstance(since, date)
            and not isinstance(since, datetime)
            and isinstance(until, date)
            and not isinstance(until, datetime)
            and since <= until
        )

    @staticmethod
    def _model(value: object) -> str:
        if not isinstance(value, str) or not value:
            return "unknown"
        try:
            return canonical_model_id(value)
        except InvalidModelID:
            return "unknown"

    @staticmethod
    def _integer(raw: dict, field: str, default: int = 0) -> int:
        value = raw.get(field, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("invalid token field")
        return value

    @classmethod
    def _usage(cls, info: object, previous: _Aggregate | None) -> tuple[_Aggregate | None, _Aggregate | None]:
        if not isinstance(info, dict):
            raise ValueError("invalid token info")
        raw_last = info.get("last_token_usage")
        raw_total = info.get("total_token_usage")
        cumulative = cls._usage_object(raw_total) if raw_total is not None else previous
        if raw_last is not None:
            return cls._usage_object(raw_last), cumulative
        if cumulative is None:
            return None, previous
        if previous is None:
            return cumulative, cumulative
        fields = (
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens", "reasoning_tokens", "total_tokens",
        )
        values = {}
        for name in fields:
            current = getattr(cumulative, name)
            old = getattr(previous, name)
            values[name] = current - old if current >= old else current
        return _Aggregate(**values), cumulative

    @classmethod
    def _usage_object(cls, raw: object) -> _Aggregate:
        if not isinstance(raw, dict):
            raise ValueError("invalid usage")
        input_tokens = cls._integer(raw, "input_tokens")
        output_tokens = cls._integer(raw, "output_tokens")
        cache_read = cls._integer(raw, "cached_input_tokens")
        cache_write = cls._integer(raw, "cache_write_input_tokens")
        reasoning = cls._integer(raw, "reasoning_output_tokens")
        total = cls._integer(raw, "total_tokens", input_tokens + output_tokens)
        if total < input_tokens + output_tokens or cache_read > input_tokens:
            raise ValueError("inconsistent token usage")
        return _Aggregate(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_write,
            reasoning_tokens=reasoning,
            total_tokens=total,
        )

    def _paths(self) -> tuple[Path, ...]:
        roots = tuple(root for root in self.session_roots if root.is_dir())
        if not roots:
            raise FileNotFoundError("sessions unavailable")
        paths: list[Path] = []
        seen: set[str] = set()
        total_bytes = 0
        for root in roots:
            for path in sorted(root.rglob("*.jsonl")):
                if path.name in seen or path.is_symlink() or not path.is_file():
                    continue
                seen.add(path.name)
                size = path.stat(follow_symlinks=False).st_size
                total_bytes += size
                if len(paths) >= MAX_SESSION_FILES or total_bytes > MAX_TOTAL_SESSION_BYTES:
                    raise ValueError("session boundary exceeded")
                paths.append(path)
        return tuple(paths)

    @staticmethod
    def _can_append(handle, state: _SessionState, stat_result: os.stat_result) -> bool:
        if (
            state.device != stat_result.st_dev
            or state.inode != stat_result.st_ino
            or stat_result.st_size < state.offset
            or stat_result.st_mtime_ns < state.mtime_ns
        ):
            return False
        if state.offset == 0:
            return True
        start = max(0, state.offset - len(state.tail))
        handle.seek(start)
        return handle.read(state.offset - start) == state.tail

    def _parse_line(self, raw: bytes, state: _SessionState) -> None:
        relevant = b'"token_count"' in raw or b'"turn_context"' in raw
        if not relevant:
            return
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise ValueError("invalid session event") from error
        if not isinstance(value, dict):
            raise ValueError("invalid session event")
        payload = value.get("payload")
        if value.get("type") == "turn_context":
            if not isinstance(payload, dict):
                raise ValueError("invalid model event")
            model_id = self._model(payload.get("model"))
            if model_id != "unknown":
                state.models_seen.add(model_id)
            state.model_id = model_id
            return
        if (
            value.get("type") != "event_msg"
            or not isinstance(payload, dict)
            or payload.get("type") != "token_count"
        ):
            return
        stamp = value.get("timestamp")
        if not isinstance(stamp, str):
            raise ValueError("invalid token timestamp")
        try:
            observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("invalid token timestamp") from error
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError("invalid token timestamp")
        info = payload.get("info")
        if info is None:
            return
        usage, cumulative = self._usage(info, state.cumulative)
        state.cumulative = cumulative
        if usage is None or usage.total_tokens == 0:
            return
        day = observed.astimezone(self.local_timezone).date().isoformat()
        if state.model_id == "unknown":
            state.pending.setdefault(day, _Aggregate()).add(usage)
            return
        key = (day, state.model_id)
        state.rows.setdefault(key, _Aggregate()).add(usage)

    def _read_session(self, path: Path, previous: _SessionState | None) -> _SessionState:
        with path.open("rb") as handle:
            stat_result = os.fstat(handle.fileno())
            if previous is not None and self._can_append(handle, previous, stat_result):
                state = copy.deepcopy(previous)
            else:
                state = _SessionState(stat_result.st_dev, stat_result.st_ino)
            if stat_result.st_size == state.offset:
                state.mtime_ns = stat_result.st_mtime_ns
                return state
            handle.seek(state.offset)
            snapshot_end = stat_result.st_size
            while handle.tell() < snapshot_end:
                line_start = handle.tell()
                remaining = snapshot_end - line_start
                raw = handle.readline(min(remaining, MAX_RELEVANT_LINE_BYTES + 1))
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    if len(raw) <= MAX_RELEVANT_LINE_BYTES and handle.tell() == snapshot_end:
                        handle.seek(line_start)
                        break
                    relevant = b'"token_count"' in raw or b'"turn_context"' in raw
                    while handle.tell() < snapshot_end and not raw.endswith(b"\n"):
                        chunk = handle.readline(min(
                            snapshot_end - handle.tell(), MAX_RELEVANT_LINE_BYTES + 1
                        ))
                        relevant = relevant or b'"token_count"' in chunk or b'"turn_context"' in chunk
                        raw = chunk
                    if relevant:
                        raise ValueError("relevant event exceeds boundary")
                    state.offset = handle.tell()
                    continue
                self._parse_line(raw, state)
                state.offset = handle.tell()
            state.mtime_ns = stat_result.st_mtime_ns
            start = max(0, state.offset - TAIL_BYTES)
            handle.seek(start)
            state.tail = handle.read(state.offset - start)
            return state

    def fetch_usage(self, since: date, until: date):
        if not self._valid_range(since, until):
            return ImportFailure("invalid_request")
        if not self._lock.acquire(blocking=False):
            return ImportFailure("import_in_progress")
        try:
            try:
                paths = self._paths()
                states: dict[str, _SessionState] = {}
                for path in paths:
                    states[path.name] = self._read_session(
                        path, self._cache.get(path.name)
                    )
            except FileNotFoundError:
                return ImportFailure("sessions_unavailable")
            except (OSError, ValueError, TypeError):
                return ImportFailure("sessions_invalid")
            self._cache = states
            totals: dict[tuple[str, str], _Aggregate] = {}
            for state in states.values():
                for key, usage in state.rows.items():
                    day = date.fromisoformat(key[0])
                    if since <= day <= until:
                        totals.setdefault(key, _Aggregate()).add(usage)
                for day_key, usage in state.pending.items():
                    day = date.fromisoformat(day_key)
                    if since <= day <= until:
                        model_id = (
                            next(iter(state.models_seen))
                            if len(state.models_seen) == 1
                            else "unknown"
                        )
                        totals.setdefault((day_key, model_id), _Aggregate()).add(usage)
            imported_at = self.clock().astimezone(timezone.utc).isoformat()
            rows = tuple(
                DailyUsageRow(
                    day=day,
                    provider_id="codex",
                    account_ref="",
                    model_id=model,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_creation_tokens=usage.cache_creation_tokens,
                    reasoning_tokens=usage.reasoning_tokens,
                    total_tokens=usage.total_tokens,
                    cost_amount=None,
                    cost_currency=None,
                    cost_basis=None,
                    quality="direct",
                    imported_at=imported_at,
                )
                for (day, model), usage in sorted(totals.items())
            )
            return UsageImportSuccess(since, until, rows)
        finally:
            self._lock.release()
