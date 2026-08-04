import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_runtime_observation import document, valid_observation


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def observation(
    index: int,
    *,
    completed_at: datetime = NOW,
    provider_id: str = "openai",
    model_id: str = "gpt-5",
    scope_ref: str = "anon_0123456789abcdef",
    status: str = "completed",
    total_tokens: int = 20,
    cost_micros: int | None = 25,
    cost_currency: str | None = "usd",
):
    from openusage_bar.runtime_observation import decode_runtime_document

    raw = valid_observation()
    raw["observationId"] = f"obs_{index:032x}"
    raw["providerId"] = provider_id
    raw["modelId"] = model_id
    raw["scopeRef"] = scope_ref
    raw["status"] = status
    raw["totalTokens"] = total_tokens
    raw["inputTokens"] = total_tokens - 8
    raw["outputTokens"] = 3
    raw["cacheReadTokens"] = 4
    raw["cacheCreationTokens"] = 0
    raw["reasoningTokens"] = 1
    raw["costMicros"] = cost_micros
    raw["costCurrency"] = cost_currency
    raw["completedAt"] = completed_at.isoformat().replace("+00:00", "Z")
    raw["startedAt"] = (completed_at - timedelta(seconds=3)).isoformat().replace(
        "+00:00", "Z"
    )
    raw["firstTokenAt"] = (completed_at - timedelta(seconds=2)).isoformat().replace(
        "+00:00", "Z"
    )
    return decode_runtime_document(document([raw])).observations[0]


class RuntimeStoreIngestionTests(unittest.TestCase):
    def test_ingest_is_idempotent_and_revision_changes_only_with_stored_facts(self):
        from openusage_bar.runtime_store import RuntimeStore

        store = RuntimeStore(":memory:", clock=lambda: NOW)
        try:
            first = store.ingest((observation(1),))
            retry = store.ingest((observation(1),))
            empty = store.ingest(())

            self.assertEqual(first.accepted_count, 1)
            self.assertEqual(first.duplicate_count, 0)
            self.assertEqual(first.revision, 1)
            self.assertEqual(retry.accepted_count, 0)
            self.assertEqual(retry.duplicate_count, 1)
            self.assertEqual(retry.revision, 1)
            self.assertEqual(empty.revision, 1)
            self.assertEqual(store.observation_count(), 1)
        finally:
            store.close()

    def test_same_id_with_different_payload_fails_closed(self):
        from openusage_bar.runtime_store import RuntimeStore, RuntimeStoreError

        store = RuntimeStore(":memory:", clock=lambda: NOW)
        try:
            store.ingest((observation(1),))
            with self.assertRaises(RuntimeStoreError):
                store.ingest((observation(1, model_id="gpt-5-mini"),))
            self.assertEqual(store.observation_count(), 1)
            self.assertEqual(store.revision(), 1)
        finally:
            store.close()

    def test_expired_and_future_observations_do_not_pollute_the_store(self):
        from openusage_bar.runtime_store import RuntimeStore, RuntimeStoreError

        store = RuntimeStore(":memory:", clock=lambda: NOW)
        try:
            result = store.ingest((
                observation(1, completed_at=NOW - timedelta(hours=24, microseconds=1)),
                observation(2),
            ))
            self.assertEqual(result.accepted_count, 1)
            self.assertEqual(result.expired_count, 1)
            self.assertEqual(store.observation_count(), 1)
            with self.assertRaises(RuntimeStoreError):
                store.ingest((observation(3, completed_at=NOW + timedelta(minutes=6)),))
            self.assertEqual(store.observation_count(), 1)
        finally:
            store.close()

    def test_retention_and_row_limit_prune_oldest_first(self):
        from openusage_bar.runtime_store import RuntimeStore

        clock = [NOW]
        store = RuntimeStore(":memory:", clock=lambda: clock[0], max_rows=2)
        try:
            result = store.ingest((
                observation(1, completed_at=NOW - timedelta(hours=2)),
                observation(2, completed_at=NOW - timedelta(hours=1)),
                observation(3, completed_at=NOW),
            ))
            self.assertEqual(result.accepted_count, 3)
            self.assertEqual(result.pruned_count, 1)
            self.assertEqual(store.observation_ids(), (
                "obs_00000000000000000000000000000002",
                "obs_00000000000000000000000000000003",
            ))

            clock[0] = NOW + timedelta(hours=24)
            pruned = store.prune()
            self.assertEqual(pruned, 1)
            self.assertEqual(store.observation_ids(), (
                "obs_00000000000000000000000000000003",
            ))
            self.assertEqual(store.revision(), 2)
        finally:
            store.close()


class RuntimeStoreSecurityTests(unittest.TestCase):
    def test_file_is_private_bounded_and_separate_from_activity_ledger(self):
        from openusage_bar.runtime_store import MAX_DATABASE_BYTES, RuntimeStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_path = root / "runtime.sqlite3"
            activity_path = root / "activity.sqlite3"
            store = RuntimeStore(runtime_path, clock=lambda: NOW)
            try:
                self.assertEqual(runtime_path.stat().st_mode & 0o777, 0o600)
                self.assertGreater(store.page_limit_bytes, 0)
                self.assertLessEqual(store.page_limit_bytes, MAX_DATABASE_BYTES)
                self.assertFalse(activity_path.exists())
            finally:
                store.close()

    def test_rejects_symlink_and_incompatible_or_newer_schema(self):
        from openusage_bar.runtime_store import RuntimeStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.sqlite3"
            target.touch()
            symlink = root / "runtime.sqlite3"
            symlink.symlink_to(target)
            with self.assertRaises(RuntimeError):
                RuntimeStore(symlink)

            incompatible = root / "incompatible.sqlite3"
            connection = sqlite3.connect(incompatible)
            connection.execute("CREATE TABLE runtime_meta (wrong TEXT)")
            connection.commit()
            connection.close()
            with self.assertRaises(RuntimeError):
                RuntimeStore(incompatible)

            newer = root / "newer.sqlite3"
            connection = sqlite3.connect(newer)
            connection.execute("PRAGMA user_version=2")
            connection.commit()
            connection.close()
            with self.assertRaises(RuntimeError):
                RuntimeStore(newer)

    def test_read_only_summary_preserves_database_bytes_metadata_and_sidecars(self):
        from openusage_bar.runtime_store import RuntimeStore, read_runtime_summary

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            writer = RuntimeStore(path, clock=lambda: NOW)
            try:
                writer.ingest((observation(1),))
            finally:
                writer.close()
            before_bytes = path.read_bytes()
            before = path.stat()

            summary = read_runtime_summary(
                path, NOW - timedelta(hours=1), NOW, clock=lambda: NOW
            )

            after = path.stat()
            self.assertEqual(summary.runtime_revision, 1)
            self.assertEqual(summary.total_tokens, 20)
            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
            self.assertEqual(after.st_mode, before.st_mode)
            self.assertFalse(Path(str(path) + "-wal").exists())
            self.assertFalse(Path(str(path) + "-shm").exists())

    def test_read_only_summary_rejects_missing_symlink_and_incompatible_database(self):
        from openusage_bar.runtime_store import read_runtime_summary

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing.sqlite3"
            with self.assertRaises(RuntimeError):
                read_runtime_summary(
                    missing, NOW - timedelta(hours=1), NOW, clock=lambda: NOW
                )

            target = root / "target.sqlite3"
            target.touch()
            symlink = root / "runtime.sqlite3"
            symlink.symlink_to(target)
            with self.assertRaises(RuntimeError):
                read_runtime_summary(
                    symlink, NOW - timedelta(hours=1), NOW, clock=lambda: NOW
                )

            incompatible = root / "incompatible.sqlite3"
            connection = sqlite3.connect(incompatible)
            connection.execute("CREATE TABLE private_payload (prompt TEXT)")
            connection.commit()
            connection.close()
            with self.assertRaises(RuntimeError):
                read_runtime_summary(
                    incompatible, NOW - timedelta(hours=1), NOW, clock=lambda: NOW
                )


class RuntimeStoreSummaryTests(unittest.TestCase):
    def test_summary_reports_bounded_groups_tokens_status_cost_and_latency(self):
        from openusage_bar.runtime_store import RuntimeStore

        store = RuntimeStore(":memory:", clock=lambda: NOW)
        try:
            store.ingest((
                observation(1, provider_id="openai", model_id="gpt-5"),
                observation(
                    2,
                    provider_id="minimax",
                    model_id="minimax-m2",
                    scope_ref="anon_abcdef0123456789",
                    status="error",
                    cost_micros=None,
                    cost_currency=None,
                ),
            ))
            summary = store.summary(NOW - timedelta(hours=1), NOW)

            self.assertEqual(summary.schema_version, 1)
            self.assertEqual(summary.runtime_revision, 1)
            self.assertEqual(summary.coverage_state, "complete")
            self.assertEqual(summary.observation_count, 2)
            self.assertEqual(summary.total_tokens, 40)
            self.assertEqual(dict(summary.status_counts), {"completed": 1, "error": 1})
            self.assertEqual(
                [(row.currency, row.cost_micros) for row in summary.costs],
                [("usd", 25)],
            )
            self.assertEqual(summary.latency.duration_sample_count, 2)
            self.assertEqual(summary.latency.duration_avg_ms, 3000)
            self.assertEqual(summary.latency.duration_p95_ms, 3000)
            self.assertEqual(summary.latency.ttft_sample_count, 2)
            self.assertEqual(len(summary.groups), 2)
            self.assertEqual(
                [(row.provider_id, row.model_id) for row in summary.groups],
                [("minimax", "minimax-m2"), ("openai", "gpt-5")],
            )
        finally:
            store.close()

    def test_complete_empty_and_partial_group_coverage_are_explicit(self):
        from openusage_bar.runtime_store import RuntimeStore

        store = RuntimeStore(":memory:", clock=lambda: NOW, max_groups=1)
        try:
            empty = store.summary(NOW - timedelta(hours=1), NOW)
            self.assertEqual(empty.coverage_state, "complete")
            self.assertEqual(empty.observation_count, 0)
            self.assertEqual(empty.groups, ())

            store.ingest((
                observation(1, provider_id="openai", model_id="gpt-5"),
                observation(2, provider_id="minimax", model_id="minimax-m2"),
            ))
            partial = store.summary(NOW - timedelta(hours=1), NOW)
            self.assertEqual(partial.coverage_state, "partial")
            self.assertEqual(partial.observation_count, 2)
            self.assertEqual(len(partial.groups), 1)
            self.assertEqual(partial.omitted_group_count, 1)
        finally:
            store.close()

    def test_summary_rejects_invalid_or_oversized_windows(self):
        from openusage_bar.runtime_store import RuntimeStore

        store = RuntimeStore(":memory:", clock=lambda: NOW)
        try:
            for start, end in (
                (NOW, NOW),
                (NOW, NOW - timedelta(seconds=1)),
                (NOW - timedelta(hours=24, microseconds=1), NOW),
                (NOW.replace(tzinfo=None), NOW),
            ):
                with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                    store.summary(start, end)
        finally:
            store.close()
