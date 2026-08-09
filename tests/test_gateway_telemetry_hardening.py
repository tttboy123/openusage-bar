"""RED hardening contracts for optional Gateway telemetry.

These tests freeze four externally observable safety properties identified by
independent review: client-controlled model labels are stable anonymous query
scopes, telemetry never adds lock-contention latency to completed responses,
incomplete telemetry never becomes a falsely precise burn rate, and the
bounded burn-rate query has an order-compatible composite index.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from openusage_bar.gateway.providers import ProviderResult
from openusage_bar.gateway.runtime import GatewayRuntime
from openusage_bar.gateway.telemetry import GatewayTelemetryStore


_RUNTIME_MODEL = "client-request-private-42"
_DIRECT_MODEL = "Jane-Smith"
_LEGACY_MODEL = "Legacy-Jane-Smith"
_SCOPE_SHAPED_RAW_MODEL = "model-sha256:" + ("a" * 64)
_CONTENDED_MODEL = "safe-contended-model"
_MAX_COMPLETED_RESPONSE_SECONDS = 0.75


def _payload(model: str) -> dict[str, object]:
    return {
        "provider": "openai",
        "model": model,
        "request": {
            "model": model,
            "input": "private prompt must remain outside telemetry",
            "stream": False,
        },
    }


def _json_result(
    text: str = "safe public response",
    *,
    input_tokens: int = 3,
    output_tokens: int = 4,
) -> ProviderResult:
    return ProviderResult(
        200,
        (("Content-Type", "application/json"),),
        (
            json.dumps(
                {
                    "output_text": text,
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    },
                },
                separators=(",", ":"),
            ).encode("utf-8"),
        ),
    )


def _record_direct(
    store: GatewayTelemetryStore,
    *,
    model_id: str,
    finished_at: datetime,
    input_tokens: int = 6,
    output_tokens: int = 4,
) -> None:
    store.record_request(
        request_id="local-synthetic-direct-record",
        provider_id="openai",
        model_id=model_id,
        started_at=finished_at - timedelta(milliseconds=20),
        finished_at=finished_at,
        status_class="2xx",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=20,
        estimated_cost=None,
        actual_cost=None,
        cache_outcome="disabled",
        fallback_count=0,
        error_code=None,
    )


def _database_bytes(path: Path) -> bytes:
    return b"".join(
        candidate.read_bytes()
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    )


def _persisted_model_ids(path: Path) -> tuple[str, ...]:
    with closing(sqlite3.connect(path)) as connection:
        return tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT model_id FROM request_aggregates ORDER BY model_id"
            )
        )


class GatewayTelemetryScopePrivacyTests(unittest.TestCase):
    def test_runtime_anonymizes_client_model_before_record_and_remains_queryable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-telemetry.sqlite3"
            store = GatewayTelemetryStore(path)
            try:
                with patch.object(
                    store,
                    "record_request",
                    wraps=store.record_request,
                ) as record:
                    runtime = GatewayRuntime(
                        egress=lambda *_args, **_kwargs: _json_result(),
                        telemetry=store,
                    )

                    response = runtime(_payload(_RUNTIME_MODEL))

                record.assert_called_once()
                recorded = record.call_args.kwargs
                burn_rate = store.recent_burn_rate(
                    "openai",
                    _RUNTIME_MODEL,
                    window_minutes=5,
                    max_rows=100,
                )
            finally:
                store.close()

            persisted_models = _persisted_model_ids(path)
            raw_database = _database_bytes(path)

        self.assertEqual(response["object"], "gateway.response")
        self.assertEqual(response["model"], _RUNTIME_MODEL)
        self.assertEqual(burn_rate, 1.4)
        self.assertEqual(len(persisted_models), 1)
        with self.subTest(channel="runtime record kwargs"):
            self.assertNotIn(_RUNTIME_MODEL, repr(recorded))
        with self.subTest(channel="persisted model scope"):
            self.assertNotEqual(persisted_models[0], _RUNTIME_MODEL)
        with self.subTest(channel="database and sidecars"):
            self.assertFalse(
                _RUNTIME_MODEL.encode("utf-8") in raw_database,
                "raw runtime model was found in SQLite or a sidecar",
            )

    def test_direct_store_anonymizes_model_and_raw_scope_still_queries_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-telemetry.sqlite3"
            store = GatewayTelemetryStore(path)
            try:
                _record_direct(
                    store,
                    model_id=_DIRECT_MODEL,
                    finished_at=datetime.now(timezone.utc),
                )
                burn_rate = store.recent_burn_rate(
                    "openai",
                    _DIRECT_MODEL,
                    window_minutes=5,
                    max_rows=100,
                )
            finally:
                store.close()

            persisted_models = _persisted_model_ids(path)
            raw_database = _database_bytes(path)

        self.assertEqual(burn_rate, 2.0)
        self.assertEqual(len(persisted_models), 1)
        with self.subTest(channel="persisted model scope"):
            self.assertNotEqual(persisted_models[0], _DIRECT_MODEL)
        with self.subTest(channel="database and sidecars"):
            self.assertFalse(
                _DIRECT_MODEL.encode("utf-8") in raw_database,
                "raw direct model was found in SQLite or a sidecar",
            )

    def test_scope_shaped_raw_model_is_rehashed_and_remains_queryable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-telemetry.sqlite3"
            store = GatewayTelemetryStore(path)
            try:
                _record_direct(
                    store,
                    model_id=_SCOPE_SHAPED_RAW_MODEL,
                    finished_at=datetime.now(timezone.utc),
                )
                burn_rate = store.recent_burn_rate(
                    "openai",
                    _SCOPE_SHAPED_RAW_MODEL,
                    window_minutes=5,
                    max_rows=100,
                )
            finally:
                store.close()

            persisted_models = _persisted_model_ids(path)
            raw_database = _database_bytes(path)

        self.assertEqual(burn_rate, 2.0)
        self.assertEqual(len(persisted_models), 1)
        self.assertNotEqual(persisted_models[0], _SCOPE_SHAPED_RAW_MODEL)
        self.assertNotIn(_SCOPE_SHAPED_RAW_MODEL.encode("utf-8"), raw_database)

    def test_legacy_raw_model_row_is_preserved_through_anonymous_migration(
        self,
    ) -> None:
        now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
        old_schema = """
            CREATE TABLE request_aggregates (
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
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-telemetry.sqlite3"
            with closing(sqlite3.connect(path)) as legacy:
                legacy.execute(old_schema)
                legacy.execute(
                    """
                    CREATE INDEX request_aggregates_finished_at_idx
                    ON request_aggregates(finished_at)
                    """
                )
                legacy.execute(
                    """
                    INSERT INTO request_aggregates (
                        request_id_hash, provider_id, model_id, started_at,
                        finished_at, status_class, input_tokens,
                        output_tokens, latency_ms, estimated_cost,
                        actual_cost, cache_outcome, fallback_count, error_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "a" * 64,
                        "openai",
                        _LEGACY_MODEL,
                        (now - timedelta(milliseconds=20)).isoformat(),
                        now.isoformat(),
                        "2xx",
                        6,
                        4,
                        20,
                        None,
                        None,
                        "disabled",
                        0,
                        None,
                    ),
                )
                legacy.commit()

            store = GatewayTelemetryStore(path, clock=lambda: now)
            try:
                burn_rate = store.recent_burn_rate(
                    "openai",
                    _LEGACY_MODEL,
                    window_minutes=5,
                    max_rows=100,
                )
            finally:
                store.close()

            with closing(sqlite3.connect(path)) as inspector:
                rows = inspector.execute(
                    "SELECT model_id FROM request_aggregates"
                ).fetchall()
            raw_database = _database_bytes(path)

        self.assertEqual(burn_rate, 2.0)
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0][0], _LEGACY_MODEL)
        self.assertFalse(
            _LEGACY_MODEL.encode("utf-8") in raw_database,
            "legacy raw model survived migration in SQLite or a sidecar",
        )

    def test_version_zero_scope_shaped_raw_model_is_rehashed_on_migration(
        self,
    ) -> None:
        now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
        old_schema = """
            CREATE TABLE request_aggregates (
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
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-telemetry.sqlite3"
            with closing(sqlite3.connect(path)) as legacy:
                legacy.execute(old_schema)
                legacy.execute(
                    """
                    CREATE INDEX request_aggregates_finished_at_idx
                    ON request_aggregates(finished_at)
                    """
                )
                legacy.execute("PRAGMA user_version=0")
                legacy.execute(
                    """
                    INSERT INTO request_aggregates (
                        request_id_hash, provider_id, model_id, started_at,
                        finished_at, status_class, input_tokens,
                        output_tokens, latency_ms, estimated_cost,
                        actual_cost, cache_outcome, fallback_count, error_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "b" * 64,
                        "openai",
                        _SCOPE_SHAPED_RAW_MODEL,
                        (now - timedelta(milliseconds=20)).isoformat(),
                        now.isoformat(),
                        "2xx",
                        6,
                        4,
                        20,
                        None,
                        None,
                        "disabled",
                        0,
                        None,
                    ),
                )
                legacy.commit()

            store = GatewayTelemetryStore(path, clock=lambda: now)
            try:
                burn_rate = store.recent_burn_rate(
                    "openai",
                    _SCOPE_SHAPED_RAW_MODEL,
                    window_minutes=5,
                    max_rows=100,
                )
            finally:
                store.close()

            with closing(sqlite3.connect(path)) as inspector:
                stored_model = str(
                    inspector.execute(
                        "SELECT model_id FROM request_aggregates"
                    ).fetchone()[0]
                )
                user_version = int(
                    inspector.execute("PRAGMA user_version").fetchone()[0]
                )
            raw_database = _database_bytes(path)

        self.assertEqual(burn_rate, 2.0)
        self.assertEqual(user_version, 1)
        self.assertNotEqual(stored_model, _SCOPE_SHAPED_RAW_MODEL)
        self.assertNotIn(_SCOPE_SHAPED_RAW_MODEL.encode("utf-8"), raw_database)

    def test_version_zero_migration_preserves_colliding_raw_scope_semantics(
        self,
    ) -> None:
        """Each v0 row is transformed from its original value exactly once."""

        now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
        source_model = "legacy-collision-source"
        old_schema = """
            CREATE TABLE request_aggregates (
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
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probe_path = root / "probe" / "gateway-telemetry.sqlite3"
            probe = GatewayTelemetryStore(probe_path, clock=lambda: now)
            try:
                _record_direct(
                    probe,
                    model_id=source_model,
                    finished_at=now,
                )
            finally:
                probe.close()
            colliding_raw_model = _persisted_model_ids(probe_path)[0]

            path = root / "legacy" / "gateway-telemetry.sqlite3"
            path.parent.mkdir()
            with closing(sqlite3.connect(path)) as legacy:
                legacy.execute(old_schema)
                legacy.execute(
                    """
                    CREATE INDEX request_aggregates_finished_at_idx
                    ON request_aggregates(finished_at)
                    """
                )
                legacy.execute("PRAGMA user_version=0")
                legacy.executemany(
                    """
                    INSERT INTO request_aggregates (
                        request_id_hash, provider_id, model_id, started_at,
                        finished_at, status_class, input_tokens,
                        output_tokens, latency_ms, estimated_cost,
                        actual_cost, cache_outcome, fallback_count, error_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            "c" * 64,
                            "openai",
                            source_model,
                            (now - timedelta(milliseconds=20)).isoformat(),
                            now.isoformat(),
                            "2xx",
                            6,
                            4,
                            20,
                            None,
                            None,
                            "disabled",
                            0,
                            None,
                        ),
                        (
                            "d" * 64,
                            "openai",
                            colliding_raw_model,
                            (now - timedelta(milliseconds=20)).isoformat(),
                            now.isoformat(),
                            "2xx",
                            15,
                            5,
                            20,
                            None,
                            None,
                            "disabled",
                            0,
                            None,
                        ),
                    ),
                )
                legacy.commit()

            store = GatewayTelemetryStore(path, clock=lambda: now)
            try:
                source_burn_rate = store.recent_burn_rate(
                    "openai",
                    source_model,
                    window_minutes=5,
                    max_rows=100,
                )
                colliding_burn_rate = store.recent_burn_rate(
                    "openai",
                    colliding_raw_model,
                    window_minutes=5,
                    max_rows=100,
                )
            finally:
                store.close()

            with closing(sqlite3.connect(path)) as inspector:
                stored_models = tuple(
                    str(row[0])
                    for row in inspector.execute(
                        "SELECT model_id FROM request_aggregates ORDER BY model_id"
                    )
                )
                user_version = int(
                    inspector.execute("PRAGMA user_version").fetchone()[0]
                )
            raw_database = _database_bytes(path)

        self.assertEqual(source_burn_rate, 2.0)
        self.assertEqual(colliding_burn_rate, 4.0)
        self.assertEqual(len(set(stored_models)), 2)
        self.assertEqual(user_version, 1)
        self.assertNotIn(source_model.encode("utf-8"), raw_database)


class GatewayTelemetryContentionTests(unittest.TestCase):
    def test_burn_query_detects_write_gap_created_during_its_read(self) -> None:
        """A lock-racing completion invalidates an already-started query."""

        now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
        query_armed = threading.Event()
        query_entered = threading.Event()
        release_query = threading.Event()

        def clock() -> datetime:
            if query_armed.is_set():
                query_entered.set()
                if not release_query.wait(timeout=2):
                    raise RuntimeError("test query release timed out")
            return now

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-telemetry.sqlite3"
            store = GatewayTelemetryStore(path, clock=clock)
            try:
                _record_direct(
                    store,
                    model_id=_CONTENDED_MODEL,
                    finished_at=now,
                    input_tokens=0,
                    output_tokens=0,
                )
                query_armed.set()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(
                        store.recent_burn_rate,
                        "openai",
                        _CONTENDED_MODEL,
                        window_minutes=5,
                        max_rows=100,
                    )
                    try:
                        self.assertTrue(query_entered.wait(timeout=2))
                        started = time.monotonic()
                        with self.assertRaises(RuntimeError):
                            _record_direct(
                                store,
                                model_id=_CONTENDED_MODEL,
                                finished_at=now,
                                input_tokens=10_000,
                                output_tokens=10_000,
                            )
                        failed_write_elapsed = time.monotonic() - started
                    finally:
                        release_query.set()
                    burn_rate = future.result(timeout=2)
            finally:
                release_query.set()
                store.close()

        self.assertLess(failed_write_elapsed, _MAX_COMPLETED_RESPONSE_SECONDS)
        self.assertIsNone(burn_rate)

    def test_failed_fast_writes_make_overlapping_burn_rate_unknown(self) -> None:
        """Dropped high-Token completions must not look like a zero burn."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-telemetry.sqlite3"
            store = GatewayTelemetryStore(path)
            observer = GatewayTelemetryStore(path)
            try:
                _record_direct(
                    store,
                    model_id=_CONTENDED_MODEL,
                    finished_at=datetime.now(timezone.utc),
                    input_tokens=0,
                    output_tokens=0,
                )
                self.assertEqual(
                    store.recent_burn_rate(
                        "openai",
                        _CONTENDED_MODEL,
                        window_minutes=5,
                        max_rows=100,
                    ),
                    0.0,
                )

                runtime = GatewayRuntime(
                    egress=lambda *_args, **_kwargs: _json_result(
                        input_tokens=10_000,
                        output_tokens=10_000,
                    ),
                    telemetry=store,
                )
                with closing(
                    sqlite3.connect(path, timeout=0.0, isolation_level=None)
                ) as locker:
                    locker.execute("PRAGMA busy_timeout=0")
                    locker.execute("BEGIN IMMEDIATE")
                    started = time.monotonic()
                    responses = [
                        runtime(_payload(_CONTENDED_MODEL)) for _index in range(2)
                    ]
                    elapsed = time.monotonic() - started
                    locker.rollback()

                writer_burn_rate = store.recent_burn_rate(
                    "openai",
                    _CONTENDED_MODEL,
                    window_minutes=5,
                    max_rows=100,
                )
                observer_burn_rate = observer.recent_burn_rate(
                    "openai",
                    _CONTENDED_MODEL,
                    window_minutes=5,
                    max_rows=100,
                )
            finally:
                observer.close()
                store.close()

        self.assertEqual(
            [response["status"] for response in responses],
            ["complete", "complete"],
        )
        self.assertLess(elapsed, _MAX_COMPLETED_RESPONSE_SECONDS)
        self.assertIsNone(writer_burn_rate)
        self.assertIsNone(observer_burn_rate)

    def test_locked_telemetry_never_serializes_two_completed_json_responses(
        self,
    ) -> None:
        """Optional storage must not add its five-second SQLite wait to calls."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-telemetry.sqlite3"
            store = GatewayTelemetryStore(path)
            runtime_ready = threading.Barrier(3)

            def egress(*_args, **_kwargs) -> ProviderResult:
                runtime_ready.wait(timeout=2)
                return _json_result()

            runtime = GatewayRuntime(egress=egress, telemetry=store)
            with closing(
                sqlite3.connect(path, timeout=0.0, isolation_level=None)
            ) as locker:
                locker.execute("PRAGMA busy_timeout=0")
                locker.execute("BEGIN IMMEDIATE")
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [
                        pool.submit(runtime, _payload(f"safe-model-{index}"))
                        for index in range(2)
                    ]
                    try:
                        runtime_ready.wait(timeout=2)
                        started = time.monotonic()
                        completed, _pending = wait(
                            futures,
                            timeout=_MAX_COMPLETED_RESPONSE_SECONDS,
                        )
                        elapsed = time.monotonic() - started
                        completed_while_locked = len(completed) == len(futures)
                    finally:
                        locker.rollback()
                    responses = [future.result(timeout=2) for future in futures]
            store.close()

        self.assertEqual(
            [response["object"] for response in responses],
            ["gateway.response", "gateway.response"],
        )
        self.assertEqual(
            [response["status"] for response in responses],
            ["complete", "complete"],
        )
        self.assertNotIn("telemetry", repr(responses).casefold())
        self.assertTrue(
            completed_while_locked,
            "two completed responses exceeded the 0.75s telemetry budget",
        )
        self.assertLess(elapsed, _MAX_COMPLETED_RESPONSE_SECONDS)


class GatewayTelemetryQueryPlanTests(unittest.TestCase):
    def test_burn_rate_has_scope_time_order_index_without_temp_sort(self) -> None:
        query = """
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
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-telemetry.sqlite3"
            store = GatewayTelemetryStore(path)
            store.close()

            with closing(sqlite3.connect(path)) as connection:
                compatible_indexes: list[str] = []
                for index_row in connection.execute(
                    "PRAGMA index_list(request_aggregates)"
                ):
                    index_name = str(index_row[1])
                    escaped_name = index_name.replace('"', '""')
                    key_columns = tuple(
                        (str(row[2]), int(row[3]))
                        for row in connection.execute(
                            f'PRAGMA index_xinfo("{escaped_name}")'
                        )
                        if int(row[5]) == 1 and row[2] is not None
                    )
                    if (
                        tuple(name for name, _descending in key_columns[:4])
                        == (
                            "provider_id",
                            "model_id",
                            "finished_at",
                            "request_id_hash",
                        )
                        and key_columns[2][1] == key_columns[3][1]
                    ):
                        compatible_indexes.append(index_name)

                now = datetime.now(timezone.utc)
                plan_rows = connection.execute(
                    "EXPLAIN QUERY PLAN " + query,
                    (
                        "openai",
                        "scope-placeholder",
                        (now - timedelta(minutes=5)).isoformat(),
                        now.isoformat(),
                        100,
                    ),
                ).fetchall()

        plan = " | ".join(str(row[3]) for row in plan_rows).upper()
        with self.subTest(contract="composite scope/time/order index"):
            self.assertTrue(
                compatible_indexes,
                "missing provider/model/finished_at/request_id_hash index",
            )
        with self.subTest(contract="no table or time-only range scan"):
            self.assertNotIn("SCAN REQUEST_AGGREGATES", plan)
            self.assertIn("PROVIDER_ID=?", plan)
            self.assertIn("MODEL_ID=?", plan)
            self.assertIn("FINISHED_AT>?", plan)
        with self.subTest(contract="no temporary order tree"):
            self.assertNotIn("TEMP B-TREE", plan)


if __name__ == "__main__":
    unittest.main()
