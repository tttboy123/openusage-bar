"""Aggregate-only telemetry storage for the optional Gateway runtime."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
import threading
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterator

from ..windows_file_security import native_windows_file_security


DEFAULT_RETENTION_DAYS = 7
_WINDOWS_FILE_SECURITY = native_windows_file_security()

SANITIZED_ERROR_CODES = frozenset(
    {
        "authentication_expiry",
        "cache_error",
        "circuit_open",
        "empty_result",
        "failed",
        "fallback_exhausted",
        "gateway_disabled",
        "invalid_upstream_response",
        "policy_rejected",
        "provider_unavailable",
        "rate_limit",
        "stream_interrupted",
        "timeout",
        "unavailable",
        "upstream_client_error",
        "upstream_rate_limited",
        "upstream_server_error",
        "upstream_timeout",
    }
)

_BUSY_TIMEOUT_SECONDS = 5.0
_BUSY_TIMEOUT_MS = int(_BUSY_TIMEOUT_SECONDS * 1_000)
_OPERATION_BUSY_TIMEOUT_MS = 50
_IN_PROCESS_WRITE_WAIT_SECONDS = 0.5
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_MAX_REQUEST_ID_BYTES = 4_096
_MAX_MODEL_LENGTH = 256
_DATABASE_FILENAME = "gateway-telemetry.sqlite3"
_STABLE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_MODEL_SCOPE_PREFIX = "model-sha256:"
_MODEL_SCOPE = re.compile(r"\Amodel-sha256:[0-9a-f]{64}\Z")
_MODEL_SCOPE_DOMAIN = b"openusage-gateway-telemetry-model/v1\0"
_MODEL_SCOPE_ADMISSION = object()
_STORAGE_VERSION = 1
_MAX_BURN_WINDOW_MINUTES = 60

_CONFIG_ERROR = "invalid telemetry configuration"
_PATH_ERROR = "invalid telemetry database path"
_RECORD_ERROR = "invalid telemetry record"
_QUERY_ERROR = "invalid telemetry query"
_CLOCK_ERROR = "invalid telemetry clock"
_TABLE_ERROR = "invalid telemetry table"
_CLOSED_ERROR = "telemetry store is closed"
_SCHEMA_ERROR = "telemetry database schema is incompatible"
_INITIALIZATION_ERROR = "telemetry database initialization failed"
_OPERATION_ERROR = "telemetry database operation failed"

_TABLE_NAME = "request_aggregates"
_INDEX_NAME = "request_aggregates_finished_at_idx"
_SCOPE_INDEX_NAME = "request_aggregates_scope_finished_at_idx"
_TABLE_SIGNATURE = (
    (0, "request_id_hash", "TEXT", 1, None, 1, 0),
    (1, "provider_id", "TEXT", 1, None, 0, 0),
    (2, "model_id", "TEXT", 1, None, 0, 0),
    (3, "started_at", "TEXT", 1, None, 0, 0),
    (4, "finished_at", "TEXT", 1, None, 0, 0),
    (5, "status_class", "TEXT", 1, None, 0, 0),
    (6, "input_tokens", "INTEGER", 0, None, 0, 0),
    (7, "output_tokens", "INTEGER", 0, None, 0, 0),
    (8, "latency_ms", "INTEGER", 0, None, 0, 0),
    (9, "estimated_cost", "TEXT", 0, None, 0, 0),
    (10, "actual_cost", "TEXT", 0, None, 0, 0),
    (11, "cache_outcome", "TEXT", 0, None, 0, 0),
    (12, "fallback_count", "INTEGER", 1, None, 0, 0),
    (13, "error_code", "TEXT", 0, None, 0, 0),
)
_INDEX_SIGNATURES = {
    _INDEX_NAME: (
        0,
        "c",
        0,
        (
            (0, 4, "finished_at", 0, "BINARY", 1),
            (1, -1, None, 0, "BINARY", 0),
        ),
    ),
    _SCOPE_INDEX_NAME: (
        0,
        "c",
        0,
        (
            (0, 1, "provider_id", 0, "BINARY", 1),
            (1, 2, "model_id", 0, "BINARY", 1),
            (2, 4, "finished_at", 1, "BINARY", 1),
            (3, 0, "request_id_hash", 1, "BINARY", 1),
            (4, -1, None, 0, "BINARY", 0),
        ),
    ),
    "sqlite_autoindex_request_aggregates_1": (
        1,
        "pk",
        0,
        (
            (0, 0, "request_id_hash", 0, "BINARY", 1),
            (1, -1, None, 0, "BINARY", 0),
        ),
    ),
}
_EXPECTED_OBJECTS = {
    ("table", _TABLE_NAME),
    ("index", _INDEX_NAME),
    ("index", _SCOPE_INDEX_NAME),
}


class _IncompleteWindowState:
    """Share telemetry gaps and bounded writer coordination for one path."""

    __slots__ = (
        "_lock",
        "_writer_lock",
        "_next_generation",
        "_latest_by_scope",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._writer_lock = threading.Lock()
        self._next_generation = 0
        self._latest_by_scope: dict[tuple[str, str], tuple[str, int]] = {}

    @contextmanager
    def serialized_writer(self) -> Iterator[None]:
        acquired = self._writer_lock.acquire(
            timeout=_IN_PROCESS_WRITE_WAIT_SECONDS
        )
        if not acquired:
            raise RuntimeError(_OPERATION_ERROR)
        try:
            yield
        finally:
            self._writer_lock.release()

    def mark(self, provider_id: str, model_scope: str, finished_at: str) -> None:
        key = (provider_id, model_scope)
        with self._lock:
            self._next_generation += 1
            previous = self._latest_by_scope.get(key)
            latest = finished_at
            if previous is not None and previous[0] > latest:
                latest = previous[0]
            self._latest_by_scope[key] = (latest, self._next_generation)

    def overlaps(
        self,
        provider_id: str,
        model_scope: str,
        *,
        cutoff: str,
        oldest_relevant: str,
    ) -> bool:
        key = (provider_id, model_scope)
        with self._lock:
            entry = self._latest_by_scope.get(key)
            if entry is None:
                return False
            latest = entry[0]
            if latest < oldest_relevant:
                del self._latest_by_scope[key]
                return False
            return latest >= cutoff

    def generation(self) -> int:
        with self._lock:
            return self._next_generation

    def clear_through(self, generation: int) -> None:
        with self._lock:
            self._latest_by_scope = {
                key: entry
                for key, entry in self._latest_by_scope.items()
                if entry[1] > generation
            }


_INCOMPLETE_STATES_LOCK = threading.Lock()
_INCOMPLETE_STATES: dict[str, _IncompleteWindowState] = {}


def _incomplete_state_for(path: Path) -> _IncompleteWindowState:
    try:
        key = os.path.normcase(str(path.resolve(strict=False)))
    except (OSError, RuntimeError):
        key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _INCOMPLETE_STATES_LOCK:
        state = _INCOMPLETE_STATES.get(key)
        if state is None:
            state = _IncompleteWindowState()
            _INCOMPLETE_STATES[key] = state
        return state


class _TelemetryModelScope:
    __slots__ = ("_value", "_admission")

    def __init__(self, value: str, admission: object) -> None:
        if admission is not _MODEL_SCOPE_ADMISSION:
            raise ValueError(_RECORD_ERROR)
        self._value = value
        self._admission = admission

    def __repr__(self) -> str:
        return "<anonymous-model-scope>"


def _model_scope_id_from_raw(value: object) -> str:
    """Hash one untrusted caller-supplied model label."""

    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_MODEL_LENGTH
        or value != value.strip()
        or any(
            unicodedata.category(character).startswith("C")
            for character in value
        )
    ):
        raise ValueError(_RECORD_ERROR)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(_RECORD_ERROR) from None
    if len(encoded) > _MAX_MODEL_LENGTH * 4:
        raise ValueError(_RECORD_ERROR)
    digest = hashlib.sha256(_MODEL_SCOPE_DOMAIN + encoded).hexdigest()
    return f"{_MODEL_SCOPE_PREFIX}{digest}"


def sealed_telemetry_model_scope(value: object) -> _TelemetryModelScope:
    return _TelemetryModelScope(
        _model_scope_id_from_raw(value),
        _MODEL_SCOPE_ADMISSION,
    )


def _stored_model_scope_id(value: object) -> str:
    if (
        type(value) is _TelemetryModelScope
        and value._admission is _MODEL_SCOPE_ADMISSION
        and _MODEL_SCOPE.fullmatch(value._value) is not None
    ):
        return value._value
    return _model_scope_id_from_raw(value)


class GatewayTelemetryStore:
    """Store bounded Gateway aggregates without payloads or raw identifiers."""

    def __init__(
        self,
        path: str | Path,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not _positive_int(retention_days) or (
            clock is not None and not callable(clock)
        ):
            raise ValueError(_CONFIG_ERROR)
        self.path = _coerce_database_path(path)
        self.retention_days = retention_days
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._incomplete_state = _incomplete_state_for(self.path)
        self._closed = False

        try:
            _prepare_private_database(self.path)
            self.journal_mode = self._initialize_database()
        except ValueError:
            raise
        except (OSError, sqlite3.Error):
            raise RuntimeError(_INITIALIZATION_ERROR) from None

    def close(self) -> None:
        """Mark this lightweight store handle closed; safe to call repeatedly."""

        with self._lock:
            self._closed = True

    def record_request(
        self,
        *,
        request_id: str,
        provider_id: str,
        model_id: str | _TelemetryModelScope,
        started_at: datetime,
        finished_at: datetime,
        status_class: str,
        input_tokens: int | None,
        output_tokens: int | None,
        latency_ms: int | None,
        estimated_cost: str | None,
        actual_cost: str | None,
        cache_outcome: str | None,
        fallback_count: int,
        error_code: str | None,
    ) -> None:
        record = _validated_record(
            request_id=request_id,
            provider_id=provider_id,
            model_id=model_id,
            started_at=started_at,
            finished_at=finished_at,
            status_class=status_class,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            estimated_cost=estimated_cost,
            actual_cost=actual_cost,
            cache_outcome=cache_outcome,
            fallback_count=fallback_count,
            error_code=error_code,
        )
        try:
            with self._try_operation_lock():
                with self._incomplete_state.serialized_writer():
                    cutoff = self._automatic_retention_cutoff()
                    with self._write_connection(
                        busy_timeout_ms=_OPERATION_BUSY_TIMEOUT_MS
                    ) as connection:
                        connection.execute(
                            """
                            INSERT INTO request_aggregates (
                                request_id_hash, provider_id, model_id,
                                started_at, finished_at, status_class,
                                input_tokens, output_tokens, latency_ms,
                                estimated_cost, actual_cost, cache_outcome,
                                fallback_count, error_code
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(request_id_hash) DO UPDATE SET
                                provider_id = excluded.provider_id,
                                model_id = excluded.model_id,
                                started_at = excluded.started_at,
                                finished_at = excluded.finished_at,
                                status_class = excluded.status_class,
                                input_tokens = excluded.input_tokens,
                                output_tokens = excluded.output_tokens,
                                latency_ms = excluded.latency_ms,
                                estimated_cost = excluded.estimated_cost,
                                actual_cost = excluded.actual_cost,
                                cache_outcome = excluded.cache_outcome,
                                fallback_count = excluded.fallback_count,
                                error_code = excluded.error_code
                            """,
                            record,
                        )
                        self._delete_before_cutoff(connection, cutoff)
        except (OSError, sqlite3.Error):
            self._mark_incomplete(record)
            raise RuntimeError(_OPERATION_ERROR) from None
        except (RuntimeError, ValueError):
            self._mark_incomplete(record)
            raise

    def apply_retention(self, now: datetime) -> int:
        try:
            cutoff = _iso(_require_aware_datetime(now) - timedelta(days=self.retention_days))
        except (TypeError, ValueError, OverflowError):
            raise ValueError(_RECORD_ERROR) from None

        with self._lock:
            self._require_open()
            try:
                with self._write_connection() as connection:
                    cursor = connection.execute(
                        "DELETE FROM request_aggregates WHERE finished_at < ?",
                        (cutoff,),
                    )
                    return int(cursor.rowcount)
            except (OSError, sqlite3.Error):
                raise RuntimeError(_OPERATION_ERROR) from None

    def recent_burn_rate(
        self,
        provider_id: str,
        model_id: str,
        *,
        window_minutes: int = 5,
        max_rows: int = 1_000,
    ) -> float | None:
        """Return bounded recent Token throughput for one Provider/model.

        ``None`` means that no row in the closed window reported Token data;
        an explicit aggregate containing zero Tokens returns ``0.0``.  The
        fixed window denominator keeps the result deterministic and prevents
        one sparse sample from being presented as a full-minute rate.
        """

        try:
            model_scope = _model_scope_id_from_raw(model_id)
        except ValueError:
            raise ValueError(_QUERY_ERROR) from None
        if (
            not _valid_pii_neutral_stable_id(provider_id)
            or not _bounded_query_int(window_minutes, maximum=60)
            or not _bounded_query_int(max_rows, maximum=1_000)
        ):
            raise ValueError(_QUERY_ERROR)

        with self._try_operation_lock():
            now = self._current_time()
            cutoff = now - timedelta(minutes=window_minutes)
            oldest_relevant = now - timedelta(
                minutes=_MAX_BURN_WINDOW_MINUTES
            )
            if self._incomplete_state.overlaps(
                provider_id,
                model_scope,
                cutoff=_iso(cutoff),
                oldest_relevant=_iso(oldest_relevant),
            ):
                return None
            try:
                with self._read_connection(
                    busy_timeout_ms=_OPERATION_BUSY_TIMEOUT_MS
                ) as connection:
                    rows = connection.execute(
                        """
                        SELECT input_tokens, output_tokens
                        FROM request_aggregates
                        WHERE provider_id = ?
                          AND model_id = ?
                          AND finished_at >= ?
                          AND finished_at <= ?
                          AND (
                              input_tokens IS NOT NULL
                              OR output_tokens IS NOT NULL
                          )
                        ORDER BY finished_at DESC, request_id_hash DESC
                        LIMIT ?
                        """,
                        (
                            provider_id,
                            model_scope,
                            _iso(cutoff),
                            _iso(now),
                            max_rows,
                        ),
                    ).fetchall()
            except (OSError, sqlite3.Error):
                raise RuntimeError(_OPERATION_ERROR) from None

        if self._incomplete_state.overlaps(
            provider_id,
            model_scope,
            cutoff=_iso(cutoff),
            oldest_relevant=_iso(oldest_relevant),
        ):
            return None

        if not rows:
            return None
        total_tokens = 0
        for input_tokens, output_tokens in rows:
            if not _valid_optional_nonnegative_int(input_tokens) or not (
                _valid_optional_nonnegative_int(output_tokens)
            ):
                raise RuntimeError(_OPERATION_ERROR)
            total_tokens += int(input_tokens or 0) + int(output_tokens or 0)
        return float(total_tokens) / float(window_minutes)

    def clear(self) -> None:
        with self._lock:
            self._require_open()
            incomplete_generation = self._incomplete_state.generation()
            try:
                with self._write_connection() as connection:
                    connection.execute("DELETE FROM request_aggregates")
            except (OSError, sqlite3.Error):
                raise RuntimeError(_OPERATION_ERROR) from None
            self._incomplete_state.clear_through(incomplete_generation)

    def column_names(self, table_name: str) -> tuple[str, ...]:
        if table_name != _TABLE_NAME:
            raise ValueError(_TABLE_ERROR)
        with self._lock:
            self._require_open()
            try:
                with self._read_connection() as connection:
                    return tuple(
                        str(row[1])
                        for row in connection.execute(
                            "PRAGMA table_info(request_aggregates)"
                        )
                    )
            except (OSError, sqlite3.Error):
                raise RuntimeError(_OPERATION_ERROR) from None

    def _initialize_database(self) -> str:
        migrated_model_scopes = False
        with self._configured_connection() as connection:
            journal_mode = str(
                connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            ).casefold()
            if journal_mode != "wal":
                raise sqlite3.OperationalError("WAL unavailable")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS request_aggregates (
                        request_id_hash TEXT PRIMARY KEY NOT NULL,
                        provider_id TEXT NOT NULL,
                        model_id TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT NOT NULL,
                        status_class TEXT NOT NULL,
                        input_tokens INTEGER,
                        output_tokens INTEGER,
                        latency_ms INTEGER,
                        estimated_cost TEXT,
                        actual_cost TEXT,
                        cache_outcome TEXT,
                        fallback_count INTEGER NOT NULL,
                        error_code TEXT
                    )
                    """
                )
                _validate_table_signature(connection)
                _validate_known_objects(connection, allow_missing_indexes=True)
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS request_aggregates_finished_at_idx
                    ON request_aggregates(finished_at)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    request_aggregates_scope_finished_at_idx
                    ON request_aggregates(
                        provider_id,
                        model_id,
                        finished_at DESC,
                        request_id_hash DESC
                    )
                    """
                )
                _validate_structural_schema(connection)
                migrated_model_scopes = _migrate_model_scopes(connection)
                self._delete_before_cutoff(
                    connection,
                    self._automatic_retention_cutoff(),
                )
                _validate_schema(connection)
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()
            if migrated_model_scopes:
                _truncate_wal(connection)
                connection.execute("VACUUM")
                _truncate_wal(connection)
                _validate_schema(connection)
        _tighten_database_files(self.path)
        return journal_mode

    @contextmanager
    def _configured_connection(
        self,
        *,
        busy_timeout_ms: int = _BUSY_TIMEOUT_MS,
    ) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=float(busy_timeout_ms) / 1_000.0,
            isolation_level=None,
        )
        try:
            connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA secure_delete=ON")
            yield connection
        finally:
            connection.close()
            # Windows sidecars inherit the protected DACL established before
            # the database is opened. Re-running native ACL mutation for every
            # completed response is both redundant and an unbounded hot-path
            # operation. POSIX still tightens each transient WAL/SHM file.
            if _WINDOWS_FILE_SECURITY is None:
                _tighten_database_files(self.path)

    @contextmanager
    def _read_connection(
        self,
        *,
        busy_timeout_ms: int = _BUSY_TIMEOUT_MS,
    ) -> Iterator[sqlite3.Connection]:
        with self._configured_connection(
            busy_timeout_ms=busy_timeout_ms
        ) as connection:
            yield connection

    @contextmanager
    def _write_connection(
        self,
        *,
        busy_timeout_ms: int = _BUSY_TIMEOUT_MS,
    ) -> Iterator[sqlite3.Connection]:
        with self._configured_connection(
            busy_timeout_ms=busy_timeout_ms
        ) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    @contextmanager
    def _try_operation_lock(self) -> Iterator[None]:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError(_OPERATION_ERROR)
        try:
            self._require_open()
            yield
        finally:
            self._lock.release()

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError(_CLOSED_ERROR)

    def _mark_incomplete(self, record: tuple[object, ...]) -> None:
        provider_id = record[1]
        model_scope = record[2]
        finished_at = record[4]
        if not (
            type(provider_id) is str
            and type(model_scope) is str
            and type(finished_at) is str
        ):
            return
        self._incomplete_state.mark(
            provider_id,
            model_scope,
            finished_at,
        )

    def _automatic_retention_cutoff(self) -> str:
        now = self._current_time()
        try:
            cutoff = now - timedelta(days=self.retention_days)
            return _iso(cutoff)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(_CLOCK_ERROR) from None

    def _current_time(self) -> datetime:
        try:
            now = self._clock()
        except Exception:
            raise ValueError(_CLOCK_ERROR) from None
        try:
            if (
                not isinstance(now, datetime)
                or now.tzinfo is None
                or now.utcoffset() is None
            ):
                raise ValueError(_CLOCK_ERROR)
            return now.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(_CLOCK_ERROR) from None

    @staticmethod
    def _delete_before_cutoff(
        connection: sqlite3.Connection,
        cutoff: str,
    ) -> int:
        cursor = connection.execute(
            "DELETE FROM request_aggregates WHERE finished_at < ?",
            (cutoff,),
        )
        return int(cursor.rowcount)


def _migrate_model_scopes(connection: sqlite3.Connection) -> bool:
    version_row = connection.execute("PRAGMA user_version").fetchone()
    if version_row is None:
        raise ValueError(_SCHEMA_ERROR)
    version = int(version_row[0])
    if version == _STORAGE_VERSION:
        _validate_stored_model_scopes(connection)
        return False
    if version != 0:
        raise ValueError(_SCHEMA_ERROR)

    rows = tuple(
        connection.execute(
            "SELECT request_id_hash, model_id FROM request_aggregates"
        )
    )
    migrated = False
    for request_id_hash, raw_model_id in rows:
        if type(request_id_hash) is not str or type(raw_model_id) is not str:
            raise ValueError(_SCHEMA_ERROR)
        try:
            model_scope = _model_scope_id_from_raw(raw_model_id)
        except ValueError:
            raise ValueError(_SCHEMA_ERROR) from None
        cursor = connection.execute(
            """
            UPDATE request_aggregates
            SET model_id = ?
            WHERE request_id_hash = ? AND model_id = ?
            """,
            (model_scope, request_id_hash, raw_model_id),
        )
        if int(cursor.rowcount) != 1:
            raise ValueError(_SCHEMA_ERROR)
        migrated = True
    connection.execute(f"PRAGMA user_version={_STORAGE_VERSION}")
    _validate_stored_model_scopes(connection)
    return migrated


def _validate_stored_model_scopes(connection: sqlite3.Connection) -> None:
    for (model_scope,) in connection.execute(
        "SELECT DISTINCT model_id FROM request_aggregates"
    ):
        if (
            type(model_scope) is not str
            or _MODEL_SCOPE.fullmatch(model_scope) is None
        ):
            raise ValueError(_SCHEMA_ERROR)


def _truncate_wal(connection: sqlite3.Connection) -> None:
    result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if result is None or int(result[0]) != 0:
        raise sqlite3.OperationalError("WAL checkpoint unavailable")


def _validated_record(
    *,
    request_id: str,
    provider_id: str,
    model_id: str | _TelemetryModelScope,
    started_at: datetime,
    finished_at: datetime,
    status_class: str,
    input_tokens: int | None,
    output_tokens: int | None,
    latency_ms: int | None,
    estimated_cost: str | None,
    actual_cost: str | None,
    cache_outcome: str | None,
    fallback_count: int,
    error_code: str | None,
) -> tuple[object, ...]:
    try:
        request_bytes = request_id.encode("utf-8")
        model_scope = _stored_model_scope_id(model_id)
        started = _require_aware_datetime(started_at)
        finished = _require_aware_datetime(finished_at)
    except (AttributeError, TypeError, UnicodeError, ValueError, OverflowError):
        raise ValueError(_RECORD_ERROR) from None

    valid = (
        isinstance(request_id, str)
        and 0 < len(request_bytes) <= _MAX_REQUEST_ID_BYTES
        and _valid_pii_neutral_stable_id(provider_id)
        and _MODEL_SCOPE.fullmatch(model_scope) is not None
        and _valid_stable_id(status_class)
        and _valid_optional_nonnegative_int(input_tokens)
        and _valid_optional_nonnegative_int(output_tokens)
        and _valid_optional_nonnegative_int(latency_ms)
        and _valid_cost(estimated_cost)
        and _valid_cost(actual_cost)
        and _valid_optional_stable_id(cache_outcome)
        and _nonnegative_int(fallback_count)
        and _valid_optional_error_code(error_code)
        and started <= finished
    )
    if not valid:
        raise ValueError(_RECORD_ERROR)

    return (
        hashlib.sha256(request_bytes).hexdigest(),
        provider_id,
        model_scope,
        _iso(started),
        _iso(finished),
        status_class,
        input_tokens,
        output_tokens,
        latency_ms,
        estimated_cost,
        actual_cost,
        cache_outcome,
        fallback_count,
        error_code,
    )


def _coerce_database_path(value: str | Path) -> Path:
    try:
        path = Path(value)
    except (TypeError, ValueError, OSError):
        raise ValueError(_PATH_ERROR) from None
    if path.name != _DATABASE_FILENAME:
        raise ValueError(_PATH_ERROR)
    return path


def _prepare_private_database(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if _WINDOWS_FILE_SECURITY is not None:
        _WINDOWS_FILE_SECURITY.harden_directory(path.parent)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
    ):
        raise ValueError(_PATH_ERROR)

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_RDWR | os.O_CREAT | nofollow
    descriptor = os.open(path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(_PATH_ERROR)
        if _WINDOWS_FILE_SECURITY is not None:
            _WINDOWS_FILE_SECURITY.harden_file(descriptor)
        else:
            fchmod = getattr(os, "fchmod", None)
            if fchmod is None:
                os.chmod(path, 0o600)
            else:
                fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _tighten_database_files(path: Path) -> None:
    if _WINDOWS_FILE_SECURITY is not None:
        _WINDOWS_FILE_SECURITY.harden_directory(path.parent)
        return
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            metadata = candidate.lstat()
            if stat.S_ISREG(metadata.st_mode):
                if os.chmod in os.supports_follow_symlinks:
                    os.chmod(candidate, 0o600, follow_symlinks=False)
                else:
                    os.chmod(candidate, 0o600)
        except FileNotFoundError:
            continue


def _validate_schema(connection: sqlite3.Connection) -> None:
    _validate_structural_schema(connection)
    version_row = connection.execute("PRAGMA user_version").fetchone()
    if version_row is None or int(version_row[0]) != _STORAGE_VERSION:
        raise ValueError(_SCHEMA_ERROR)
    _validate_stored_model_scopes(connection)


def _validate_structural_schema(connection: sqlite3.Connection) -> None:
    _validate_table_signature(connection)
    _validate_known_objects(connection, allow_missing_indexes=False)
    if _index_signatures(connection) != _INDEX_SIGNATURES:
        raise ValueError(_SCHEMA_ERROR)


def _validate_table_signature(connection: sqlite3.Connection) -> None:
    signature = tuple(
        (
            int(row[0]),
            str(row[1]),
            str(row[2]),
            int(row[3]),
            None if row[4] is None else str(row[4]),
            int(row[5]),
            int(row[6]),
        )
        for row in connection.execute("PRAGMA table_xinfo(request_aggregates)")
    )
    if signature != _TABLE_SIGNATURE:
        raise ValueError(_SCHEMA_ERROR)


def _validate_known_objects(
    connection: sqlite3.Connection,
    *,
    allow_missing_indexes: bool,
) -> None:
    objects = {
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE name NOT GLOB 'sqlite_*'"
        )
    }
    valid = objects.issubset(_EXPECTED_OBJECTS)
    if allow_missing_indexes:
        valid = valid and ("table", _TABLE_NAME) in objects
    else:
        valid = valid and objects == _EXPECTED_OBJECTS
    if not valid:
        raise ValueError(_SCHEMA_ERROR)


def _index_signatures(
    connection: sqlite3.Connection,
) -> dict[str, tuple[object, ...]]:
    metadata = {
        str(row[1]): (int(row[2]), str(row[3]), int(row[4]))
        for row in connection.execute("PRAGMA index_list(request_aggregates)")
    }
    if set(metadata) != set(_INDEX_SIGNATURES):
        return {}

    signatures: dict[str, tuple[object, ...]] = {}
    for name, prefix in metadata.items():
        escaped_name = name.replace('"', '""')
        columns = tuple(
            (
                int(row[0]),
                int(row[1]),
                None if row[2] is None else str(row[2]),
                int(row[3]),
                None if row[4] is None else str(row[4]),
                int(row[5]),
            )
            for row in connection.execute(
                f'PRAGMA index_xinfo("{escaped_name}")'
            )
        )
        signatures[name] = (*prefix, columns)
    return signatures


def _require_aware_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(_RECORD_ERROR)
    offset = value.utcoffset()
    if offset is None:
        raise ValueError(_RECORD_ERROR)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _positive_int(value: object) -> bool:
    return _nonnegative_int(value) and value > 0


def _bounded_query_int(value: object, *, maximum: int) -> bool:
    return type(value) is int and 1 <= value <= maximum


def _nonnegative_int(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= _MAX_SQLITE_INTEGER
    )


def _valid_optional_nonnegative_int(value: object) -> bool:
    return value is None or _nonnegative_int(value)


def _valid_stable_id(value: object) -> bool:
    return isinstance(value, str) and _STABLE_ID.fullmatch(value) is not None


def _valid_pii_neutral_stable_id(value: object) -> bool:
    if type(value) is not str or _STABLE_ID.fullmatch(value) is None:
        return False
    try:
        from .pii import Redactor

        redacted = Redactor().redact(value)
    except (ImportError, TypeError, ValueError):
        return False
    return bool(
        redacted.cacheable
        and redacted.placeholder_count == 0
        and redacted.redacted_text == value
    )


def _valid_optional_stable_id(value: object) -> bool:
    return value is None or _valid_stable_id(value)


def _valid_optional_error_code(value: object) -> bool:
    return value is None or (
        isinstance(value, str) and value in SANITIZED_ERROR_CODES
    )


def _valid_cost(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not value or len(value) > 64:
        return False
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return False
    return amount.is_finite() and amount >= 0
