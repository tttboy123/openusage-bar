from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openusage_bar.plugin.store import DecisionNotFound, IdempotencyConflict, PluginStore


NOW = datetime(2026, 8, 10, 4, 5, 6, tzinfo=timezone.utc)
KEY = "idem_0123456789abcdef0123456789abcdef"


class PluginStoreTests(unittest.TestCase):
    def test_same_key_same_digest_replays_across_reopen_without_raw_secret_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plugin.sqlite3"
            calls = 0

            def operation() -> tuple[int, dict[str, object]]:
                nonlocal calls
                calls += 1
                return 200, {"apiVersion": "plugin.openusage/v1", "value": "safe"}

            first_store = PluginStore(path, clock=lambda: NOW)
            first = first_store.execute_idempotent(
                principal="loom", route="/plugin/v1/route-advice",
                key=KEY, projection={"estimatedTokens": 7}, operation=operation,
            )
            first_store.close()
            second_store = PluginStore(path, clock=lambda: NOW + timedelta(seconds=1))
            second = second_store.execute_idempotent(
                principal="loom", route="/plugin/v1/route-advice",
                key=KEY, projection={"estimatedTokens": 7}, operation=operation,
            )
            second_store.close()

            self.assertEqual(first, second)
            self.assertEqual(calls, 1)
            raw = path.read_bytes()
            self.assertNotIn(KEY.encode(), raw)
            self.assertNotIn(b"estimatedTokens", raw)

    def test_same_key_different_payload_is_stable_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = PluginStore(Path(temporary) / "plugin.sqlite3", clock=lambda: NOW)
            store.execute_idempotent(
                principal="codex", route="/plugin/v1/route-advice", key=KEY,
                projection={"estimatedTokens": 1}, operation=lambda: (200, {"ok": True}),
            )
            with self.assertRaises(IdempotencyConflict):
                store.execute_idempotent(
                    principal="codex", route="/plugin/v1/route-advice", key=KEY,
                    projection={"estimatedTokens": 2}, operation=lambda: (200, {"ok": False}),
                )
            store.close()

    def test_thread_race_executes_operation_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = PluginStore(
                Path(temporary) / "plugin.sqlite3", clock=lambda: NOW,
                waiter_timeout_seconds=1.0,
            )
            barrier = threading.Barrier(8)
            release = threading.Event()
            lock = threading.Lock()
            calls = 0
            results: list[tuple[int, dict[str, object]]] = []

            def operation() -> tuple[int, dict[str, object]]:
                nonlocal calls
                with lock:
                    calls += 1
                release.wait(1)
                return 201, {"receipt": "one"}

            def invoke() -> None:
                barrier.wait()
                result = store.execute_idempotent(
                    principal="claude_code", route="/plugin/v1/outcomes", key=KEY,
                    projection={"decisionId": "decision_" + "a" * 32},
                    operation=operation,
                )
                with lock:
                    results.append(result)

            threads = [threading.Thread(target=invoke) for _ in range(8)]
            for thread in threads:
                thread.start()
            release.set()
            for thread in threads:
                thread.join(2)

            self.assertEqual(calls, 1)
            self.assertEqual(results, [(201, {"receipt": "one"})] * 8)
            store.close()

    def test_schema_is_independent_and_does_not_store_raw_key_or_request_body(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plugin.sqlite3"
            store = PluginStore(path, clock=lambda: NOW)
            store.execute_idempotent(
                principal="loom", route="/plugin/v1/route-advice", key=KEY,
                projection={"model": "private-model-sentinel"},
                operation=lambda: (200, {"safe": True}),
            )
            store.close()
            connection = sqlite3.connect(path)
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(idempotency_records)")
            }
            connection.close()
            self.assertNotIn("key", columns)
            self.assertNotIn("request_body", columns)
            self.assertNotIn(b"private-model-sentinel", path.read_bytes())

    def test_owner_exception_becomes_one_stable_terminal_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plugin.sqlite3"
            store = PluginStore(path, clock=lambda: NOW)
            calls = 0

            def fail() -> tuple[int, dict[str, object]]:
                nonlocal calls
                calls += 1
                raise RuntimeError("private failure")

            first = store.execute_idempotent(
                principal="loom", route="/plugin/v1/route-advice", key=KEY,
                projection={"safe": True}, operation=fail,
            )
            second = store.execute_idempotent(
                principal="loom", route="/plugin/v1/route-advice", key=KEY,
                projection={"safe": True}, operation=fail,
            )
            self.assertEqual(first, second)
            self.assertEqual(first[0], 500)
            self.assertEqual(first[1]["error"]["code"], "internal_error")
            self.assertEqual(calls, 1)
            self.assertNotIn("private failure", repr(first))
            store.close()

    def test_transaction_mutation_exception_is_terminal_and_not_reexecuted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = PluginStore(Path(temporary) / "plugin.sqlite3", clock=lambda: NOW)
            calls = 0

            def operation() -> tuple[int, dict[str, object], object]:
                nonlocal calls
                calls += 1
                def mutation(_connection: object) -> None:
                    raise RuntimeError("private mutation failure")
                return 200, {"unsafe": False}, mutation

            first = store.execute_idempotent(
                principal="loom", route="/plugin/v1/outcomes", key=KEY,
                projection={"safe": True}, operation=operation,
            )
            second = store.execute_idempotent(
                principal="loom", route="/plugin/v1/outcomes", key=KEY,
                projection={"safe": True}, operation=operation,
            )
            self.assertEqual(first, second)
            self.assertEqual(first[0], 500)
            self.assertEqual(calls, 1)
            self.assertNotIn("private mutation failure", repr(first))
            store.close()

    def test_dead_process_inflight_becomes_stable_503_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plugin.sqlite3"
            store = PluginStore(path, clock=lambda: NOW)
            key_hash = store._hash("loom", "/plugin/v1/route-advice", KEY)
            digest = store._digest("loom", "/plugin/v1/route-advice", {"safe": True})
            connection = sqlite3.connect(path)
            connection.execute(
                "INSERT INTO idempotency_records(principal,route,key_hash,request_digest,state,status_code,response_json,owner_pid,created_at,expires_at) VALUES(?,?,?,?, 'inflight',NULL,NULL,?,?,?)",
                ("loom", "/plugin/v1/route-advice", key_hash, digest, 2_147_483_647, "2026-08-10T04:05:06.000000Z", "2026-08-17T04:05:06.000000Z"),
            )
            connection.commit()
            connection.close()
            store.close()
            reopened = PluginStore(path, clock=lambda: NOW)
            calls = 0
            def forbidden() -> tuple[int, dict[str, object]]:
                nonlocal calls
                calls += 1
                return 200, {"bad": True}
            result = reopened.execute_idempotent(
                principal="loom", route="/plugin/v1/route-advice", key=KEY,
                projection={"safe": True}, operation=forbidden,
            )
            self.assertEqual(result[0], 503)
            self.assertEqual(result[1]["error"]["code"], "dependency_unavailable")
            self.assertEqual(calls, 0)
            reopened.close()

    def test_unknown_database_object_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plugin.sqlite3"
            store = PluginStore(path, clock=lambda: NOW)
            store.close()
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE hostile(secret TEXT)")
            connection.commit()
            connection.close()
            with self.assertRaises(RuntimeError):
                PluginStore(path, clock=lambda: NOW)


class PluginStoreFailClosedTests(unittest.TestCase):
    """Cover the plugin store validation and fail-closed branches."""

    def test_constructor_rejects_invalid_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid Plugin database path"):
            PluginStore("not-a-path")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plugin.sqlite3"
            with self.assertRaisesRegex(ValueError, "invalid waiter timeout"):
                PluginStore(path, waiter_timeout_seconds=0)
            with self.assertRaisesRegex(ValueError, "invalid Plugin capacity"):
                PluginStore(path, max_records_per_principal=0)

    def test_execute_idempotent_rejects_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = PluginStore(Path(temporary) / "plugin.sqlite3", clock=lambda: NOW)
            with self.assertRaisesRegex(ValueError, "invalid idempotency input"):
                store.execute_idempotent(
                    principal="unknown", route="/plugin/v1/route-advice", key=KEY,
                    projection={}, operation=lambda: (200, {}),
                )
            with self.assertRaisesRegex(ValueError, "invalid idempotency input"):
                store.execute_idempotent(
                    principal="loom", route="/other/v1", key=KEY,
                    projection={}, operation=lambda: (200, {}),
                )
            with self.assertRaisesRegex(ValueError, "invalid idempotency input"):
                store.execute_idempotent(
                    principal="loom", route="/plugin/v1/route-advice", key="bad",
                    projection={}, operation=lambda: (200, {}),
                )
            with self.assertRaisesRegex(ValueError, "invalid idempotency input"):
                store.execute_idempotent(
                    principal="loom", route="/plugin/v1/route-advice", key=KEY,
                    projection={}, operation="not-callable",
                )
            store.close()

    def test_execute_idempotent_rejects_invalid_operation_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = PluginStore(Path(temporary) / "plugin.sqlite3", clock=lambda: NOW)
            result = store.execute_idempotent(
                principal="loom", route="/plugin/v1/route-advice", key=KEY,
                projection={}, operation=lambda: ("not", "a", "tuple", "of", "2or3"),
            )
            self.assertEqual(result[0], 500)
            store.close()

    def test_decision_transaction_validation_branches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = PluginStore(Path(temporary) / "plugin.sqlite3", clock=lambda: NOW)
            with self.assertRaisesRegex(ValueError, "invalid Plugin transaction"):
                store.insert_decision_in_transaction(
                    object(), "loom", {"decisionId": "dec_0123456789abcdef0123456789abcdef"}
                )
            with self.assertRaisesRegex(ValueError, "invalid decision"):
                store.insert_decision_in_transaction(
                    sqlite3.connect(":memory:"), "loom", {"decisionId": "bad"}
                )
            with self.assertRaisesRegex(ValueError, "invalid Plugin transaction"):
                store.record_outcome_in_transaction(
                    object(), "loom", "dec_0123456789abcdef0123456789abcdef", {}
                )
            with self.assertRaises(DecisionNotFound):
                store.get_decision("unknown", "dec_0123456789abcdef0123456789abcdef")
            with self.assertRaises(DecisionNotFound):
                store.get_decision("loom", "bad")
            store.close()


if __name__ == "__main__":
    unittest.main()
