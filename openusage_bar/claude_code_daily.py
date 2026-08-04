"""Incrementally aggregate Token facts from local Claude Code JSONL sessions.

Claude Code persists every conversation as a JSONL transcript under
``~/.claude/projects/<slug>/<session>.jsonl``. ``assistant`` rows carry the
model id and a ``usage`` object with input / output / cache token counts.
This importer is read-only, incremental and deliberately mirrors the
Codex local session importer's caching contract so the activity ledger can
deduplicate repeated refreshes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from .providers.contracts import ImportFailure, UsageImportSuccess


MAX_SESSION_FILES = 4096
MAX_TOTAL_SESSION_BYTES = 512 * 1024 * 1024
MAX_RELEVANT_LINE_BYTES = 2 * 1024 * 1024
TAIL_BYTES = 4096
MAX_CACHE_FACTS_PER_SESSION = 4096
MAX_CACHE_INTEGER = 10**15
MAX_CACHE_ROWS = 1_000_000


@dataclass
class _Aggregate:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )

    def add(self, other: "_Aggregate") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_creation_tokens += other.cache_creation_tokens


@dataclass
class _SessionState:
    device: int
    inode: int
    offset: int = 0
    mtime_ns: int = 0
    tail_digest: bytes = b""
    rows: dict[tuple[str, str], _Aggregate] = field(default_factory=dict)
    pending: dict[str, _Aggregate] = field(default_factory=dict)


class ClaudeCodeLocalDailyImporter:
    """Incrementally aggregate Token facts from local Claude Code sessions."""

    usage_source_id = "claude_code.local_sessions"
    history_contract_revision = 1
    cost_source_id = None
    account_ref = ""
    eager_local = True

    def __init__(
        self,
        *,
        session_roots: Iterable[Path] | None = None,
        cache_path: Path | None = None,
        local_timezone=None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        home = Path.home()
        uses_default_roots = session_roots is None
        self.session_roots = tuple(session_roots or (home / ".claude" / "projects",))
        if cache_path is not None and not cache_path.is_absolute():
            raise ValueError("cache path must be absolute")
        self.cache_path = cache_path or (
            home / ".local" / "state" / "openusage-bar" / "claude-code-session-cache.json"
            if uses_default_roots
            else None
        )
        self.local_timezone = (
            local_timezone or datetime.now().astimezone().tzinfo or timezone.utc
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[str, _SessionState] = {}
        self._cache_loaded = False
        self._cache_digest: bytes | None = None
        self._cache_writable = True
        self._lock = threading.Lock()

    @staticmethod
    def _session_key(path: Path) -> str:
        return hashlib.sha256(path.name.encode("utf-8")).hexdigest()

    @staticmethod
    def _cache_integer(value: object) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > MAX_CACHE_INTEGER
        ):
            raise ValueError("invalid cache integer")
        return value

    @classmethod
    def _aggregate_payload(cls, value: _Aggregate) -> list[int]:
        return [
            value.input_tokens,
            value.output_tokens,
            value.cache_read_tokens,
            value.cache_creation_tokens,
        ]

    @classmethod
    def _aggregate_from_payload(cls, raw: object) -> _Aggregate:
        if not isinstance(raw, list) or len(raw) != 4:
            raise ValueError("invalid cached aggregate")
        return _Aggregate(*[cls._cache_integer(value) for value in raw])

    @classmethod
    def _state_payload(cls, key: str, state: _SessionState) -> dict:
        return {
            "key": key,
            "device": state.device,
            "inode": state.inode,
            "offset": state.offset,
            "mtimeNs": state.mtime_ns,
            "tailDigest": state.tail_digest.hex(),
            "rows": [
                [day, model, *cls._aggregate_payload(usage)]
                for (day, model), usage in sorted(state.rows.items())
            ],
            "pending": [
                [day, *cls._aggregate_payload(usage)]
                for day, usage in sorted(state.pending.items())
            ],
        }

    @classmethod
    def _state_from_payload(cls, raw: object) -> tuple[str, _SessionState]:
        expected = {
            "key", "device", "inode", "offset", "mtimeNs",
            "tailDigest", "rows", "pending",
        }
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ValueError("invalid cached state")
        key = raw["key"]
        tail_digest = raw["tailDigest"]
        if (
            not isinstance(key, str)
            or len(key) != 64
            or any(character not in "0123456789abcdef" for character in key)
            or not isinstance(tail_digest, str)
            or len(tail_digest) not in (0, 64)
            or any(
                character not in "0123456789abcdef"
                for character in tail_digest
            )
        ):
            raise ValueError("invalid cached identity")
        device = cls._cache_integer(raw["device"])
        inode = cls._cache_integer(raw["inode"])
        offset = cls._cache_integer(raw["offset"])
        mtime_ns = cls._cache_integer(raw["mtimeNs"])
        state = _SessionState(device, inode, offset, mtime_ns, bytes.fromhex(tail_digest))
        rows = raw["rows"]
        pending = raw["pending"]
        if (
            not isinstance(rows, list)
            or len(rows) > MAX_CACHE_FACTS_PER_SESSION
            or not isinstance(pending, list)
            or len(pending) > MAX_CACHE_FACTS_PER_SESSION
        ):
            raise ValueError("cached collection exceeds boundary")
        for row in rows:
            if not isinstance(row, list) or len(row) != 6:
                raise ValueError("invalid cached row")
            day, model = row[:2]
            if (
                not isinstance(day, str)
                or date.fromisoformat(day).isoformat() != day
                or not isinstance(model, str)
                or not model
                or (day, model) in state.rows
            ):
                raise ValueError("invalid cached row identity")
            state.rows[(day, model)] = cls._aggregate_from_payload(row[2:])
        for row in pending:
            if not isinstance(row, list) or len(row) != 5:
                raise ValueError("invalid cached pending row")
            day = row[0]
            if (
                not isinstance(day, str)
                or date.fromisoformat(day).isoformat() != day
                or day in state.pending
            ):
                raise ValueError("invalid cached pending identity")
            state.pending[day] = cls._aggregate_from_payload(row[1:])
        return key, state

    def _load_cache(self) -> None:
        if self._cache_loaded or self.cache_path is None:
            return
        self._cache_loaded = True
        if not self.cache_path.is_file():
            return
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            raw_states = payload.get("states")
            if not isinstance(raw_states, dict):
                raise ValueError("invalid cache envelope")
            for key, raw in raw_states.items():
                cached_key, state = self._state_from_payload(raw)
                if cached_key != key:
                    raise ValueError("cached key mismatch")
                self._cache[key] = state
        except (OSError, ValueError, json.JSONDecodeError, UnicodeError):
            self._cache = {}
            self._cache_writable = False

    def _save_cache(self) -> None:
        if self.cache_path is None or not self._cache_writable:
            return
        payload = {
            "schemaVersion": 1,
            "states": {
                key: self._state_payload(key, state)
                for key, state in sorted(self._cache.items())
            },
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).digest()
        if digest == self._cache_digest:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary, self.cache_path)
            self._cache_digest = digest
        except OSError:
            self._cache_writable = False

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
        tail_length = min(TAIL_BYTES, state.offset)
        start = state.offset - tail_length
        handle.seek(start)
        return (
            hashlib.sha256(handle.read(tail_length)).digest()
            == state.tail_digest
        )

    @staticmethod
    def _usage(message: object) -> _Aggregate | None:
        if not isinstance(message, dict):
            return None
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return None
        result = _Aggregate(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
        )
        if result.total_tokens == 0:
            return None
        return result

    @staticmethod
    def _model(value: object) -> str:
        if isinstance(value, str) and value:
            return value
        return "unknown"

    def _parse_line(self, raw: bytes, state: _SessionState) -> None:
        relevant = b'"assistant"' in raw or b'"usage"' in raw
        if not relevant:
            return
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise ValueError("invalid session event") from error
        if not isinstance(value, dict):
            raise ValueError("invalid session event")
        stamp = value.get("timestamp")
        if not isinstance(stamp, str):
            return
        try:
            observed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return
        if observed.tzinfo is None or observed.utcoffset() is None:
            return
        message = value.get("message")
        usage = self._usage(message)
        if usage is None:
            return
        model_id = self._model(value.get("model") or (message.get("model") if isinstance(message, dict) else None))
        day = observed.astimezone(self.local_timezone).date().isoformat()
        if model_id == "unknown":
            state.pending.setdefault(day, _Aggregate()).add(usage)
            return
        key = (day, model_id)
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
                if len(raw) > MAX_RELEVANT_LINE_BYTES:
                    raise ValueError("session line exceeds boundary")
                self._parse_line(raw, state)
            state.offset = snapshot_end
            state.mtime_ns = stat_result.st_mtime_ns
            tail_length = min(TAIL_BYTES, snapshot_end)
            handle.seek(snapshot_end - tail_length)
            state.tail_digest = hashlib.sha256(handle.read(tail_length)).digest()
            return state

    def _rows(self) -> list:
        from .activity_records import DailyUsageRow

        merged: dict[tuple[str, str], _Aggregate] = {}
        pending: dict[str, _Aggregate] = {}
        total = 0
        for path in self._paths():
            key = self._session_key(path)
            try:
                state = self._read_session(path, self._cache.get(key))
            except (OSError, ValueError):
                continue
            self._cache[key] = state
            for (day, model_id), usage in state.rows.items():
                merged.setdefault((day, model_id), _Aggregate()).add(usage)
            for day, usage in state.pending.items():
                if usage.total_tokens == 0:
                    continue
                pending.setdefault(day, _Aggregate()).add(usage)
        rows: list[DailyUsageRow] = []
        for (day, model_id), usage in merged.items():
            total += 1
            if total > MAX_CACHE_ROWS:
                raise ValueError("usage rows exceed boundary")
            rows.append(
                DailyUsageRow(
                    day=day,
                    provider_id="claude_code",
                    model_id=model_id,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_creation_tokens=usage.cache_creation_tokens,
                    reasoning_tokens=None,
                    total_tokens=usage.total_tokens,
                    cost_amount=None,
                    cost_currency=None,
                    cost_basis=None,
                    quality="direct",
                    account_ref="",
                    token_counting_convention="components_disjoint",
                )
            )
        for day, usage in pending.items():
            total += 1
            if total > MAX_CACHE_ROWS:
                raise ValueError("usage rows exceed boundary")
            rows.append(
                DailyUsageRow(
                    day=day,
                    provider_id="claude_code",
                    model_id="unknown",
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_creation_tokens=usage.cache_creation_tokens,
                    reasoning_tokens=None,
                    total_tokens=usage.total_tokens,
                    cost_amount=None,
                    cost_currency=None,
                    cost_basis=None,
                    quality="direct",
                    account_ref="",
                    token_counting_convention="components_disjoint",
                )
            )
        return rows

    def fetch_usage(self, since: date, until: date):
        if since > until:
            return ImportFailure("invalid_request")
        with self._lock:
            self._load_cache()
            try:
                rows = self._rows()
            except FileNotFoundError:
                return ImportFailure("sessions_unavailable")
            except (OSError, ValueError):
                return ImportFailure("sessions_invalid")
            finally:
                self._save_cache()
        since_text = since.isoformat()
        until_text = until.isoformat()
        scoped = tuple(
            row for row in rows if since_text <= row.day <= until_text
        )
        return UsageImportSuccess(since, until, scoped)
