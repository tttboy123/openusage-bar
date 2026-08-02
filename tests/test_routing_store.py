import dataclasses
import inspect
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def decision(index: int, *, generated_at: datetime = NOW, selected_suffix: str = "gpt-5"):
    from openusage_bar.routing_contract import (
        RejectedTarget,
        RouteDecision,
        ScoreComponents,
        ScoredTarget,
    )

    components = ScoreComponents(9_000, 8_000, 7_000, 6_000)
    return RouteDecision(
        generated_at=timestamp(generated_at),
        expires_at=timestamp(generated_at + timedelta(seconds=30)),
        policy_id="reliable",
        policy_revision=3,
        data_revision=60_000 + index,
        runtime_revision=120 + index,
        selected=ScoredTarget(
            target_id=f"openai.work.{selected_suffix}",
            provider_id="openai",
            account_ref="private_account_123",
            model_id=selected_suffix,
            score=8_500,
            components=components,
            reasons=("healthy_source", "quota_headroom"),
        ),
        alternatives=(
            ScoredTarget(
                target_id="openai.work.gpt-5-mini",
                provider_id="openai",
                account_ref="private_account_123",
                model_id="gpt-5-mini",
                score=7_500,
                components=components,
                reasons=("healthy_source",),
            ),
        ),
        rejected=(
            RejectedTarget("minimax.work.m2", ("fact_stale",)),
        ),
    )


def evidence(index: int, *, generated_at: datetime = NOW, selected_suffix: str = "gpt-5"):
    from openusage_bar.routing_store import DecisionEvidence

    return DecisionEvidence(
        decision_id=f"route_{index:032x}",
        client_request_ref=f"req_{index:032x}",
        session_ref=f"anon_{index:032x}",
        decision=decision(index, generated_at=generated_at, selected_suffix=selected_suffix),
    )


def attempt(
    index: int,
    decision_index: int,
    *,
    completed_at: datetime = NOW,
    ordinal: int = 1,
):
    from openusage_bar.routing_store import ExecutionAttemptEvidence

    return ExecutionAttemptEvidence(
        attempt_id=f"attempt_{index:032x}",
        decision_id=f"route_{decision_index:032x}",
        target_id="openai.work.gpt-5",
        ordinal=ordinal,
        started_at=timestamp(completed_at - timedelta(seconds=2)),
        completed_at=timestamp(completed_at),
        outcome="succeeded",
        status_class="success",
        reason_code="provider_completed",
        input_tokens=12,
        output_tokens=3,
        cache_read_tokens=4,
        cache_creation_tokens=0,
        reasoning_tokens=1,
        total_tokens=20,
        cost_micros=25,
        cost_currency="USD",
    )


class RoutingStoreContractTests(unittest.TestCase):
    def test_evidence_values_reject_identity_content_and_unbounded_attempts(self):
        from openusage_bar.routing_store import DecisionEvidence, ExecutionAttemptEvidence

        invalid = (
            lambda: dataclasses.replace(evidence(1), decision_id="route_customer@example.com"),
            lambda: dataclasses.replace(evidence(1), client_request_ref="customer@example.com"),
            lambda: dataclasses.replace(evidence(1), session_ref="/Users/customer/session"),
            lambda: dataclasses.replace(attempt(1, 1), attempt_id="attempt_customer@example.com"),
            lambda: dataclasses.replace(attempt(1, 1), ordinal=4),
            lambda: dataclasses.replace(attempt(1, 1), reason_code="provider said: secret body"),
            lambda: dataclasses.replace(attempt(1, 1), cost_currency=None),
        )
        for case in invalid:
            with self.subTest(case=case), self.assertRaises(ValueError):
                case()

        self.assertNotIn("payload", inspect.signature(DecisionEvidence).parameters)
        self.assertNotIn("error_body", inspect.signature(ExecutionAttemptEvidence).parameters)
        self.assertNotIn("endpoint", inspect.signature(ExecutionAttemptEvidence).parameters)


class RoutingStorePersistenceTests(unittest.TestCase):
    def test_round_trip_is_canonical_private_bounded_and_content_free(self):
        from openusage_bar.routing_store import MAX_DATABASE_BYTES, RoutingStore

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routing.sqlite3"
            store = RoutingStore(path, clock=lambda: NOW)
            try:
                result = store.record_decision(evidence(1))
                stored = store.get_decision("route_00000000000000000000000000000001")

                self.assertTrue(result.stored)
                self.assertFalse(result.duplicate)
                self.assertEqual(result.revision, 1)
                self.assertIsNotNone(stored)
                self.assertEqual(stored.selected_target_id, "openai.work.gpt-5")
                self.assertEqual(stored.selected_score, 8_500)
                self.assertEqual(stored.alternatives[0].target_id, "openai.work.gpt-5-mini")
                self.assertEqual(stored.rejected[0].reason_codes, ("fact_stale",))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertGreater(store.page_limit_bytes, 0)
                self.assertLessEqual(store.page_limit_bytes, MAX_DATABASE_BYTES)
            finally:
                store.close()

            connection = sqlite3.connect(path)
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                self.assertEqual(
                    tables,
                    {"routing_meta", "routing_decisions", "routing_attempts"},
                )
                candidates_json = connection.execute(
                    "SELECT candidates_json FROM routing_decisions"
                ).fetchone()[0]
                all_columns = " ".join(
                    row[1]
                    for table in tables
                    for row in connection.execute(f"PRAGMA table_info({table})")
                )
            finally:
                connection.close()

            for forbidden in (
                "prompt", "response", "message", "tool", "header", "credential",
                "payload", "endpoint", "url", "account_ref", "provider_id", "model_id",
            ):
                self.assertNotIn(forbidden, all_columns.lower())
            self.assertNotIn("private_account_123", candidates_json)

    def test_idempotency_mismatch_and_foreign_key_fail_closed(self):
        from openusage_bar.routing_store import RoutingStore, RoutingStoreError

        store = RoutingStore(":memory:", clock=lambda: NOW)
        try:
            first = store.record_decision(evidence(1))
            duplicate = store.record_decision(evidence(1))
            self.assertEqual(first.revision, 1)
            self.assertTrue(duplicate.stored)
            self.assertTrue(duplicate.duplicate)
            self.assertEqual(duplicate.revision, 1)

            with self.assertRaises(RoutingStoreError):
                store.record_decision(evidence(1, selected_suffix="gpt-5-pro"))
            with self.assertRaises(RoutingStoreError):
                store.record_attempt(attempt(1, 999))
            self.assertEqual(store.decision_count(), 1)
            self.assertEqual(store.attempt_count(), 0)
        finally:
            store.close()

    def test_attempt_round_trip_and_row_caps_prune_oldest_deterministically(self):
        from openusage_bar.routing_store import RoutingStore

        store = RoutingStore(
            ":memory:", clock=lambda: NOW, max_decisions=2, max_attempts=2
        )
        try:
            for index, age in ((1, 2), (2, 1), (3, 0)):
                store.record_decision(
                    evidence(index, generated_at=NOW - timedelta(hours=age))
                )
            self.assertEqual(
                tuple(row.decision_id for row in store.list_decisions(limit=10)),
                (
                    "route_00000000000000000000000000000003",
                    "route_00000000000000000000000000000002",
                ),
            )

            for index, decision_index, age, ordinal in (
                (1, 2, 2, 1),
                (2, 2, 1, 2),
                (3, 3, 0, 1),
            ):
                store.record_attempt(
                    attempt(
                        index,
                        decision_index,
                        completed_at=NOW - timedelta(minutes=age),
                        ordinal=ordinal,
                    )
                )
            self.assertEqual(store.attempt_count(), 2)
            self.assertEqual(
                tuple(row.attempt_id for row in store.attempts_for_decision(
                    "route_00000000000000000000000000000002"
                )),
                ("attempt_00000000000000000000000000000002",),
            )
        finally:
            store.close()

    def test_retention_pagination_and_explicit_prune(self):
        from openusage_bar.routing_store import RoutingStore

        clock = [NOW]
        store = RoutingStore(":memory:", clock=lambda: clock[0], max_decisions=5)
        try:
            for index, age in ((1, 2), (2, 1), (3, 0)):
                store.record_decision(
                    evidence(index, generated_at=NOW - timedelta(hours=age))
                )
            page = store.list_decisions(
                before="route_00000000000000000000000000000003",
                limit=1,
            )
            self.assertEqual(
                tuple(row.decision_id for row in page),
                ("route_00000000000000000000000000000002",),
            )

            clock[0] = NOW + timedelta(days=8)
            result = store.prune()
            self.assertEqual(result.pruned_decisions, 3)
            self.assertEqual(store.decision_count(), 0)
        finally:
            store.close()

    def test_row_cap_uses_insertion_order_when_timestamps_tie(self):
        from openusage_bar.routing_store import RoutingStore

        store = RoutingStore(":memory:", clock=lambda: NOW, max_decisions=2)
        try:
            for index in (3, 2, 1):
                store.record_decision(evidence(index))
            self.assertEqual(
                {row.decision_id for row in store.list_decisions(limit=10)},
                {
                    "route_00000000000000000000000000000001",
                    "route_00000000000000000000000000000002",
                },
            )
        finally:
            store.close()

    def test_logging_failure_is_non_authoritative(self):
        from openusage_bar.routing_store import try_record_decision

        selected = decision(1)

        class BrokenStore:
            def record_decision(self, _value):
                raise sqlite3.OperationalError("database is full: /private/path")

        self.assertFalse(try_record_decision(BrokenStore(), evidence(1)))
        self.assertEqual(selected.selected.target_id, "openai.work.gpt-5")


class RoutingStoreSecurityTests(unittest.TestCase):
    def test_rejects_relative_symlink_nonprivate_incompatible_newer_and_corrupt_paths(self):
        from openusage_bar.routing_store import RoutingStore

        with self.assertRaises(RuntimeError):
            RoutingStore("relative.sqlite3")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.sqlite3"
            target.touch(mode=0o600)
            symlink = root / "routing.sqlite3"
            symlink.symlink_to(target)
            with self.assertRaises(RuntimeError):
                RoutingStore(symlink)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, 0o755)
            with self.assertRaises(RuntimeError):
                RoutingStore(root / "routing.sqlite3")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nonprivate = root / "routing.sqlite3"
            nonprivate.touch(mode=0o644)
            with self.assertRaises(RuntimeError):
                RoutingStore(nonprivate)

            for name, setup in (
                ("incompatible.sqlite3", "CREATE TABLE private_payload (prompt TEXT)"),
                ("newer.sqlite3", "PRAGMA user_version=2"),
            ):
                path = root / name
                connection = sqlite3.connect(path)
                connection.execute(setup)
                connection.commit()
                connection.close()
                os.chmod(path, 0o600)
                with self.assertRaises(RuntimeError):
                    RoutingStore(path)

            corrupt = root / "corrupt.sqlite3"
            corrupt.write_bytes(b"not a sqlite database")
            os.chmod(corrupt, 0o600)
            with self.assertRaises(RuntimeError):
                RoutingStore(corrupt)

            partial = root / "partial.sqlite3"
            connection = sqlite3.connect(partial)
            connection.execute(
                "CREATE TABLE routing_meta (key TEXT PRIMARY KEY,value INTEGER NOT NULL)"
            )
            connection.execute("INSERT INTO routing_meta VALUES('revision',0)")
            connection.commit()
            connection.close()
            os.chmod(partial, 0o600)
            with self.assertRaises(RuntimeError):
                RoutingStore(partial)

    def test_rejects_counterfeit_constraints_and_meta_rows(self):
        from openusage_bar.routing_store import RoutingStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "routing.sqlite3"
            store = RoutingStore(path, clock=lambda: NOW)
            store.close()

            connection = sqlite3.connect(path)
            connection.execute("INSERT INTO routing_meta VALUES('unexpected',1)")
            connection.commit()
            connection.close()
            with self.assertRaises(RuntimeError):
                RoutingStore(path)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "routing.sqlite3"
            store = RoutingStore(path, clock=lambda: NOW)
            store.close()
            original = sqlite3.connect(path)
            decisions_sql = original.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='routing_decisions'"
            ).fetchone()[0]
            attempts_columns = original.execute(
                "PRAGMA table_info(routing_attempts)"
            ).fetchall()
            original.close()
            path.unlink()

            counterfeit = sqlite3.connect(path)
            counterfeit.execute("CREATE TABLE routing_meta (key TEXT PRIMARY KEY,value INTEGER NOT NULL)")
            counterfeit.execute(decisions_sql)
            counterfeit.execute(
                "CREATE TABLE routing_attempts (" + ",".join(
                    f"{row[1]} {row[2]}" + (" NOT NULL" if row[3] else "")
                    + (" PRIMARY KEY" if row[5] else "")
                    for row in attempts_columns
                ) + ")"
            )
            counterfeit.execute(
                "CREATE INDEX routing_decisions_created ON routing_decisions(created_at,decision_id)"
            )
            counterfeit.execute(
                "CREATE INDEX routing_attempts_completed ON routing_attempts(completed_at,attempt_id)"
            )
            counterfeit.execute(
                "CREATE INDEX routing_attempts_decision ON routing_attempts(decision_id,ordinal)"
            )
            counterfeit.execute("INSERT INTO routing_meta VALUES('revision',0)")
            counterfeit.execute("PRAGMA user_version=1")
            counterfeit.commit()
            counterfeit.close()
            os.chmod(path, 0o600)
            with self.assertRaises(RuntimeError):
                RoutingStore(path)

    def test_tampered_candidates_and_attempt_rows_fail_closed(self):
        from openusage_bar.routing_store import (
            RoutingStore,
            RoutingStoreError,
            _candidates_json,
        )

        store = RoutingStore(":memory:", clock=lambda: NOW)
        try:
            store.record_decision(evidence(1))
            store.record_attempt(attempt(1, 1))
            connection = store._connection
            self.assertIsNotNone(connection)
            connection.execute(
                "UPDATE routing_decisions SET candidates_json=?",
                ('{"selected":null,"selected":{},"alternatives":[],"rejected":[]}',),
            )
            connection.commit()
            with self.assertRaises(RoutingStoreError):
                store.get_decision("route_00000000000000000000000000000001")

            connection.execute(
                "UPDATE routing_decisions SET candidates_json=?",
                (_candidates_json(evidence(1).decision),),
            )
            connection.execute("UPDATE routing_attempts SET ordinal=4")
            connection.commit()
            with self.assertRaises(RoutingStoreError):
                store.attempts_for_decision(
                    "route_00000000000000000000000000000001"
                )
        finally:
            store.close()
