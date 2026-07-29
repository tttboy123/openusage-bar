from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import tempfile
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
CACHE_SCHEMA_VERSION = 1
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_CACHE_FACTS = 200_000
MAX_CACHE_FACTS_PER_SESSION = 4_096
MAX_CACHE_INTEGER = (1 << 63) - 1


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
    tail_digest: bytes = b""
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
        cache_path: Path | None = None,
        local_timezone=None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        home = Path.home()
        uses_default_roots = session_roots is None
        self.session_roots = tuple(session_roots or (
            home / ".codex/sessions",
            home / ".codex/archived_sessions",
        ))
        if cache_path is not None and not cache_path.is_absolute():
            raise ValueError("cache path must be absolute")
        self.cache_path = cache_path or (
            home / ".local/state/openusage-bar/codex-session-cache.json"
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
    def _aggregate_payload(cls, value: _Aggregate | None) -> list[int] | None:
        if value is None:
            return None
        return [
            value.input_tokens,
            value.output_tokens,
            value.cache_read_tokens,
            value.cache_creation_tokens,
            value.reasoning_tokens,
            value.total_tokens,
        ]

    @classmethod
    def _aggregate_from_payload(cls, raw: object) -> _Aggregate | None:
        if raw is None:
            return None
        if not isinstance(raw, list) or len(raw) != 6:
            raise ValueError("invalid cached aggregate")
        values = [cls._cache_integer(value) for value in raw]
        return _Aggregate(*values)

    @classmethod
    def _state_payload(cls, key: str, state: _SessionState) -> dict:
        return {
            "key": key,
            "device": state.device,
            "inode": state.inode,
            "offset": state.offset,
            "mtimeNs": state.mtime_ns,
            "tailDigest": state.tail_digest.hex(),
            "modelId": state.model_id,
            "modelsSeen": sorted(state.models_seen),
            "cumulative": cls._aggregate_payload(state.cumulative),
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
    def _state_from_payload(cls, raw: object) -> tuple[str, _SessionState, int]:
        expected = {
            "key", "device", "inode", "offset", "mtimeNs", "tailDigest",
            "modelId", "modelsSeen", "cumulative", "rows", "pending",
        }
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ValueError("invalid cached state")
        key = raw["key"]
        tail_digest = raw["tailDigest"]
        model_id = raw["modelId"]
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
            or not isinstance(model_id, str)
            or cls._model(model_id) != model_id
        ):
            raise ValueError("invalid cached identity")
        models_seen = raw["modelsSeen"]
        rows = raw["rows"]
        pending = raw["pending"]
        if (
            not isinstance(models_seen, list)
            or len(models_seen) > 256
            or not isinstance(rows, list)
            or len(rows) > MAX_CACHE_FACTS_PER_SESSION
            or not isinstance(pending, list)
            or len(pending) > MAX_CACHE_FACTS_PER_SESSION
        ):
            raise ValueError("cached collection exceeds boundary")
        canonical_models: set[str] = set()
        for value in models_seen:
            if (
                not isinstance(value, str)
                or cls._model(value) != value
                or value in canonical_models
            ):
                raise ValueError("invalid cached model")
            canonical_models.add(value)
        cached_rows: dict[tuple[str, str], _Aggregate] = {}
        for row in rows:
            if not isinstance(row, list) or len(row) != 8:
                raise ValueError("invalid cached row")
            day, model = row[:2]
            if (
                not isinstance(day, str)
                or date.fromisoformat(day).isoformat() != day
                or not isinstance(model, str)
                or cls._model(model) != model
                or (day, model) in cached_rows
            ):
                raise ValueError("invalid cached row identity")
            aggregate = cls._aggregate_from_payload(row[2:])
            if aggregate is None:
                raise ValueError("missing cached aggregate")
            cached_rows[(day, model)] = aggregate
        cached_pending: dict[str, _Aggregate] = {}
        for row in pending:
            if not isinstance(row, list) or len(row) != 7:
                raise ValueError("invalid cached pending row")
            day = row[0]
            if (
                not isinstance(day, str)
                or date.fromisoformat(day).isoformat() != day
                or day in cached_pending
            ):
                raise ValueError("invalid cached pending identity")
            aggregate = cls._aggregate_from_payload(row[1:])
            if aggregate is None:
                raise ValueError("missing cached aggregate")
            cached_pending[day] = aggregate
        state = _SessionState(
            device=cls._cache_integer(raw["device"]),
            inode=cls._cache_integer(raw["inode"]),
            offset=cls._cache_integer(raw["offset"]),
            mtime_ns=cls._cache_integer(raw["mtimeNs"]),
            tail_digest=bytes.fromhex(tail_digest),
            model_id=model_id,
            models_seen=canonical_models,
            cumulative=cls._aggregate_from_payload(raw["cumulative"]),
            rows=cached_rows,
            pending=cached_pending,
        )
        return key, state, len(rows) + len(pending)

    def _load_persistent_cache(self) -> dict[str, _SessionState]:
        path = self.cache_path
        if path is None:
            return {}
        try:
            details = path.lstat()
        except FileNotFoundError:
            return {}
        except OSError:
            self._cache_writable = False
            return {}
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or details.st_mode & 0o077
            or details.st_size > MAX_CACHE_BYTES
        ):
            self._cache_writable = False
            return {}
        try:
            encoded = path.read_bytes()
            payload = json.loads(encoded)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"schemaVersion", "sessions"}
                or payload["schemaVersion"] != CACHE_SCHEMA_VERSION
                or not isinstance(payload["sessions"], list)
                or len(payload["sessions"]) > MAX_SESSION_FILES
            ):
                raise ValueError("invalid cache envelope")
            states: dict[str, _SessionState] = {}
            fact_count = 0
            for raw in payload["sessions"]:
                key, state, facts = self._state_from_payload(raw)
                if key in states:
                    raise ValueError("duplicate cached session")
                states[key] = state
                fact_count += facts
                if fact_count > MAX_CACHE_FACTS:
                    raise ValueError("cache fact boundary exceeded")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            return {}
        self._cache_digest = hashlib.sha256(encoded).digest()
        return states

    def _cache_parent_is_safe(self) -> bool:
        path = self.cache_path
        if path is None or not self._cache_writable:
            return False
        parent = path.parent
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            details = parent.lstat()
        except OSError:
            return False
        return (
            stat.S_ISDIR(details.st_mode)
            and details.st_uid == os.getuid()
            and not (details.st_mode & 0o022)
        )

    def _save_persistent_cache(self, states: dict[str, _SessionState]) -> None:
        path = self.cache_path
        if path is None or not self._cache_parent_is_safe():
            return
        payload = {
            "schemaVersion": CACHE_SCHEMA_VERSION,
            "sessions": [
                self._state_payload(key, state)
                for key, state in sorted(states.items())
            ],
        }
        encoded = json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        if len(encoded) > MAX_CACHE_BYTES:
            return
        digest = hashlib.sha256(encoded).digest()
        if digest == self._cache_digest:
            return
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=".codex-session-cache.", dir=path.parent
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
            os.chmod(path, 0o600)
            self._cache_digest = digest
        except OSError:
            pass
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

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
        tail_length = min(TAIL_BYTES, state.offset)
        start = state.offset - tail_length
        handle.seek(start)
        return (
            hashlib.sha256(handle.read(tail_length)).digest()
            == state.tail_digest
        )

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
            state.tail_digest = hashlib.sha256(
                handle.read(state.offset - start)
            ).digest()
            return state

    def fetch_usage(self, since: date, until: date):
        if not self._valid_range(since, until):
            return ImportFailure("invalid_request")
        if not self._lock.acquire(blocking=False):
            return ImportFailure("import_in_progress")
        try:
            if not self._cache_loaded:
                self._cache = self._load_persistent_cache()
                self._cache_loaded = True
            try:
                paths = self._paths()
                states: dict[str, _SessionState] = {}
                for path in paths:
                    key = self._session_key(path)
                    states[key] = self._read_session(
                        path, self._cache.get(key)
                    )
            except FileNotFoundError:
                return ImportFailure("sessions_unavailable")
            except (OSError, ValueError, TypeError):
                return ImportFailure("sessions_invalid")
            self._cache = states
            self._save_persistent_cache(states)
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
                    token_counting_convention=(
                        "input_includes_cache"
                        if usage.total_tokens
                        == usage.input_tokens + usage.output_tokens
                        else "provider_reported"
                    ),
                )
                for (day, model), usage in sorted(totals.items())
            )
            return UsageImportSuccess(since, until, rows)
        finally:
            self._lock.release()
