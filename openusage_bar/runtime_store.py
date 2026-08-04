"""Independent, short-retention storage for runtime observations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote

from .runtime_observation import (
    SCHEMA_VERSION,
    RuntimeObservation,
    canonical_runtime_timestamp,
)


MAX_DATABASE_BYTES = 64 * 1024 * 1024
MAX_ROWS = 100_000
MAX_GROUPS = 512
RETENTION = timedelta(hours=24)
MAX_FUTURE_SKEW = timedelta(minutes=5)

_TABLES = frozenset({"runtime_meta", "runtime_observations"})


class RuntimeStoreError(RuntimeError):
    """The bounded runtime store could not safely apply an operation."""


@dataclass(frozen=True)
class RuntimeIngestResult:
    accepted_count: int
    duplicate_count: int
    expired_count: int
    pruned_count: int
    revision: int


@dataclass(frozen=True)
class RuntimeCostTotal:
    currency: str
    cost_micros: int


@dataclass(frozen=True)
class RuntimeLatencySummary:
    duration_sample_count: int
    duration_avg_ms: int | None
    duration_p95_ms: int | None
    ttft_sample_count: int
    ttft_avg_ms: int | None
    ttft_p95_ms: int | None


@dataclass(frozen=True)
class RuntimeSummaryGroup:
    provider_id: str
    model_id: str
    scope_ref: str
    observation_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    reasoning_tokens: int | None
    total_tokens: int
    token_counting_conventions: tuple[str, ...]
    status_counts: tuple[tuple[str, int], ...]
    cost_coverage_state: str
    costs: tuple[RuntimeCostTotal, ...]
    latency: RuntimeLatencySummary


@dataclass(frozen=True)
class RuntimeSummary:
    schema_version: int
    runtime_revision: int
    generated_at: str
    window_start: str
    window_end: str
    coverage_state: str
    omitted_group_count: int
    observation_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    reasoning_tokens: int | None
    total_tokens: int
    token_counting_conventions: tuple[str, ...]
    status_counts: tuple[tuple[str, int], ...]
    cost_coverage_state: str
    costs: tuple[RuntimeCostTotal, ...]
    latency: RuntimeLatencySummary
    groups: tuple[RuntimeSummaryGroup, ...]


def _runtime_latency_wire(value: RuntimeLatencySummary) -> dict[str, object]:
    return {
        "durationSampleCount": value.duration_sample_count,
        "durationAvgMs": value.duration_avg_ms,
        "durationP95Ms": value.duration_p95_ms,
        "ttftSampleCount": value.ttft_sample_count,
        "ttftAvgMs": value.ttft_avg_ms,
        "ttftP95Ms": value.ttft_p95_ms,
    }


def _runtime_costs_wire(
    values: tuple[RuntimeCostTotal, ...],
) -> list[dict[str, object]]:
    return [
        {"currency": value.currency, "micros": value.cost_micros}
        for value in values
    ]


def _runtime_tokens_wire(
    value: RuntimeSummary | RuntimeSummaryGroup,
) -> dict[str, object]:
    return {
        "input": value.input_tokens,
        "output": value.output_tokens,
        "cacheRead": value.cache_read_tokens,
        "cacheCreation": value.cache_creation_tokens,
        "reasoning": value.reasoning_tokens,
        "total": value.total_tokens,
        "countingConventions": list(value.token_counting_conventions),
    }


def runtime_summary_wire(value: RuntimeSummary) -> dict[str, object]:
    """Return the one canonical, content-free Runtime Summary representation."""
    groups = [
        {
            "providerId": group.provider_id,
            "modelId": group.model_id,
            "scopeRef": group.scope_ref,
            "observationCount": group.observation_count,
            "tokens": _runtime_tokens_wire(group),
            "statusCounts": dict(group.status_counts),
            "costCoverage": {"state": group.cost_coverage_state},
            "costs": _runtime_costs_wire(group.costs),
            "latency": _runtime_latency_wire(group.latency),
        }
        for group in value.groups
    ]
    return {
        "schemaVersion": value.schema_version,
        "runtimeRevision": value.runtime_revision,
        "generatedAt": value.generated_at,
        "window": {"start": value.window_start, "end": value.window_end},
        "coverage": {
            "state": value.coverage_state,
            "omittedGroupCount": value.omitted_group_count,
        },
        "observationCount": value.observation_count,
        "tokens": _runtime_tokens_wire(value),
        "statusCounts": dict(value.status_counts),
        "costCoverage": {"state": value.cost_coverage_state},
        "costs": _runtime_costs_wire(value.costs),
        "latency": _runtime_latency_wire(value.latency),
        "groups": groups,
    }


def _canonical_row(observation: RuntimeObservation) -> tuple[object, ...]:
    return (
        observation.observation_id,
        observation.provider_id,
        observation.model_id,
        observation.scope_ref,
        canonical_runtime_timestamp(observation.started_at),
        (
            canonical_runtime_timestamp(observation.first_token_at)
            if observation.first_token_at is not None
            else None
        ),
        canonical_runtime_timestamp(observation.completed_at),
        observation.input_tokens,
        observation.output_tokens,
        observation.cache_read_tokens,
        observation.cache_creation_tokens,
        observation.reasoning_tokens,
        observation.total_tokens,
        observation.token_counting_convention,
        observation.status,
        observation.cost_micros,
        observation.cost_currency,
        observation.source_id,
        observation.quality,
        observation.duration_ms,
        observation.ttft_ms,
    )


def _payload_hash(row: tuple[object, ...]) -> str:
    payload = json.dumps(
        row, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _utc(value: datetime, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"{field} must be UTC")
    return value.astimezone(timezone.utc)


def _latency(values: list[int], ttft: list[int]) -> RuntimeLatencySummary:
    def metrics(samples: list[int]) -> tuple[int, int | None, int | None]:
        if not samples:
            return 0, None, None
        ordered = sorted(samples)
        rank = max(0, math.ceil(len(ordered) * 0.95) - 1)
        return len(ordered), sum(ordered) // len(ordered), ordered[rank]

    duration_count, duration_avg, duration_p95 = metrics(values)
    ttft_count, ttft_avg, ttft_p95 = metrics(ttft)
    return RuntimeLatencySummary(
        duration_count,
        duration_avg,
        duration_p95,
        ttft_count,
        ttft_avg,
        ttft_p95,
    )


class RuntimeStore:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        max_rows: int = MAX_ROWS,
        max_groups: int = MAX_GROUPS,
    ) -> None:
        if (
            isinstance(max_rows, bool)
            or not isinstance(max_rows, int)
            or not 1 <= max_rows <= MAX_ROWS
            or isinstance(max_groups, bool)
            or not isinstance(max_groups, int)
            or not 1 <= max_groups <= MAX_GROUPS
        ):
            raise ValueError("invalid runtime store limit")
        self.path = str(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_rows = max_rows
        self.max_groups = max_groups
        self.page_limit_bytes = 0
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        if self.path != ":memory:":
            self._prepare_path(Path(self.path))
        try:
            self._connection = sqlite3.connect(
                self.path, timeout=5.0, check_same_thread=False
            )
            self._connection.row_factory = sqlite3.Row
            with self._lock:
                self._connection.execute("PRAGMA journal_mode=DELETE")
                version = int(
                    self._connection.execute("PRAGMA user_version").fetchone()[0]
                )
                if version > SCHEMA_VERSION:
                    raise RuntimeError("runtime database uses a newer schema")
                self._validate_existing_schema()
                self._set_page_limit()
                self._initialize_schema()
                self._validate_existing_schema(require_all=True)
                with self._connection:
                    self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except Exception:
            if self._connection is not None:
                self._connection.close()
            raise

    @classmethod
    def open_read_only(
        cls,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        max_groups: int = MAX_GROUPS,
    ) -> "RuntimeStore":
        if (
            isinstance(max_groups, bool)
            or not isinstance(max_groups, int)
            or not 1 <= max_groups <= MAX_GROUPS
        ):
            raise ValueError("invalid runtime store limit")
        selected = Path(path)
        if not selected.is_absolute() or selected.is_symlink():
            raise RuntimeError("runtime database path is unavailable")
        try:
            parent = selected.parent.stat()
            identity = selected.stat(follow_symlinks=False)
        except OSError as error:
            raise RuntimeError("runtime database path is unavailable") from error
        if (
            not stat.S_ISDIR(parent.st_mode)
            or selected.parent.is_symlink()
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) & 0o077
            or not stat.S_ISREG(identity.st_mode)
            or identity.st_uid != os.getuid()
            or stat.S_IMODE(identity.st_mode) & 0o077
            or identity.st_size > MAX_DATABASE_BYTES
        ):
            raise RuntimeError("runtime database path is unavailable")

        store = cls.__new__(cls)
        store.path = str(selected)
        store.clock = clock or (lambda: datetime.now(timezone.utc))
        store.max_rows = MAX_ROWS
        store.max_groups = max_groups
        store.page_limit_bytes = MAX_DATABASE_BYTES
        store._lock = threading.RLock()
        store._connection = None
        uri = f"file:{quote(str(selected), safe='/')}?mode=ro"
        try:
            connection = sqlite3.connect(
                uri, uri=True, timeout=5.0, check_same_thread=False
            )
            store._connection = connection
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            current = selected.stat(follow_symlinks=False)
            if (
                (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino)
                or version != SCHEMA_VERSION
            ):
                raise RuntimeError("runtime database schema is incompatible")
            store._validate_existing_schema(require_all=True)
            return store
        except Exception:
            if store._connection is not None:
                store._connection.close()
                store._connection = None
            raise

    @staticmethod
    def _prepare_path(path: Path) -> None:
        if not path.is_absolute() or not path.parent.is_dir():
            raise RuntimeError("runtime database path is unavailable")
        try:
            existing = path.lstat()
        except FileNotFoundError:
            flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags, 0o600)
            os.close(descriptor)
        else:
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                raise RuntimeError("runtime database path is unavailable")
            os.chmod(path, 0o600)

    def _set_page_limit(self) -> None:
        assert self._connection is not None
        page_size = int(self._connection.execute("PRAGMA page_size").fetchone()[0])
        requested = MAX_DATABASE_BYTES // page_size
        actual = int(
            self._connection.execute(f"PRAGMA max_page_count={requested}").fetchone()[0]
        )
        if actual * page_size > MAX_DATABASE_BYTES:
            raise RuntimeError("runtime database exceeds its size limit")
        self.page_limit_bytes = actual * page_size

    def _validate_existing_schema(self, *, require_all: bool = False) -> None:
        assert self._connection is not None
        existing = {
            str(row[0])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if existing - _TABLES:
            raise RuntimeError("runtime database contains incompatible tables")
        expected = {
            "runtime_meta": (
                ("key", "TEXT", 0, 1),
                ("value", "INTEGER", 1, 0),
            ),
            "runtime_observations": (
                ("observation_id", "TEXT", 0, 1),
                ("payload_hash", "TEXT", 1, 0),
                ("provider_id", "TEXT", 1, 0),
                ("model_id", "TEXT", 1, 0),
                ("scope_ref", "TEXT", 1, 0),
                ("started_at", "TEXT", 1, 0),
                ("first_token_at", "TEXT", 0, 0),
                ("completed_at", "TEXT", 1, 0),
                ("input_tokens", "INTEGER", 1, 0),
                ("output_tokens", "INTEGER", 1, 0),
                ("cache_read_tokens", "INTEGER", 1, 0),
                ("cache_creation_tokens", "INTEGER", 1, 0),
                ("reasoning_tokens", "INTEGER", 0, 0),
                ("total_tokens", "INTEGER", 1, 0),
                ("token_counting_convention", "TEXT", 1, 0),
                ("status", "TEXT", 1, 0),
                ("cost_micros", "INTEGER", 0, 0),
                ("cost_currency", "TEXT", 0, 0),
                ("source_id", "TEXT", 1, 0),
                ("quality", "TEXT", 1, 0),
                ("duration_ms", "INTEGER", 1, 0),
                ("ttft_ms", "INTEGER", 0, 0),
            ),
        }
        for table in existing:
            actual = tuple(
                (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
                for row in self._connection.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            )
            if actual != expected[table]:
                raise RuntimeError("runtime database schema is incompatible")
        if require_all and existing != _TABLES:
            raise RuntimeError("runtime database schema is incomplete")

    def _initialize_schema(self) -> None:
        assert self._connection is not None
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS runtime_meta ("
                "key TEXT PRIMARY KEY,value INTEGER NOT NULL)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS runtime_observations ("
                "observation_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,"
                "provider_id TEXT NOT NULL,model_id TEXT NOT NULL,scope_ref TEXT NOT NULL,"
                "started_at TEXT NOT NULL,first_token_at TEXT,completed_at TEXT NOT NULL,"
                "input_tokens INTEGER NOT NULL,output_tokens INTEGER NOT NULL,"
                "cache_read_tokens INTEGER NOT NULL,cache_creation_tokens INTEGER NOT NULL,"
                "reasoning_tokens INTEGER,total_tokens INTEGER NOT NULL,"
                "token_counting_convention TEXT NOT NULL,status TEXT NOT NULL,"
                "cost_micros INTEGER,cost_currency TEXT,source_id TEXT NOT NULL,"
                "quality TEXT NOT NULL,duration_ms INTEGER NOT NULL,ttft_ms INTEGER)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS runtime_observations_completed "
                "ON runtime_observations(completed_at,observation_id)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS runtime_observations_scope "
                "ON runtime_observations(provider_id,model_id,scope_ref,completed_at)"
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO runtime_meta(key,value) VALUES('revision',0)"
            )

    def _now(self) -> datetime:
        return _utc(self.clock(), "clock")

    def _revision_locked(self) -> int:
        assert self._connection is not None
        row = self._connection.execute(
            "SELECT value FROM runtime_meta WHERE key='revision'"
        ).fetchone()
        if row is None or isinstance(row[0], bool) or not isinstance(row[0], int):
            raise RuntimeStoreError("runtime revision is unavailable")
        return int(row[0])

    def _advance_revision_locked(self) -> int:
        assert self._connection is not None
        current = self._revision_locked()
        if current >= 9_223_372_036_854_775_806:
            raise RuntimeStoreError("runtime revision is unavailable")
        next_revision = current + 1
        self._connection.execute(
            "UPDATE runtime_meta SET value=? WHERE key='revision'",
            (next_revision,),
        )
        return next_revision

    def revision(self) -> int:
        with self._lock:
            return self._revision_locked()

    def observation_count(self) -> int:
        with self._lock:
            assert self._connection is not None
            return int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM runtime_observations"
                ).fetchone()[0]
            )

    def observation_ids(self) -> tuple[str, ...]:
        with self._lock:
            assert self._connection is not None
            return tuple(
                str(row[0])
                for row in self._connection.execute(
                    "SELECT observation_id FROM runtime_observations "
                    "ORDER BY completed_at,observation_id"
                ).fetchall()
            )

    def _prune_locked(self, cutoff: str) -> int:
        assert self._connection is not None
        before = self._connection.total_changes
        self._connection.execute(
            "DELETE FROM runtime_observations WHERE completed_at < ?", (cutoff,)
        )
        retained = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM runtime_observations"
            ).fetchone()[0]
        )
        overflow = max(0, retained - self.max_rows)
        if overflow:
            self._connection.execute(
                "DELETE FROM runtime_observations WHERE observation_id IN ("
                "SELECT observation_id FROM runtime_observations "
                "ORDER BY completed_at,observation_id LIMIT ?)",
                (overflow,),
            )
        return self._connection.total_changes - before

    def ingest(
        self, observations: Iterable[RuntimeObservation]
    ) -> RuntimeIngestResult:
        rows = tuple(observations)
        if len(rows) > 256 or any(
            not isinstance(row, RuntimeObservation) for row in rows
        ):
            raise RuntimeStoreError("invalid runtime observation batch")
        identifiers = tuple(row.observation_id for row in rows)
        if len(identifiers) != len(set(identifiers)):
            raise RuntimeStoreError("invalid runtime observation batch")
        now = self._now()
        cutoff_at = now - RETENTION
        if any(row.completed_at > now + MAX_FUTURE_SKEW for row in rows):
            raise RuntimeStoreError("runtime observation time is unavailable")
        active = tuple(row for row in rows if row.completed_at >= cutoff_at)
        expired_count = len(rows) - len(active)
        accepted_count = 0
        duplicate_count = 0
        with self._lock:
            assert self._connection is not None
            try:
                with self._connection:
                    pruned_count = self._prune_locked(
                        canonical_runtime_timestamp(cutoff_at)
                    )
                    for observation in active:
                        values = _canonical_row(observation)
                        digest = _payload_hash(values)
                        existing = self._connection.execute(
                            "SELECT payload_hash FROM runtime_observations "
                            "WHERE observation_id=?",
                            (observation.observation_id,),
                        ).fetchone()
                        if existing is not None:
                            if str(existing[0]) != digest:
                                raise RuntimeStoreError(
                                    "runtime observation identity conflict"
                                )
                            duplicate_count += 1
                            continue
                        self._connection.execute(
                            "INSERT INTO runtime_observations VALUES ("
                            + ",".join("?" for _ in range(22))
                            + ")",
                            (values[0], digest, *values[1:]),
                        )
                        accepted_count += 1
                    pruned_count += self._prune_locked(
                        canonical_runtime_timestamp(cutoff_at)
                    )
                    revision = (
                        self._advance_revision_locked()
                        if accepted_count or pruned_count
                        else self._revision_locked()
                    )
            except sqlite3.Error as error:
                raise RuntimeStoreError("runtime store operation unavailable") from error
        return RuntimeIngestResult(
            accepted_count,
            duplicate_count,
            expired_count,
            pruned_count,
            revision,
        )

    def prune(self) -> int:
        cutoff = canonical_runtime_timestamp(self._now() - RETENTION)
        with self._lock:
            assert self._connection is not None
            try:
                with self._connection:
                    pruned = self._prune_locked(cutoff)
                    if pruned:
                        self._advance_revision_locked()
                    return pruned
            except sqlite3.Error as error:
                raise RuntimeStoreError("runtime store operation unavailable") from error

    @staticmethod
    def _aggregate(rows: list[sqlite3.Row]) -> dict[str, object]:
        status: dict[str, int] = defaultdict(int)
        costs: dict[str, int] = defaultdict(int)
        durations: list[int] = []
        ttfts: list[int] = []
        conventions: set[str] = set()
        reasoning_known = True
        reasoning_total = 0
        for row in rows:
            status[str(row["status"])] += 1
            conventions.add(str(row["token_counting_convention"]))
            if row["cost_micros"] is not None:
                costs[str(row["cost_currency"])] += int(row["cost_micros"])
            durations.append(int(row["duration_ms"]))
            if row["ttft_ms"] is not None:
                ttfts.append(int(row["ttft_ms"]))
            if row["reasoning_tokens"] is None:
                reasoning_known = False
            else:
                reasoning_total += int(row["reasoning_tokens"])
        cost_count = sum(1 for row in rows if row["cost_micros"] is not None)
        cost_coverage = (
            "complete" if cost_count == len(rows)
            else "none" if cost_count == 0
            else "partial"
        )
        return {
            "observation_count": len(rows),
            "input_tokens": sum(int(row["input_tokens"]) for row in rows),
            "output_tokens": sum(int(row["output_tokens"]) for row in rows),
            "cache_read_tokens": sum(int(row["cache_read_tokens"]) for row in rows),
            "cache_creation_tokens": sum(
                int(row["cache_creation_tokens"]) for row in rows
            ),
            "reasoning_tokens": reasoning_total if reasoning_known else None,
            "total_tokens": sum(int(row["total_tokens"]) for row in rows),
            "token_counting_conventions": tuple(sorted(conventions)),
            "status_counts": tuple(sorted(status.items())),
            "cost_coverage_state": cost_coverage,
            "costs": tuple(
                RuntimeCostTotal(currency, costs[currency])
                for currency in sorted(costs)
            ),
            "latency": _latency(durations, ttfts),
        }

    def summary(self, start: datetime, end: datetime) -> RuntimeSummary:
        selected_start = _utc(start, "window start")
        selected_end = _utc(end, "window end")
        if (
            selected_end <= selected_start
            or selected_end - selected_start > RETENTION
        ):
            raise ValueError("invalid runtime summary window")
        with self._lock:
            assert self._connection is not None
            rows = self._connection.execute(
                "SELECT * FROM runtime_observations "
                "WHERE completed_at>=? AND completed_at<=? "
                "ORDER BY provider_id,model_id,scope_ref,completed_at,observation_id",
                (
                    canonical_runtime_timestamp(selected_start),
                    canonical_runtime_timestamp(selected_end),
                ),
            ).fetchall()
            revision = self._revision_locked()
        grouped: dict[tuple[str, str, str], list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            grouped[(
                str(row["provider_id"]),
                str(row["model_id"]),
                str(row["scope_ref"]),
            )].append(row)
        ranked = sorted(
            grouped.items(),
            key=lambda item: (
                -sum(int(row["total_tokens"]) for row in item[1]),
                item[0],
            ),
        )
        selected = sorted(ranked[: self.max_groups], key=lambda item: item[0])
        groups = tuple(
            RuntimeSummaryGroup(*key, **self._aggregate(group_rows))
            for key, group_rows in selected
        )
        omitted = max(0, len(grouped) - len(groups))
        overall = self._aggregate(list(rows))
        return RuntimeSummary(
            schema_version=SCHEMA_VERSION,
            runtime_revision=revision,
            generated_at=canonical_runtime_timestamp(self._now()),
            window_start=canonical_runtime_timestamp(selected_start),
            window_end=canonical_runtime_timestamp(selected_end),
            coverage_state="partial" if omitted else "complete",
            omitted_group_count=omitted,
            groups=groups,
            **overall,
        )

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None


def read_runtime_summary(
    path: str | Path,
    start: datetime,
    end: datetime,
    *,
    clock: Callable[[], datetime] | None = None,
    max_groups: int = MAX_GROUPS,
) -> RuntimeSummary:
    store = RuntimeStore.open_read_only(
        path, clock=clock, max_groups=max_groups
    )
    try:
        return store.summary(start, end)
    finally:
        store.close()
