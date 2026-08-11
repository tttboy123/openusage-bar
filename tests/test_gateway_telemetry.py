from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import openusage_bar.gateway.telemetry as telemetry_module
from openusage_bar.gateway.pii import Redactor
from openusage_bar.gateway.telemetry import GatewayTelemetryStore


_REQUEST_AGGREGATE_COLUMN_ORDER = (
    "request_id_hash",
    "provider_id",
    "model_id",
    "started_at",
    "finished_at",
    "status_class",
    "input_tokens",
    "output_tokens",
    "latency_ms",
    "estimated_cost",
    "actual_cost",
    "cache_outcome",
    "fallback_count",
    "error_code",
)
_REQUEST_AGGREGATE_COLUMNS = set(_REQUEST_AGGREGATE_COLUMN_ORDER)


class _Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **changes: int) -> None:
        self.current += timedelta(**changes)


def _record_request(
    store: GatewayTelemetryStore,
    *,
    request_id: str,
    finished_at: datetime,
    provider_id: str = "openai",
    model_id: str = "gpt-5",
    error_code: str | None = None,
) -> None:
    store.record_request(
        request_id=request_id,
        provider_id=provider_id,
        model_id=model_id,
        started_at=finished_at - timedelta(milliseconds=250),
        finished_at=finished_at,
        status_class="2xx",
        input_tokens=12,
        output_tokens=7,
        latency_ms=250,
        estimated_cost="0.0010",
        actual_cost="0.0009",
        cache_outcome="miss",
        fallback_count=0,
        error_code=error_code,
    )


class GatewayTelemetryStoreTests(unittest.TestCase):
    def test_windows_security_seam_hardens_parent_then_telemetry_handle(self):
        calls = []

        class Security:
            def harden_directory(self, path):
                calls.append(("directory", path))

            def harden_file(self, descriptor):
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise AssertionError("expected regular telemetry database")
                calls.append(("file", descriptor))

        with tempfile.TemporaryDirectory() as directory, patch.object(
            telemetry_module,
            "_WINDOWS_FILE_SECURITY",
            Security(),
        ):
            store = GatewayTelemetryStore(
                Path(directory) / "gateway-telemetry.sqlite3"
            )
            store.close()

        self.assertEqual(
            [kind for kind, _value in calls[:2]],
            ["directory", "file"],
        )

    def test_schema_is_an_exact_aggregate_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = GatewayTelemetryStore(Path(directory) / "gateway-telemetry.sqlite3")
            try:
                columns = set(store.column_names("request_aggregates"))
            finally:
                store.close()

        self.assertEqual(columns, _REQUEST_AGGREGATE_COLUMNS)
        for forbidden in ("payload", "prompt", "response", "raw", "secret"):
            self.assertFalse(
                any(forbidden in column.casefold() for column in columns),
                f"telemetry schema contains forbidden material: {forbidden}",
            )

    def test_constructor_requires_exact_telemetry_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename in (
                "activity.sqlite3",
                "activity.sqlite3-wal",
                "activity.sqlite3-shm",
                "telemetry.sqlite3",
                "gateway-cache.sqlite3",
            ):
                with self.subTest(filename=filename), self.assertRaisesRegex(
                    ValueError, "^invalid telemetry database path$"
                ):
                    GatewayTelemetryStore(root / filename)

    def test_constructor_rejects_any_hardlinked_database_inode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "collector"
            gateway_dir = root / "gateway"
            source_dir.mkdir()
            gateway_dir.mkdir()
            activity = source_dir / "activity.sqlite3"
            activity.write_bytes(b"")
            gateway_path = gateway_dir / "gateway-telemetry.sqlite3"
            os.link(activity, gateway_path)

            with self.assertRaisesRegex(
                ValueError, "^invalid telemetry database path$"
            ):
                GatewayTelemetryStore(gateway_path)

            self.assertEqual(activity.read_bytes(), b"")

    def test_constructor_rejects_malformed_table_and_index_signatures(self) -> None:
        correct_table = """
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
        wrong_table = "CREATE TABLE request_aggregates (" + ",".join(
            f"{column} TEXT" for column in _REQUEST_AGGREGATE_COLUMN_ORDER
        ) + ")"
        cases = (
            ("wrong_table_signature", wrong_table, "finished_at"),
            ("wrong_index_signature", correct_table, "provider_id"),
        )

        for label, table_sql, index_column in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "gateway-telemetry.sqlite3"
                with closing(sqlite3.connect(path)) as database:
                    database.execute(table_sql)
                    database.execute(
                        "CREATE INDEX request_aggregates_finished_at_idx "
                        f"ON request_aggregates({index_column})"
                    )
                    database.commit()

                with self.assertRaisesRegex(
                    ValueError, "^telemetry database schema is incompatible$"
                ):
                    GatewayTelemetryStore(path)

    def test_version_one_database_with_raw_model_fails_closed(self) -> None:
        schema = """
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
        now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-telemetry.sqlite3"
            with closing(sqlite3.connect(path)) as database:
                database.execute(schema)
                database.execute("PRAGMA user_version=1")
                database.execute(
                    """
                    INSERT INTO request_aggregates (
                        request_id_hash, provider_id, model_id, started_at,
                        finished_at, status_class, input_tokens,
                        output_tokens, latency_ms, estimated_cost,
                        actual_cost, cache_outcome, fallback_count, error_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "e" * 64,
                        "openai",
                        "raw-model-must-not-survive-v1",
                        (now - timedelta(milliseconds=20)).isoformat(),
                        now.isoformat(),
                        "2xx",
                        1,
                        2,
                        20,
                        None,
                        None,
                        "disabled",
                        0,
                        None,
                    ),
                )
                database.commit()

            with self.assertRaisesRegex(
                ValueError, "^telemetry database schema is incompatible$"
            ):
                GatewayTelemetryStore(path)

    def test_request_identifier_is_hashed_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-telemetry.sqlite3"
            store = GatewayTelemetryStore(path)
            try:
                _record_request(
                    store,
                    request_id="request-private-id",
                    finished_at=datetime(2026, 8, 8, 12, tzinfo=timezone.utc),
                )
            finally:
                store.close()

            with closing(sqlite3.connect(path)) as inspector:
                stored = inspector.execute(
                    "SELECT request_id_hash FROM request_aggregates"
                ).fetchone()[0]

            self.assertEqual(
                stored,
                "9c8ca7298b9ff09e9a8550bd56e2d70e2f406f6b1ebcc948cc75a511afda99fe",
            )
            self.assertNotIn(b"request-private-id", path.read_bytes())

    @unittest.skipIf(os.name == "nt", "Windows privacy is enforced by native ACL")
    def test_file_is_private_wal_database_separate_from_activity_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            activity = root / "activity.sqlite3"
            activity.write_bytes(b"collector-owned-ledger")
            path = root / "gateway-telemetry.sqlite3"

            store = GatewayTelemetryStore(path)
            try:
                self.assertEqual(store.journal_mode, "wal")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            finally:
                store.close()

            self.assertEqual(path.name, "gateway-telemetry.sqlite3")
            self.assertEqual(activity.read_bytes(), b"collector-owned-ledger")

    def test_record_automatically_enforces_default_seven_day_retention(self) -> None:
        now = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
        clock = _Clock(now - timedelta(days=8))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-telemetry.sqlite3"
            store = GatewayTelemetryStore(path, clock=clock)
            try:
                _record_request(
                    store,
                    request_id="expired",
                    finished_at=clock.current,
                )
                clock.advance(days=8)
                _record_request(
                    store,
                    request_id="retained",
                    finished_at=now,
                )
            finally:
                store.close()

            with closing(sqlite3.connect(path)) as inspector:
                count = inspector.execute(
                    "SELECT COUNT(*) FROM request_aggregates"
                ).fetchone()[0]
            self.assertEqual(count, 1)

    def test_constructor_automatically_enforces_default_seven_day_retention(self) -> None:
        now = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
        clock = _Clock(now - timedelta(days=8))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-telemetry.sqlite3"
            store = GatewayTelemetryStore(path, clock=clock)
            try:
                _record_request(
                    store,
                    request_id="expired-before-restart",
                    finished_at=clock.current,
                )
            finally:
                store.close()

            clock.advance(days=8)
            reopened = GatewayTelemetryStore(path, clock=clock)
            reopened.close()

            with closing(sqlite3.connect(path)) as inspector:
                count = inspector.execute(
                    "SELECT COUNT(*) FROM request_aggregates"
                ).fetchone()[0]
            self.assertEqual(count, 0)

    def test_error_code_uses_allowlist_and_rejects_secret_shaped_values(self) -> None:
        secret_shaped = "sk-" + "A" * 30
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-telemetry.sqlite3"
            store = GatewayTelemetryStore(path)
            try:
                for index, unsafe in enumerate(
                    (secret_shaped, "arbitrary_but_code_shaped")
                ):
                    with self.subTest(error_code=unsafe), self.assertRaisesRegex(
                        ValueError, "^invalid telemetry record$"
                    ):
                        _record_request(
                            store,
                            request_id=f"unsafe-{index}",
                            finished_at=datetime(
                                2026, 8, 8, 12, tzinfo=timezone.utc
                            ),
                            error_code=unsafe,
                        )
                _record_request(
                    store,
                    request_id="safe-error",
                    finished_at=datetime(2026, 8, 8, 12, tzinfo=timezone.utc),
                    error_code="timeout",
                )
            finally:
                store.close()

            with closing(sqlite3.connect(path)) as inspector:
                rows = inspector.execute(
                    "SELECT error_code FROM request_aggregates"
                ).fetchall()
            self.assertEqual(rows, [("timeout",)])
            self.assertNotIn(secret_shaped.encode("utf-8"), path.read_bytes())

    def test_provider_and_model_identifiers_are_pii_neutral_before_persistence(
        self,
    ) -> None:
        unsafe_identifiers = (
            ("email", "provider.jane@example.com"),
            ("user_path", "C:/Users/Alice/private/provider"),
            ("secret", "sk-proj-0123456789abcdefghijklmnopqrstuvwxyz"),
        )

        for field in ("provider_id", "model_id"):
            for label, unsafe in unsafe_identifiers:
                with (
                    self.subTest(field=field, kind=label),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    path = Path(directory) / "gateway-telemetry.sqlite3"
                    store = GatewayTelemetryStore(path)
                    values = {"provider_id": "openai", "model_id": "gpt-5"}
                    values[field] = unsafe
                    rejected = False
                    try:
                        _record_request(
                            store,
                            request_id=f"unsafe-{field}-{label}",
                            finished_at=datetime(
                                2026, 8, 8, 12, tzinfo=timezone.utc
                            ),
                            **values,
                        )
                    except ValueError as error:
                        rejected = True
                        self.assertEqual(str(error), "invalid telemetry record")
                    finally:
                        store.close()

                    with closing(sqlite3.connect(path)) as inspector:
                        rows = inspector.execute(
                            f"SELECT {field} FROM request_aggregates"
                        ).fetchall()

                    if rejected:
                        self.assertEqual(rows, [])
                    else:
                        self.assertEqual(len(rows), 1)
                        persisted = rows[0][0]
                        self.assertNotEqual(persisted, unsafe)
                        redacted = Redactor().redact(persisted)
                        self.assertTrue(redacted.cacheable)
                        self.assertEqual(redacted.placeholder_count, 0)

                    raw = b"".join(
                        candidate_path.read_bytes()
                        for candidate_path in (
                            path,
                            Path(f"{path}-wal"),
                            Path(f"{path}-shm"),
                        )
                        if candidate_path.exists()
                    )
                    self.assertNotIn(unsafe.encode("utf-8"), raw)

    def test_clear_is_independent_and_does_not_touch_activity_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            activity = root / "activity.sqlite3"
            activity.write_bytes(b"collector-owned-ledger")
            path = root / "gateway-telemetry.sqlite3"
            store = GatewayTelemetryStore(path)
            try:
                _record_request(
                    store,
                    request_id="clear-me",
                    finished_at=datetime(2026, 8, 8, 12, tzinfo=timezone.utc),
                )
                store.clear()
            finally:
                store.close()

            with closing(sqlite3.connect(path)) as inspector:
                count = inspector.execute(
                    "SELECT COUNT(*) FROM request_aggregates"
                ).fetchone()[0]
            self.assertEqual(count, 0)
            self.assertEqual(activity.read_bytes(), b"collector-owned-ledger")

    def test_concurrent_writers_do_not_lose_aggregate_rows(self) -> None:
        now = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-telemetry.sqlite3"
            initializer = GatewayTelemetryStore(path)
            initializer.close()
            barrier = threading.Barrier(4)

            def write(index: int) -> None:
                store = GatewayTelemetryStore(path)
                try:
                    barrier.wait(timeout=5)
                    _record_request(
                        store,
                        request_id=f"concurrent-{index}",
                        finished_at=now + timedelta(milliseconds=index),
                    )
                finally:
                    store.close()

            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(write, index) for index in range(4)]
                for future in futures:
                    future.result(timeout=10)

            with closing(sqlite3.connect(path)) as inspector:
                count = inspector.execute(
                    "SELECT COUNT(*) FROM request_aggregates"
                ).fetchone()[0]
            self.assertEqual(count, 4)

    def test_queued_writer_survives_one_slow_but_bounded_database_write(self) -> None:
        """A queued handle survives one bounded scheduled-writer stall."""

        now = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-telemetry.sqlite3"
            stores = (GatewayTelemetryStore(path), GatewayTelemetryStore(path))
            original_write_connection = GatewayTelemetryStore._write_connection
            first_entered = threading.Event()
            release_first = threading.Event()
            selection_lock = threading.Lock()
            delay_next = True

            @contextmanager
            def delayed_first_write(
                store: GatewayTelemetryStore,
                *,
                busy_timeout_ms: int = 5_000,
            ):
                nonlocal delay_next
                with original_write_connection(
                    store,
                    busy_timeout_ms=busy_timeout_ms,
                ) as connection:
                    with selection_lock:
                        should_delay = delay_next
                        delay_next = False
                    if should_delay:
                        first_entered.set()
                        if not release_first.wait(timeout=3):
                            raise RuntimeError("bounded test writer was not released")
                    yield connection

            def write(store: GatewayTelemetryStore, index: int) -> None:
                _record_request(
                    store,
                    request_id=f"bounded-{index}",
                    finished_at=now + timedelta(milliseconds=index),
                )

            timer = threading.Timer(0.75, release_first.set)
            try:
                with patch.object(
                    GatewayTelemetryStore,
                    "_write_connection",
                    delayed_first_write,
                ), ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(write, stores[0], 0)
                    self.assertTrue(first_entered.wait(timeout=2))
                    timer.start()
                    second = pool.submit(write, stores[1], 1)
                    first.result(timeout=3)
                    second.result(timeout=3)
            finally:
                release_first.set()
                timer.cancel()
                for store in stores:
                    store.close()

            with closing(sqlite3.connect(path)) as inspector:
                count = inspector.execute(
                    "SELECT COUNT(*) FROM request_aggregates"
                ).fetchone()[0]
            self.assertEqual(count, 2)

    def test_independent_handles_serialize_record_writers(self) -> None:
        """Independent handles coordinate ordinary in-process record writes."""

        now = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway-telemetry.sqlite3"
            stores = tuple(GatewayTelemetryStore(path) for _index in range(4))
            barrier = threading.Barrier(len(stores))
            writer_lock = threading.Lock()
            active_writers = 0
            maximum_active_writers = 0
            entered_handles: list[int] = []
            original_write_connection = GatewayTelemetryStore._write_connection
            shared_states = {id(store._incomplete_state) for store in stores}
            self.assertEqual(len(shared_states), 1)

            @contextmanager
            def traced_write_connection(
                store: GatewayTelemetryStore,
                *,
                busy_timeout_ms: int = 5_000,
            ):
                nonlocal active_writers, maximum_active_writers
                with writer_lock:
                    active_writers += 1
                    maximum_active_writers = max(
                        maximum_active_writers,
                        active_writers,
                    )
                    if active_writers > 1:
                        active_writers -= 1
                        raise sqlite3.OperationalError(
                            "concurrent telemetry writers"
                        )
                    entered_handles.append(id(store))
                try:
                    with original_write_connection(
                        store,
                        busy_timeout_ms=busy_timeout_ms,
                    ) as connection:
                        yield connection
                finally:
                    with writer_lock:
                        active_writers -= 1

            def write(index: int) -> None:
                barrier.wait(timeout=5)
                _record_request(
                    stores[index],
                    request_id=f"serialized-{index}",
                    finished_at=now + timedelta(milliseconds=index),
                )

            try:
                with patch.object(
                    GatewayTelemetryStore,
                    "_write_connection",
                    traced_write_connection,
                ), ThreadPoolExecutor(max_workers=len(stores)) as pool:
                    futures = [
                        pool.submit(write, index)
                        for index in range(len(stores))
                    ]
                    for future in futures:
                        future.result(timeout=5)
            finally:
                for store in stores:
                    store.close()

            with closing(sqlite3.connect(path)) as inspector:
                count = inspector.execute(
                    "SELECT COUNT(*) FROM request_aggregates"
                ).fetchone()[0]
            self.assertEqual(count, len(stores))
            self.assertEqual(len(set(entered_handles)), len(stores))
            self.assertEqual(maximum_active_writers, 1)


if __name__ == "__main__":
    unittest.main()
