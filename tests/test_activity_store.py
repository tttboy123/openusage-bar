from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from openusage_bar.activity_store import (
    ActivityStore,
    DailyCostRow,
    DailyUsageRow,
    ProviderInstance,
    QuotaObservation,
)


IMPORTED_AT = "2026-07-14T02:00:00Z"


def usage(
    *,
    day: str = "2026-07-02",
    provider_id: str = "codex",
    account_ref: str = "",
    model_id: str = "gpt-5.5",
    total_tokens: int = 74_200_000,
) -> DailyUsageRow:
    return DailyUsageRow(
        day=day,
        provider_id=provider_id,
        account_ref=account_ref,
        model_id=model_id,
        input_tokens=40_000_000,
        output_tokens=4_200_000,
        cache_read_tokens=30_000_000,
        cache_creation_tokens=0,
        reasoning_tokens=None,
        total_tokens=total_tokens,
        cost_amount="4.237100",
        cost_currency="USD",
        cost_basis="price_table_estimated",
        quality="derived",
        imported_at=IMPORTED_AT,
    )


def cost(
    *,
    day: str = "2026-07-02",
    provider_id: str = "openai",
    account_ref: str = "",
    currency: str = "USD",
    cost_kind: str = "actual",
    amount: str = "12.3400",
) -> DailyCostRow:
    return DailyCostRow(
        day=day,
        provider_id=provider_id,
        account_ref=account_ref,
        cost_kind=cost_kind,
        currency=currency,
        amount=amount,
        basis="provider_reported",
        quality="direct",
        imported_at=IMPORTED_AT,
    )


def quota(
    *,
    observed_at: str = "2026-07-14T02:00:00Z",
    remaining: str = "18",
) -> QuotaObservation:
    return QuotaObservation(
        record_id="minimax.five_hour",
        observed_at=observed_at,
        provider_id="minimax",
        account_ref="primary",
        quota_name="Five hour",
        unit="percent",
        used="82.0",
        quota_limit="100",
        remaining=remaining,
        remaining_ratio=0.18,
        resets_at="2026-07-14T04:00:00Z",
        period_start="2026-07-13T23:00:00Z",
        period_end="2026-07-14T04:00:00Z",
        state="ok",
        quality="direct",
        stale=False,
    )


def provider_instance(
    *,
    provider_id: str = "codex",
    family_id: str = "codex",
    display_name: str = "Codex",
    category: str = "subscription",
    credential_source: str = "openusage",
    source_kind: str = "openusage",
    observed_at: str = IMPORTED_AT,
) -> ProviderInstance:
    return ProviderInstance(
        provider_id=provider_id,
        family_id=family_id,
        display_name=display_name,
        category=category,
        credential_source=credential_source,
        source_kind=source_kind,
        observed_at=observed_at,
    )


class ActivityStoreSchemaTests(unittest.TestCase):
    def test_schema_v4_is_idempotent_and_has_required_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "activity.sqlite3"
            with ActivityStore(path) as store:
                self.assertEqual(store.schema_version, 4)
                self.assertEqual(
                    store.table_names(),
                    {
                        "change_log",
                        "daily_cost_coverage",
                        "daily_costs",
                        "daily_coverage",
                        "daily_model_usage",
                        "ledger_meta",
                        "provider_instances",
                        "quota_snapshots",
                        "quota_state",
                        "source_status",
                    },
                )
                inspector = sqlite3.connect(path)
                try:
                    columns = {
                        row[1]: bool(row[3])
                        for row in inspector.execute(
                            "PRAGMA table_info(daily_model_usage)"
                        )
                    }
                finally:
                    inspector.close()
                self.assertTrue(columns["day"])
                self.assertTrue(columns["provider_id"])
                self.assertTrue(columns["model_id"])
                self.assertTrue(columns["source_id"])
                self.assertFalse(columns["reasoning_tokens"])
                index_inspector = sqlite3.connect(path)
                try:
                    indexes = list(
                        index_inspector.execute("PRAGMA index_list(change_log)")
                    )
                finally:
                    index_inspector.close()
                self.assertTrue(
                    any(
                        row[1] == "change_log_record_revision_unique" and row[2] == 1
                        for row in indexes
                    )
                )
            with ActivityStore(path) as reopened:
                self.assertEqual(reopened.schema_version, 4)

    def test_daily_cost_schema_contains_no_private_upstream_identity_fields(self):
        with ActivityStore(":memory:") as store:
            columns = {
                row[1]
                for row in store._connection.execute("PRAGMA table_info(daily_costs)")
            }

        self.assertEqual(
            columns,
            {
                "day",
                "provider_id",
                "account_ref",
                "cost_kind",
                "currency",
                "amount",
                "basis",
                "quality",
                "imported_at",
                "revision",
                "payload_hash",
            },
        )
        forbidden = {
            "api_key_id", "project_id", "organization_id", "email", "username",
            "token", "secret", "body", "raw_response",
        }
        self.assertFalse(columns & forbidden)

    def test_provider_instance_schema_contains_no_private_identity_or_raw_payload_fields(self):
        with ActivityStore(":memory:") as store:
            columns = {
                row[1]
                for row in store._connection.execute(
                    "PRAGMA table_info(provider_instances)"
                )
            }

        self.assertEqual(
            columns,
            {
                "provider_id",
                "family_id",
                "display_name",
                "category",
                "credential_source",
                "source_kind",
                "observed_at",
                "revision",
                "payload_hash",
            },
        )
        forbidden = {"email", "username", "account", "path", "token", "cookie", "attributes", "body"}
        self.assertFalse(columns & forbidden)

    def test_existing_v1_ledger_migrates_additively_without_cost_backfill(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "activity.sqlite3"
            ActivityStore(path).close()
            connection = sqlite3.connect(path)
            before_cursor = connection.execute(
                "SELECT COALESCE(MAX(change_seq),0) FROM change_log"
            ).fetchone()[0]
            connection.execute("DROP TABLE daily_costs")
            connection.execute("DROP TABLE daily_cost_coverage")
            connection.execute("PRAGMA user_version=1")
            connection.commit()
            connection.close()

            with ActivityStore(path) as reopened:
                self.assertEqual(reopened.schema_version, 4)
                self.assertEqual(reopened.daily_costs("2020-01-01", "2030-01-01"), [])
                changes = reopened.changes(before_cursor)
                self.assertEqual([row.record_type for row in changes], ["ledger_schema"])
                self.assertIn("daily_costs", reopened.table_names())
                self.assertIn("daily_cost_coverage", reopened.table_names())

    def test_existing_v2_usage_and_coverage_migrate_to_legacy_source_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "activity.sqlite3"
            with ActivityStore(path) as store:
                store.replace_daily_usage("codex", "2026-07-02", [usage()])
            connection = sqlite3.connect(path)
            before_cursor = connection.execute(
                "SELECT COALESCE(MAX(change_seq),0) FROM change_log"
            ).fetchone()[0]
            connection.execute("ALTER TABLE daily_model_usage RENAME TO usage_v3")
            connection.execute(
                "CREATE TABLE daily_model_usage("
                "day TEXT NOT NULL,provider_id TEXT NOT NULL,account_ref TEXT NOT NULL DEFAULT '',"
                "model_id TEXT NOT NULL,input_tokens INTEGER NOT NULL,output_tokens INTEGER NOT NULL,"
                "cache_read_tokens INTEGER NOT NULL,cache_creation_tokens INTEGER NOT NULL,"
                "reasoning_tokens INTEGER,total_tokens INTEGER NOT NULL,cost_amount TEXT,"
                "cost_currency TEXT,cost_basis TEXT,quality TEXT NOT NULL,imported_at TEXT NOT NULL,"
                "revision INTEGER NOT NULL,payload_hash TEXT NOT NULL,"
                "PRIMARY KEY(day,provider_id,account_ref,model_id))"
            )
            connection.execute(
                "INSERT INTO daily_model_usage SELECT day,provider_id,account_ref,model_id,"
                "input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,reasoning_tokens,"
                "total_tokens,cost_amount,cost_currency,cost_basis,quality,imported_at,revision,payload_hash "
                "FROM usage_v3"
            )
            connection.execute("DROP TABLE usage_v3")
            connection.execute("ALTER TABLE daily_coverage RENAME TO coverage_v3")
            connection.execute(
                "CREATE TABLE daily_coverage("
                "day TEXT NOT NULL,provider_id TEXT NOT NULL,account_ref TEXT NOT NULL DEFAULT '',"
                "imported_at TEXT NOT NULL,PRIMARY KEY(day,provider_id,account_ref))"
            )
            connection.execute(
                "INSERT INTO daily_coverage SELECT day,provider_id,account_ref,imported_at FROM coverage_v3"
            )
            connection.execute("DROP TABLE coverage_v3")
            connection.execute("PRAGMA user_version=2")
            connection.commit()
            connection.close()

            with ActivityStore(path) as reopened:
                self.assertEqual(reopened.schema_version, 4)
                snapshot = reopened.snapshot_daily_usage("2026-07-02", "2026-07-02")
                self.assertEqual(snapshot.rows[0].source_id, "legacy")
                self.assertEqual(
                    snapshot.coverage_sources,
                    frozenset({("2026-07-02", "codex", "", "legacy")}),
                )
                self.assertEqual(
                    [row.record_type for row in reopened.changes(before_cursor)],
                    ["ledger_schema"],
                )

    def test_v3_source_health_migrates_without_row_loss_and_advances_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "activity.sqlite3"
            with ActivityStore(path) as store:
                store.record_source_failure(
                    "cursor", "current.quota", "timeout",
                    datetime.fromisoformat("2026-07-14T02:00:00+00:00"),
                )
            connection = sqlite3.connect(path)
            connection.execute("ALTER TABLE source_status RENAME TO source_status_v4")
            connection.execute(
                "CREATE TABLE source_status("
                "provider_id TEXT NOT NULL,source_id TEXT NOT NULL,state TEXT NOT NULL,"
                "last_attempt_at TEXT NOT NULL,last_success_at TEXT,stale_at TEXT,error_code TEXT,"
                "PRIMARY KEY(provider_id,source_id))"
            )
            connection.execute(
                "INSERT INTO source_status SELECT provider_id,source_id,state,last_attempt_at,"
                "last_success_at,stale_at,error_code FROM source_status_v4"
            )
            connection.execute("DROP TABLE source_status_v4")
            before_cursor = connection.execute(
                "SELECT COALESCE(MAX(change_seq),0) FROM change_log"
            ).fetchone()[0]
            connection.execute("PRAGMA user_version=3")
            connection.commit()
            connection.close()

            with ActivityStore(path) as reopened:
                status = reopened.source_statuses()[0]
                self.assertEqual((status.provider_id, status.error_code), ("cursor", "timeout"))
                self.assertEqual(status.revision, 1)
                self.assertEqual(len(status.payload_hash), 64)
                changes = reopened.changes(before_cursor)
                self.assertEqual([row.record_type for row in changes], ["ledger_schema"])
                self.assertGreater(reopened.current_change_seq, before_cursor)

    def test_rejects_database_from_newer_schema_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "future.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA user_version=5")
            connection.close()
            with self.assertRaisesRegex(RuntimeError, "newer schema version"):
                ActivityStore(path)

    def test_incompatible_version_zero_table_is_not_silently_stamped_v1(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE daily_model_usage(unrelated TEXT)")
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(RuntimeError, "incompatible schema"):
                ActivityStore(path)

            inspector = sqlite3.connect(path)
            try:
                self.assertEqual(inspector.execute("PRAGMA user_version").fetchone()[0], 0)
                columns = [
                    row[1]
                    for row in inspector.execute("PRAGMA table_info(daily_model_usage)")
                ]
                self.assertEqual(columns, ["unrelated"])
            finally:
                inspector.close()

    def test_full_schema_signature_is_required_before_version_zero_stamp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "weak-constraints.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE daily_model_usage("
                "day TEXT,provider_id TEXT,account_ref TEXT,model_id TEXT,"
                "input_tokens INTEGER,output_tokens INTEGER,cache_read_tokens INTEGER,"
                "cache_creation_tokens INTEGER,reasoning_tokens INTEGER,total_tokens INTEGER,"
                "cost_amount TEXT,cost_currency TEXT,cost_basis TEXT,quality TEXT,"
                "imported_at TEXT,revision INTEGER,payload_hash TEXT)"
            )
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(RuntimeError, "incompatible schema"):
                ActivityStore(path)

            inspector = sqlite3.connect(path)
            try:
                self.assertEqual(inspector.execute("PRAGMA user_version").fetchone()[0], 0)
                account_ref = next(
                    row
                    for row in inspector.execute(
                        "PRAGMA table_info(daily_model_usage)"
                    )
                    if row[1] == "account_ref"
                )
                self.assertEqual((account_ref[3], account_ref[4], account_ref[5]), (0, None, 0))
            finally:
                inspector.close()

    def test_version_one_table_with_wrong_types_and_pk_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "malformed-v1.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE source_status("
                "provider_id INTEGER,source_id TEXT,state TEXT,last_attempt_at TEXT,"
                "last_success_at TEXT,stale_at TEXT,error_code TEXT)"
            )
            connection.execute("PRAGMA user_version=1")
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(RuntimeError, "incompatible schema"):
                ActivityStore(path)

    def test_version_zero_quota_index_with_ascending_time_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ascending-index.sqlite3"
            ActivityStore(path).close()
            connection = sqlite3.connect(path)
            connection.execute("DROP INDEX quota_snapshot_record_time")
            connection.execute(
                "CREATE INDEX quota_snapshot_record_time "
                "ON quota_snapshots(record_id ASC, observed_at ASC)"
            )
            connection.execute("PRAGMA user_version=0")
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(RuntimeError, "incompatible schema"):
                ActivityStore(path)

            inspector = sqlite3.connect(path)
            try:
                self.assertEqual(inspector.execute("PRAGMA user_version").fetchone()[0], 0)
            finally:
                inspector.close()

    def test_version_zero_integer_primary_keys_require_autoincrement(self):
        definitions = {
            "quota_snapshots": (
                "snapshot_id INTEGER PRIMARY KEY,record_id TEXT NOT NULL,"
                "observed_at TEXT NOT NULL,provider_id TEXT NOT NULL,"
                "account_ref TEXT NOT NULL DEFAULT '',quota_name TEXT NOT NULL,"
                "payload_json TEXT NOT NULL,payload_hash TEXT NOT NULL",
                "DROP INDEX quota_snapshot_record_time",
                "CREATE INDEX quota_snapshot_record_time "
                "ON quota_snapshots(record_id,observed_at DESC)",
            ),
            "change_log": (
                "change_seq INTEGER PRIMARY KEY,record_type TEXT NOT NULL,"
                "record_id TEXT NOT NULL,revision INTEGER NOT NULL,"
                "operation TEXT NOT NULL,changed_at TEXT NOT NULL,"
                "payload_json TEXT,payload_hash TEXT NOT NULL",
                "DROP INDEX change_log_record_revision_unique",
                "CREATE UNIQUE INDEX change_log_record_revision_unique "
                "ON change_log(record_type,record_id,revision)",
            ),
        }
        for table, (columns, drop_index, create_index) in definitions.items():
            with self.subTest(table=table), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / f"{table}.sqlite3"
                ActivityStore(path).close()
                connection = sqlite3.connect(path)
                connection.execute(drop_index)
                connection.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
                connection.execute(f"CREATE TABLE {table}({columns})")
                connection.execute(f"DROP TABLE {table}_old")
                connection.execute(create_index)
                connection.execute("PRAGMA user_version=0")
                connection.commit()
                connection.close()

                with self.assertRaisesRegex(RuntimeError, "incompatible schema"):
                    ActivityStore(path)

                inspector = sqlite3.connect(path)
                try:
                    self.assertEqual(
                        inspector.execute("PRAGMA user_version").fetchone()[0], 0
                    )
                finally:
                    inspector.close()

    def test_legacy_duplicate_change_revisions_fail_clearly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE change_log("
                "change_seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                "record_type TEXT NOT NULL,record_id TEXT NOT NULL,"
                "revision INTEGER NOT NULL,operation TEXT NOT NULL,"
                "changed_at TEXT NOT NULL,payload_json TEXT,payload_hash TEXT NOT NULL)"
            )
            values = ("daily_usage", "daily:one", 1, "insert", IMPORTED_AT, None, "hash")
            connection.execute(
                "INSERT INTO change_log(record_type,record_id,revision,operation,changed_at,payload_json,payload_hash) VALUES(?,?,?,?,?,?,?)",
                values,
            )
            connection.execute(
                "INSERT INTO change_log(record_type,record_id,revision,operation,changed_at,payload_json,payload_hash) VALUES(?,?,?,?,?,?,?)",
                values,
            )
            connection.execute("PRAGMA user_version=1")
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(RuntimeError, "duplicate record revisions"):
                ActivityStore(path)

    def test_file_database_uses_wal_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            with ActivityStore(Path(directory) / "activity.sqlite3") as store:
                self.assertEqual(store.journal_mode, "wal")
                self.assertTrue(store.foreign_keys_enabled)


class DailyCostValueTests(unittest.TestCase):
    def setUp(self):
        self.store = ActivityStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_row_is_frozen_canonicalizes_decimal_and_validates_identifiers(self):
        row = cost()
        self.assertEqual(row.amount, "12.34")
        with self.assertRaises(FrozenInstanceError):
            row.amount = "1"
        for changes in (
            {"day": "2026-7-2"},
            {"provider_id": "bad/provider"},
            {"account_ref": "bad account"},
            {"currency": "US/D"},
            {"cost_kind": "credit"},
            {"basis": "bad basis"},
            {"quality": "bad quality"},
            {"amount": "-0.01"},
            {"amount": "NaN"},
            {"amount": "Infinity"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                DailyCostRow(**(row.__dict__ | changes))

    def test_atomic_replace_known_zero_idempotency_revision_and_tombstone(self):
        original = cost()
        self.store.replace_daily_costs("openai", original.day, [original])
        first = self.store.snapshot_daily_costs(original.day, original.day)
        self.assertEqual(first.rows[0].record_id, "cost:2026-07-02:openai::actual:USD")
        self.assertEqual(first.rows[0].revision, 1)

        self.store.replace_daily_costs("openai", original.day, [original])
        unchanged = self.store.snapshot_daily_costs(original.day, original.day)
        self.assertEqual(unchanged.cursor, first.cursor)

        corrected = cost(amount="13.00")
        self.store.replace_daily_costs("openai", original.day, [corrected])
        updated = self.store.snapshot_daily_costs(original.day, original.day)
        self.assertEqual(updated.rows[0].amount, "13")
        self.assertEqual(updated.rows[0].revision, 2)

        self.store.replace_daily_costs("openai", original.day, [])
        empty = self.store.snapshot_daily_costs(original.day, original.day)
        self.assertEqual(empty.rows, ())
        self.assertIn((original.day, "openai", ""), empty.covered)
        self.assertIn(("openai", ""), empty.known_scopes)
        latest = self.store.changes(0)[-1]
        self.assertEqual((latest.record_type, latest.operation), ("daily_cost", "delete"))

    def test_scope_mismatch_and_duplicate_identity_roll_back(self):
        original = cost()
        self.store.replace_daily_costs("openai", original.day, [original])
        with self.assertRaisesRegex(ValueError, "scope"):
            self.store.replace_daily_costs(
                "openai",
                original.day,
                [cost(amount="1"), cost(provider_id="anthropic", amount="2")],
            )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.store.replace_daily_costs(
                "openai", original.day, [cost(amount="1"), cost(amount="2")]
            )
        self.assertEqual(self.store.daily_costs(original.day, original.day), [original])

    def test_account_scope_isolated_and_range_replace_marks_each_day_covered(self):
        self.store.replace_daily_cost_range(
            "openai",
            datetime.fromisoformat("2026-07-01T00:00:00").date(),
            datetime.fromisoformat("2026-07-03T00:00:00").date(),
            [cost(day="2026-07-02", account_ref="team", amount="2")],
            account_ref="team",
            imported_at=IMPORTED_AT,
        )
        snapshot = self.store.snapshot_daily_costs("2026-07-01", "2026-07-03")
        self.assertEqual([(row.day, row.account_ref) for row in snapshot.rows], [("2026-07-02", "team")])
        self.assertEqual(
            snapshot.covered,
            frozenset({
                ("2026-07-01", "openai", "team"),
                ("2026-07-02", "openai", "team"),
                ("2026-07-03", "openai", "team"),
            }),
        )


class AtomicImportCommitTests(unittest.TestCase):
    def setUp(self):
        self.store = ActivityStore(":memory:")
        self.since = datetime.fromisoformat("2026-07-01T00:00:00Z").date()
        self.until = datetime.fromisoformat("2026-07-03T00:00:00Z").date()
        self.attempted = datetime.fromisoformat(IMPORTED_AT.replace("Z", "+00:00"))

    def tearDown(self):
        self.store.close()

    def test_usage_and_cost_success_commit_coverage_and_health_together(self):
        self.assertTrue(
            self.store.commit_usage_import_success(
                "openai",
                "openai.organization.usage",
                self.since,
                self.until,
                [usage(provider_id="openai")],
                self.attempted,
            )
        )
        self.assertTrue(
            self.store.commit_cost_import_success(
                "openai",
                "openai.organization.costs",
                self.since,
                self.until,
                [cost()],
                self.attempted,
            )
        )

        self.assertTrue(self.store.has_source_success("openai", "openai.organization.usage"))
        self.assertTrue(self.store.has_source_success("openai", "openai.organization.costs"))
        self.assertTrue(self.store.has_cost_history("openai"))
        self.assertEqual(
            {status.source_id: status.state for status in self.store.source_statuses()},
            {
                "openai.organization.costs": "ok",
                "openai.organization.usage": "ok",
            },
        )

    def test_source_success_failure_rolls_back_rows_coverage_and_changes(self):
        before = self.store.snapshot_daily_usage("2026-07-01", "2026-07-03")
        with patch.object(
            self.store,
            "_record_source_success_locked",
            side_effect=RuntimeError("injected"),
        ):
            with self.assertRaises(RuntimeError):
                self.store.commit_usage_import_success(
                    "openai",
                    "openai.organization.usage",
                    self.since,
                    self.until,
                    [usage(provider_id="openai")],
                    self.attempted,
                )

        self.assertEqual(
            self.store.snapshot_daily_usage("2026-07-01", "2026-07-03"), before
        )
        self.assertEqual(self.store.source_statuses(), [])

    def test_older_success_cannot_overwrite_a_newer_attempt(self):
        newer = datetime.fromisoformat("2026-07-14T03:00:00+00:00")
        self.store.record_source_failure(
            "openai", "openai.organization.usage", "rate_limited", newer
        )

        committed = self.store.commit_usage_import_success(
            "openai",
            "openai.organization.usage",
            self.since,
            self.until,
            [usage(provider_id="openai")],
            self.attempted,
        )

        self.assertFalse(committed)
        self.assertEqual(
            self.store.snapshot_daily_usage("2026-07-01", "2026-07-03").rows, ()
        )
        self.assertEqual(self.store.source_statuses()[0].last_attempt_at, "2026-07-14T03:00:00.000000Z")


class DailyUsageTests(unittest.TestCase):
    def setUp(self):
        self.store = ActivityStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_known_zero_usage_and_cost_coverage_are_public_changes(self):
        before = self.store.current_change_seq
        self.store.replace_daily_usage("codex", "2026-07-18", [])
        self.store.replace_daily_costs("openai", "2026-07-18", [])

        self.assertEqual(
            [row.record_type for row in self.store.changes(before)],
            ["daily_coverage", "daily_cost_coverage"],
        )

    def test_row_is_frozen_and_validates_canonical_fields(self):
        row = usage()
        with self.assertRaises(FrozenInstanceError):
            row.total_tokens = 1
        for changes in (
            {"day": "2026-7-2"},
            {"provider_id": "bad/provider"},
            {"account_ref": "bad account"},
            {"model_id": "bad/model"},
            {"input_tokens": -1},
            {"output_tokens": True},
        ):
            values = row.__dict__ | changes
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                DailyUsageRow(**values)

    def test_exact_plan_row_defaults_import_time_to_normalized_utc(self):
        row = DailyUsageRow(
            day="2026-07-02",
            provider_id="codex",
            model_id="gpt-5.5",
            input_tokens=40_000_000,
            output_tokens=4_200_000,
            cache_read_tokens=30_000_000,
            cache_creation_tokens=0,
            reasoning_tokens=None,
            total_tokens=74_200_000,
            cost_amount="4.237100",
            cost_currency="USD",
            cost_basis="price_table_estimated",
            quality="derived",
        )

        self.assertEqual(row.account_ref, "")
        self.assertTrue(row.imported_at.endswith("Z"))
        parsed = datetime.fromisoformat(row.imported_at.replace("Z", "+00:00"))
        self.assertIsNotNone(parsed.utcoffset())

    def test_atomic_replacement_and_covered_zero_day(self):
        first = usage(model_id="gpt-5.5")
        second = usage(model_id="o3", total_tokens=10)
        self.store.replace_daily_usage("codex", "2026-07-02", [first, second])
        self.assertEqual([r.model_id for r in self.store.daily_usage("2026-07-02", "2026-07-02")], ["gpt-5.5", "o3"])

        self.store.replace_daily_usage("codex", "2026-07-02", [])
        self.assertEqual(self.store.daily_usage("2026-07-02", "2026-07-02"), [])
        self.assertTrue(self.store.is_day_covered("codex", "2026-07-02"))
        self.assertFalse(self.store.is_day_covered("codex", "2026-07-03"))

    def test_scope_mismatch_rolls_back_entire_replacement(self):
        original = usage()
        self.store.replace_daily_usage("codex", "2026-07-02", [original])
        mismatch = usage(provider_id="minimax", model_id="minimax-m2")
        with self.assertRaisesRegex(ValueError, "scope"):
            self.store.replace_daily_usage("codex", "2026-07-02", [usage(model_id="o3"), mismatch])
        self.assertEqual(self.store.daily_usage("2026-07-02", "2026-07-02"), [original])

    def test_unchanged_payload_is_noop_and_correction_increments_revision(self):
        row = usage()
        self.store.replace_daily_usage("codex", row.day, [row])
        initial = self.store.snapshot_daily_usage(row.day, row.day)
        self.store.replace_daily_usage("codex", row.day, [row])
        unchanged = self.store.snapshot_daily_usage(row.day, row.day)
        self.assertEqual(unchanged.cursor, initial.cursor)
        self.assertEqual(unchanged.rows[0].revision, 1)

        corrected = usage(total_tokens=74_200_001)
        self.store.replace_daily_usage("codex", row.day, [corrected])
        latest = self.store.snapshot_daily_usage(row.day, row.day)
        self.assertEqual(latest.rows[0].record_id, initial.rows[0].record_id)
        self.assertEqual(latest.rows[0].revision, 2)
        self.assertGreater(latest.cursor, initial.cursor)

    def test_usage_source_is_persisted_and_source_switch_is_a_revision(self):
        row = usage(provider_id="openai")
        self.store.commit_usage_import_success(
            "openai",
            "openai.organization.usage",
            date.fromisoformat(row.day),
            date.fromisoformat(row.day),
            [row],
            datetime.fromisoformat("2026-07-14T02:00:00+00:00"),
        )
        official = self.store.snapshot_daily_usage(row.day, row.day)
        self.assertEqual(official.rows[0].source_id, "openai.organization.usage")
        self.assertEqual(
            official.coverage_sources,
            frozenset({(row.day, "openai", "", "openai.organization.usage")}),
        )

        self.store.replace_daily_usage(
            "openai", row.day, [row], source_id="openusage.daily"
        )
        fallback = self.store.snapshot_daily_usage(row.day, row.day)
        self.assertEqual(fallback.rows[0].source_id, "openusage.daily")
        self.assertEqual(fallback.rows[0].revision, official.rows[0].revision + 1)
        self.assertGreater(fallback.cursor, official.cursor)

    def test_replacement_deletion_emits_tombstone_for_incremental_consumers(self):
        self.store.replace_daily_usage("codex", "2026-07-02", [usage(model_id="gpt-5.5"), usage(model_id="o3")])
        cursor = self.store.snapshot_daily_usage("2026-07-02", "2026-07-02").cursor
        self.store.replace_daily_usage("codex", "2026-07-02", [usage(model_id="gpt-5.5")])
        changes = self.store.changes(cursor)
        self.assertEqual([(item.operation, item.record_id.endswith(":o3")) for item in changes], [("delete", True)])

    def test_revision_remains_monotonic_across_delete_reinsert_and_correction(self):
        row = usage()
        self.store.replace_daily_usage("codex", row.day, [row])
        record_id = self.store.snapshot_daily_usage(row.day, row.day).rows[0].record_id
        self.store.replace_daily_usage("codex", row.day, [])
        self.store.replace_daily_usage("codex", row.day, [row])
        self.store.replace_daily_usage("codex", row.day, [usage(total_tokens=74_200_001)])

        revisions = [
            (change.operation, change.revision)
            for change in self.store.changes(0)
            if change.record_type == "daily_usage" and change.record_id == record_id
        ]
        self.assertEqual(
            revisions,
            [("insert", 1), ("delete", 2), ("insert", 3), ("update", 4)],
        )

    def test_snapshot_uses_one_sqlite_read_transaction_across_connections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "activity.sqlite3"
            reader = ActivityStore(path)
            writer = ActivityStore(path)
            cursor_select_entered = threading.Event()
            release_cursor_select = threading.Event()
            original_cursor = reader._current_change_seq_locked

            def blocking_cursor():
                cursor_select_entered.set()
                if not release_cursor_select.wait(timeout=2):
                    raise TimeoutError("test did not release cursor read")
                return original_cursor()

            try:
                reader.replace_daily_usage("codex", "2026-07-02", [usage()])
                with patch.object(reader, "_current_change_seq_locked", side_effect=blocking_cursor):
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        future = executor.submit(
                            reader.snapshot_daily_usage,
                            "2026-07-01",
                            "2026-07-03",
                        )
                        self.assertTrue(cursor_select_entered.wait(timeout=2))
                        writer.replace_daily_usage(
                            "codex",
                            "2026-07-03",
                            [usage(day="2026-07-03")],
                        )
                        writer_cursor = writer.current_change_seq
                        release_cursor_select.set()
                        snapshot = future.result(timeout=2)

                self.assertEqual([row.day for row in snapshot.rows], ["2026-07-02"])
                self.assertLess(snapshot.cursor, writer_cursor)
                self.assertTrue(reader.changes(snapshot.cursor))
            finally:
                release_cursor_select.set()
                writer.close()
                reader.close()

    def test_snapshot_collections_are_immutable(self):
        self.store.replace_daily_usage("codex", "2026-07-02", [usage()])
        snapshot = self.store.snapshot_daily_usage("2026-07-02", "2026-07-02")

        self.assertIsInstance(snapshot.rows, tuple)
        self.assertIsInstance(snapshot.covered, frozenset)
        with self.assertRaises(AttributeError):
            snapshot.rows.append(usage())
        with self.assertRaises(AttributeError):
            snapshot.covered.add(("2026-07-03", "codex", ""))

    def test_independent_writers_serialize_revisions_for_same_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "activity.sqlite3"
            first = ActivityStore(path)
            second = ActivityStore(path)
            first.replace_daily_usage("codex", "2026-07-02", [usage()])
            rendezvous = threading.Barrier(2)
            original_next_revision = ActivityStore._next_revision_locked

            def coordinated_revision(store, record_type, record_id):
                revision = original_next_revision(store, record_type, record_id)
                if record_type == "daily_usage" and not store._connection.in_transaction:
                    rendezvous.wait(timeout=2)
                return revision

            try:
                with patch.object(
                    ActivityStore,
                    "_next_revision_locked",
                    autospec=True,
                    side_effect=coordinated_revision,
                ):
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        futures = [
                            executor.submit(
                                store.replace_daily_usage,
                                "codex",
                                "2026-07-02",
                                [usage(total_tokens=total)],
                            )
                            for store, total in (
                                (first, 74_200_001),
                                (second, 74_200_002),
                            )
                        ]
                        for future in futures:
                            future.result(timeout=3)

                changes = [
                    change
                    for change in first.changes(0)
                    if change.record_type == "daily_usage"
                ]
                revisions = [change.revision for change in changes]
                self.assertEqual(revisions, [1, 2, 3])
                self.assertEqual(len(revisions), len(set(revisions)))
                stored = first.snapshot_daily_usage("2026-07-02", "2026-07-02").rows[0]
                self.assertEqual(stored.revision, revisions[-1])
            finally:
                second.close()
                first.close()

    def test_consistent_snapshot_cursor_and_gap_safe_changes(self):
        self.store.replace_daily_usage("codex", "2026-07-02", [usage()])
        snapshot = self.store.snapshot_daily_usage("2026-07-01", "2026-07-03")
        self.store.replace_daily_usage("codex", "2026-07-03", [usage(day="2026-07-03")])
        page = self.store.changes(snapshot.cursor, limit=1)
        self.assertEqual(len(page), 1)
        self.assertGreater(page[0].change_seq, snapshot.cursor)
        next_page = self.store.changes(page[0].change_seq, limit=10)
        self.assertTrue(all(change.change_seq > page[0].change_seq for change in next_page))


class ProviderInstanceTests(unittest.TestCase):
    def setUp(self):
        self.store = ActivityStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_exact_insert_persists_canonical_instance_and_upsert_change(self):
        stored = self.store.upsert_provider_instance(provider_instance())

        self.assertEqual(stored.revision, 1)
        self.assertEqual(stored.observed_at, "2026-07-14T02:00:00.000000Z")
        self.assertEqual(self.store.provider_instances(), (stored,))
        changes = self.store.changes(0)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].record_type, "provider_instance")
        self.assertEqual(changes[0].record_id, "codex")
        self.assertEqual(changes[0].operation, "upsert")
        self.assertNotIn("observed_at", changes[0].payload_json or "")

    def test_unchanged_metadata_is_a_noop(self):
        first = self.store.upsert_provider_instance(provider_instance())
        cursor = self.store.current_change_seq

        unchanged = self.store.upsert_provider_instance(
            provider_instance(observed_at="2026-07-14T03:00:00Z")
        )

        self.assertEqual(unchanged, first)
        self.assertEqual(self.store.current_change_seq, cursor)

    def test_metadata_correction_increments_monotonic_revision(self):
        self.store.upsert_provider_instance(provider_instance())

        corrected = self.store.upsert_provider_instance(
            provider_instance(
                display_name="OpenAI Codex",
                observed_at="2026-07-14T03:00:00Z",
            )
        )

        self.assertEqual(corrected.revision, 2)
        self.assertEqual(corrected.display_name, "OpenAI Codex")
        self.assertEqual(
            [change.revision for change in self.store.changes(0)], [1, 2]
        )

    def test_invalid_unknown_family_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown family"):
            self.store.upsert_provider_instance(
                provider_instance(provider_id="future_agent", family_id="other_agent")
            )
        self.assertEqual(self.store.provider_instances(), ())
        self.assertEqual(self.store.current_change_seq, 0)

    def test_known_provider_family_mismatch_rolls_back(self):
        last_good = self.store.upsert_provider_instance(provider_instance())
        cursor = self.store.current_change_seq

        with self.assertRaisesRegex(ValueError, "family mismatch"):
            self.store.upsert_provider_instance(
                provider_instance(family_id="cursor", display_name="Cursor")
            )

        self.assertEqual(self.store.provider_instances(), (last_good,))
        self.assertEqual(self.store.current_change_seq, cursor)

    def test_existing_multi_account_instance_cannot_rebind_to_another_family(self):
        last_good = self.store.upsert_provider_instance(
            provider_instance(
                provider_id="minimax-primary",
                family_id="minimax",
                display_name="MiniMax primary",
                credential_source="minimax_builtin_api",
                source_kind="builtin_api",
            )
        )
        cursor = self.store.current_change_seq

        with self.assertRaisesRegex(ValueError, "family mismatch"):
            self.store.upsert_provider_instance(
                provider_instance(
                    provider_id="minimax-primary",
                    family_id="step_plan",
                    display_name="Step Plan primary",
                    credential_source="step_plan_official_api",
                    source_kind="official_api",
                )
            )

        self.assertEqual(self.store.provider_instances(), (last_good,))
        self.assertEqual(self.store.current_change_seq, cursor)

    def test_unknown_future_upstream_family_can_bind_only_to_itself(self):
        stored = self.store.upsert_provider_instance(
            provider_instance(
                provider_id="future_agent",
                family_id="future_agent",
                display_name="Future Agent",
                category="api",
            )
        )

        self.assertEqual(stored.family_id, "future_agent")

    def test_unknown_generic_https_family_accepts_only_explicit_safe_pair(self):
        for category in ("api", "subscription"):
            with self.subTest(category=category):
                provider_id = f"generic-{category}"
                stored = self.store.upsert_provider_instance(
                    provider_instance(
                        provider_id=provider_id,
                        family_id=provider_id,
                        display_name=f"Generic {category}",
                        category=category,
                        credential_source="api_key",
                        source_kind="generic_https",
                    )
                )
                self.assertEqual(stored.family_id, provider_id)

    def test_unknown_family_rejects_generic_source_impersonation_and_arbitrary_pairs(self):
        invalid_pairs = (
            ("api_key", "openusage"),
            ("openusage", "generic_https"),
            ("minimax_builtin_api", "builtin_api"),
            ("api_key", "official_api"),
        )
        for credential_source, source_kind in invalid_pairs:
            with self.subTest(
                credential_source=credential_source, source_kind=source_kind
            ), self.assertRaisesRegex(ValueError, "unknown family"):
                self.store.upsert_provider_instance(
                    provider_instance(
                        provider_id=f"custom-{source_kind}",
                        family_id=f"custom-{source_kind}",
                        display_name="Custom Provider",
                        category="api",
                        credential_source=credential_source,
                        source_kind=source_kind,
                    )
                )

    def test_instance_ids_can_bind_explicitly_to_a_known_builtin_family(self):
        stored = self.store.upsert_provider_instance(
            provider_instance(
                provider_id="minimax-1783978290",
                family_id="minimax",
                display_name="MiniMax primary",
                credential_source="minimax_builtin_api",
                source_kind="builtin_api",
            )
        )

        self.assertEqual(stored.family_id, "minimax")

    def test_provider_instance_snapshot_is_stably_ordered_and_immutable(self):
        self.store.upsert_provider_instance(
            provider_instance(
                provider_id="future_agent",
                family_id="future_agent",
                display_name="Future Agent",
                category="api",
            )
        )
        self.store.upsert_provider_instance(provider_instance())

        snapshot = self.store.snapshot_provider_instances()

        self.assertIsInstance(snapshot.rows, tuple)
        self.assertEqual(
            [row.provider_id for row in snapshot.rows], ["codex", "future_agent"]
        )
        with self.assertRaises(AttributeError):
            snapshot.rows.append(provider_instance())
        with self.assertRaises(FrozenInstanceError):
            snapshot.rows[0].display_name = "mutated"

    def test_failed_observation_preserves_last_good_instance(self):
        last_good = self.store.upsert_provider_instance(provider_instance())
        cursor = self.store.current_change_seq

        with patch.object(
            self.store, "_append_change", side_effect=RuntimeError("write failed")
        ), self.assertRaisesRegex(RuntimeError, "write failed"):
            self.store.upsert_provider_instance(
                provider_instance(
                    display_name="OpenAI Codex",
                    observed_at="2026-07-14T03:00:00Z",
                )
            )

        self.assertEqual(self.store.provider_instances(), (last_good,))
        self.assertEqual(self.store.current_change_seq, cursor)

    def test_private_display_labels_are_rejected_without_persistence_or_echo(self):
        private_labels = (
            "alice@example.com",
            "/Users/alice/.config/openusage/credentials.json",
            r"C:\\Users\\alice\\provider-token.txt",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
            "credential=super-secret-value",
            "token=top-secret-value",
            "cookie=session-secret-value",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP",
            "sk-abcdefghijklmnopqrstuvwxyz123456",
            "Codex (/Users/alice/.config/openusage/credentials.json)",
            r"Codex C:\Users\alice\provider-token.txt",
            "Step Plan " + "Q4KG" + "X" * 60,
            "username=alice",
            "user: alice",
            "account_email=alice@localhost",
            'response_body={"user":"alice"}',
            '{"user":"alice"}',
            "token abcdefghijklmnopqrstuvwxyzabcdef",
        )

        for label in private_labels:
            with self.subTest(label=label):
                stdout = StringIO()
                stderr = StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    with self.assertRaisesRegex(ValueError, "safe provider label") as raised:
                        self.store.upsert_provider_instance(
                            provider_instance(display_name=label)
                        )
                database_text = "\n".join(self.store._connection.iterdump())
                emitted = stdout.getvalue() + stderr.getvalue() + str(raised.exception)
                self.assertNotIn(label, database_text)
                self.assertNotIn(label, emitted)
                self.assertEqual(self.store.provider_instances(), ())
                self.assertEqual(self.store.changes(0), [])

    def test_normal_provider_labels_remain_accepted(self):
        for index, label in enumerate(
            (
                "Z.AI",
                "Alibaba Cloud",
                "MiniMax primary",
                "Step Plan China",
                "OpenAI GPT-4o",
                "Claude 3.5 Sonnet",
                "Qwen2.5-Coder-32B-Instruct",
                "OpenAI/Anthropic",
                "User API",
                "Body AI",
                "Superuser: AI",
                "Token Activity",
                "Access Token Monitor",
                "Secret Management",
            )
        ):
            with self.subTest(label=label):
                stored = self.store.upsert_provider_instance(
                    provider_instance(
                        provider_id=f"future-provider-{index}",
                        family_id=f"future-provider-{index}",
                        display_name=label,
                        category="api",
                    )
                )
                self.assertEqual(stored.display_name, label)


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.store = ActivityStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_unchanged_within_hour_deduplicates(self):
        first = self.store.record_quota(quota())
        second = self.store.record_quota(quota(observed_at="2026-07-14T02:59:59Z"))
        self.assertEqual(first.revision, 1)
        self.assertEqual(second.revision, 1)
        self.assertEqual(len(self.store.quota_snapshots(first.record_id)), 1)
        self.assertEqual(
            [row.record_type for row in self.store.changes(0)],
            ["quota_snapshot", "quota"],
        )

    def test_changed_quota_updates_current_and_appends_history(self):
        first = self.store.record_quota(quota())
        cursor = self.store.current_change_seq
        changed = self.store.record_quota(quota(observed_at="2026-07-14T02:15:00Z", remaining="17"))
        self.assertEqual(changed.revision, 2)
        self.assertEqual(self.store.quota_states(), [changed])
        self.assertEqual(len(self.store.quota_snapshots(first.record_id)), 2)
        self.assertEqual(
            [row.record_type for row in self.store.changes(cursor)],
            ["quota_snapshot", "quota"],
        )

    def test_unchanged_after_hour_samples_history_without_canonical_change(self):
        self.store.record_quota(quota())
        cursor = self.store.current_change_seq
        latest = self.store.record_quota(quota(observed_at="2026-07-14T03:00:00Z"))
        self.assertEqual(latest.revision, 1)
        self.assertEqual(len(self.store.quota_snapshots(latest.record_id)), 2)
        changes = self.store.changes(cursor)
        self.assertEqual([change.record_type for change in changes], ["quota_snapshot"])
        self.assertGreater(self.store.current_change_seq, cursor)

    def test_late_changed_observation_is_history_only_and_deduplicates(self):
        newest = self.store.record_quota(
            quota(observed_at="2026-07-14T03:00:00Z", remaining="17")
        )
        cursor = self.store.current_change_seq
        late = quota(observed_at="2026-07-14T02:00:00Z", remaining="18")

        returned = self.store.record_quota(late)
        self.store.record_quota(late)

        self.assertEqual(returned, newest)
        self.assertEqual(self.store.quota_states(), [newest])
        self.assertEqual(
            [row.record_type for row in self.store.changes(cursor)],
            ["quota_snapshot"],
        )
        self.assertEqual(len(self.store.quota_snapshots(newest.record_id)), 2)

    def test_equivalent_zero_ratios_have_one_canonical_representation(self):
        base = quota()
        observations = [
            QuotaObservation(
                **(
                    base.__dict__
                    | {
                        "observed_at": f"2026-07-14T02:0{index}:00Z",
                        "remaining_ratio": ratio,
                    }
                )
            )
            for index, ratio in enumerate((0, 0.0, -0.0))
        ]

        states = [self.store.record_quota(item) for item in observations]

        self.assertTrue(all(state.remaining_ratio == 0.0 for state in states))
        self.assertTrue(all(str(state.remaining_ratio) == "0.0" for state in states))
        self.assertEqual([state.revision for state in states], [1, 1, 1])
        self.assertEqual(
            [row.record_type for row in self.store.changes(0)],
            ["quota_snapshot", "quota"],
        )

    def test_quota_validation_rejects_bool_invalid_ids_and_ratio(self):
        base = quota()
        for changes in ({"remaining_ratio": True}, {"remaining_ratio": 1.01}, {"provider_id": "bad/id"}, {"account_ref": "bad account"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                QuotaObservation(**(base.__dict__ | changes))


class SourceStatusFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.store = ActivityStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_source_health_changes_advance_the_public_cursor_without_duplicate_noise(self):
        attempted = datetime.fromisoformat("2026-07-14T02:00:00+00:00")
        before = self.store.current_change_seq

        self.store.record_source_failure(
            "codex", "current.quota", "timeout", attempted
        )
        inserted = self.store.snapshot_changes(before, 100)
        self.assertEqual([row.record_type for row in inserted.rows], ["source_status"])
        self.assertGreater(inserted.cursor, before)

        self.store.record_source_failure(
            "codex", "current.quota", "timeout", attempted
        )
        self.assertEqual(self.store.current_change_seq, inserted.cursor)

    def test_transient_failures_preserve_last_success_and_freshness_deadline(self):
        success = datetime.fromisoformat("2026-07-14T02:00:00+00:00")
        self.store.record_source_success(
            "codex", "openusage.daily", success, freshness_seconds=300
        )

        for minute, state in enumerate(
            ("temporarily_unavailable", "auth", "permission_denied", "rate_limited"),
            start=1,
        ):
            attempted = datetime.fromisoformat(
                f"2026-07-14T02:0{minute}:00+00:00"
            )
            self.store.record_source_status(
                "codex", "openusage.daily", state, attempted, f"{state}_error"
            )
            status = self.store.source_statuses()[0]
            self.assertEqual(status.state, state)
            self.assertEqual(status.last_success_at, "2026-07-14T02:00:00.000000Z")
            self.assertEqual(status.stale_at, "2026-07-14T02:05:00.000000Z")

    def test_explicit_stale_never_moves_an_existing_stale_deadline_later(self):
        success = datetime.fromisoformat("2026-07-14T02:00:00+00:00")
        self.store.record_source_success(
            "codex", "openusage.daily", success, freshness_seconds=300
        )
        self.store.record_source_status(
            "codex",
            "openusage.daily",
            "stale",
            datetime.fromisoformat("2026-07-14T02:10:00+00:00"),
            "expired",
        )

        status = self.store.source_statuses()[0]
        self.assertEqual(status.last_success_at, "2026-07-14T02:00:00.000000Z")
        self.assertEqual(status.stale_at, "2026-07-14T02:05:00.000000Z")

    def test_first_failure_has_no_freshness_and_later_success_replaces_it(self):
        attempted = datetime.fromisoformat("2026-07-14T02:00:00+00:00")
        self.store.record_source_failure(
            "codex", "openusage.daily", "command_failed", attempted
        )
        failure = self.store.source_statuses()[0]
        self.assertIsNone(failure.last_success_at)
        self.assertIsNone(failure.stale_at)

        recovered = datetime.fromisoformat("2026-07-14T03:00:00+00:00")
        self.store.record_source_success(
            "codex", "openusage.daily", recovered, freshness_seconds=600
        )
        status = self.store.source_statuses()[0]
        self.assertEqual(status.state, "ok")
        self.assertEqual(status.revision, 2)
        self.assertEqual(status.last_success_at, "2026-07-14T03:00:00.000000Z")
        self.assertEqual(status.stale_at, "2026-07-14T03:10:00.000000Z")
        self.assertIsNone(status.error_code)

    def test_older_attempt_cannot_replace_newer_source_health(self):
        newer = datetime.fromisoformat("2026-07-14T03:00:00+00:00")
        older = datetime.fromisoformat("2026-07-14T02:00:00+00:00")

        self.store.record_source_success("cursor", "current.quota", newer)
        self.store.record_source_status(
            "cursor", "current.quota", "stale", older, "quota_unavailable"
        )

        status = self.store.source_statuses()[0]
        self.assertEqual(status.state, "ok")
        self.assertEqual(status.last_attempt_at, "2026-07-14T03:00:00.000000Z")
        self.assertEqual(status.last_success_at, "2026-07-14T03:00:00.000000Z")
        self.assertEqual(status.stale_at, "2026-07-14T03:05:00.000000Z")
        self.assertIsNone(status.error_code)

        store = ActivityStore(":memory:")
        try:
            store.record_source_status(
                "cursor",
                "current.quota",
                "temporarily_unavailable",
                newer,
                "quota_unavailable",
            )
            store.record_source_success("cursor", "current.quota", older)
            status = store.source_statuses()[0]
            self.assertEqual(status.state, "temporarily_unavailable")
            self.assertEqual(
                status.last_attempt_at, "2026-07-14T03:00:00.000000Z"
            )
            self.assertEqual(status.error_code, "quota_unavailable")
        finally:
            store.close()

    def test_clock_rollback_compare_and_swap_replaces_only_observed_attempt(self):
        current = datetime.fromisoformat("2026-07-14T03:00:00+00:00")
        future = datetime.fromisoformat("2026-07-17T03:00:00+00:00")
        later = datetime.fromisoformat("2026-07-18T03:00:00+00:00")

        self.store.record_source_success("cursor", "current.quota", future)
        self.store.record_source_success(
            "cursor",
            "current.quota",
            current,
            replace_if_last_attempt_at=future,
        )
        self.assertEqual(
            self.store.source_statuses()[0].last_attempt_at,
            "2026-07-14T03:00:00.000000Z",
        )

        self.store.record_source_success("cursor", "current.quota", later)
        self.store.record_source_status(
            "cursor",
            "current.quota",
            "stale",
            current,
            "quota_unavailable",
            replace_if_last_attempt_at=future,
        )
        status = self.store.source_statuses()[0]
        self.assertEqual(status.state, "ok")
        self.assertEqual(status.last_attempt_at, "2026-07-18T03:00:00.000000Z")

    def test_delete_source_status_is_exact_and_validated(self):
        attempted = datetime.fromisoformat("2026-07-14T03:00:00+00:00")
        self.store.record_source_success("hermes", "openusage.daily", attempted)
        self.store.record_source_status(
            "hermes",
            "current.quota",
            "stale",
            attempted,
            "quota_unavailable",
        )
        self.store.record_source_status(
            "openclaw",
            "current.quota",
            "stale",
            attempted,
            "quota_unavailable",
        )
        before_delete = self.store.current_change_seq

        self.store.delete_source_status("hermes", "current.quota", attempted)

        self.assertEqual(
            {(row.provider_id, row.source_id) for row in self.store.source_statuses()},
            {("hermes", "openusage.daily"), ("openclaw", "current.quota")},
        )
        self.assertEqual(
            [(row.record_type, row.operation) for row in self.store.changes(before_delete)],
            [("source_status", "delete")],
        )
        with self.assertRaises(ValueError):
            self.store.delete_source_status("bad/id", "current.quota", attempted)
        with self.assertRaises(ValueError):
            self.store.delete_source_status("hermes", "bad source", attempted)

    def test_delete_source_status_cannot_erase_newer_health(self):
        older = datetime.fromisoformat("2026-07-14T03:00:00+00:00")
        newer = datetime.fromisoformat("2026-07-14T04:00:00+00:00")
        self.store.record_source_success("hermes", "current.quota", newer)

        self.store.delete_source_status("hermes", "current.quota", older)

        status = self.store.source_statuses()[0]
        self.assertEqual(status.provider_id, "hermes")
        self.assertEqual(status.last_attempt_at, "2026-07-14T04:00:00.000000Z")


class RetentionAndConcurrencyTests(unittest.TestCase):
    def test_730_day_retention_uses_exclusive_cutoff_keeps_current_quota_and_emits_daily_tombstone(self):
        store = ActivityStore(":memory:")
        try:
            # 2024-07-14 is exactly 730 days before 2026-07-14.
            store.replace_daily_usage("codex", "2024-07-13", [usage(day="2024-07-13")])
            store.replace_daily_usage("codex", "2024-07-14", [usage(day="2024-07-14")])
            store.replace_daily_costs("openai", "2024-07-13", [cost(day="2024-07-13")])
            store.replace_daily_costs("openai", "2024-07-14", [cost(day="2024-07-14")])
            store.record_quota(quota(observed_at="2024-07-13T00:00:00Z"))
            store.record_quota(quota(observed_at="2026-07-14T02:00:00Z", remaining="17"))
            cursor = store.current_change_seq
            result = store.purge_before("2024-07-14", "2024-07-14T00:00:00Z")
            self.assertEqual(result.daily_rows, 1)
            self.assertEqual(result.cost_rows, 1)
            self.assertEqual(result.cost_coverage_rows, 1)
            self.assertEqual(result.quota_snapshots, 1)
            self.assertFalse(store.is_day_covered("codex", "2024-07-13"))
            self.assertTrue(store.is_day_covered("codex", "2024-07-14"))
            cost_snapshot = store.snapshot_daily_costs("2024-07-13", "2024-07-14")
            self.assertEqual([row.day for row in cost_snapshot.rows], ["2024-07-14"])
            self.assertNotIn(("2024-07-13", "openai", ""), cost_snapshot.covered)
            self.assertEqual(len(store.quota_states()), 1)
            deleted = store.changes(cursor)
            self.assertTrue(any(change.operation == "delete" for change in deleted))
            self.assertIn(
                ("quota_snapshot", "delete"),
                [(change.record_type, change.operation) for change in deleted],
            )

            store.replace_daily_usage(
                "codex", "2024-07-13", [usage(day="2024-07-13")]
            )
            coverage_revisions = [
                (change.operation, change.revision)
                for change in store.changes(0)
                if change.record_type == "daily_coverage"
                and change.record_id == "coverage:2024-07-13:codex:"
            ]
            self.assertEqual(
                coverage_revisions,
                [("insert", 1), ("delete", 2), ("insert", 3)],
            )
        finally:
            store.close()

    def test_summary_read_is_serialized_by_single_rlock(self):
        store = ActivityStore(":memory:")
        write_entered = threading.Event()
        release_write = threading.Event()
        read_started = threading.Event()
        try:
            store.replace_daily_usage("codex", "2026-07-02", [usage()])

            original_payload = ActivityStore._daily_payload

            def blocking_payload(row, source_id):
                write_entered.set()
                if not release_write.wait(timeout=2):
                    raise TimeoutError("test did not release writer")
                return original_payload(row, source_id)

            def read_summary():
                read_started.set()
                return store.summary("2026-07-02", "2026-07-02")

            with patch.object(ActivityStore, "_daily_payload", side_effect=blocking_payload):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    writer = executor.submit(
                        store.replace_daily_usage,
                        "codex",
                        "2026-07-02",
                        [usage(total_tokens=74_200_001)],
                    )
                    self.assertTrue(write_entered.wait(timeout=2))
                    reader = executor.submit(read_summary)
                    self.assertTrue(read_started.wait(timeout=2))
                    self.assertFalse(reader.done())
                    release_write.set()
                    writer.result(timeout=2)
                    self.assertEqual(reader.result(timeout=2).total_tokens, 74_200_001)
        finally:
            release_write.set()
            store.close()


class ResourceSnapshotTests(unittest.TestCase):
    def test_resource_snapshot_is_revision_consistent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "activity.sqlite3"
            reader = ActivityStore(path)
            writer = ActivityStore(path)
            quota_select_entered = threading.Event()
            release_quota_select = threading.Event()
            try:
                reader.replace_daily_usage(
                    "codex", "2026-07-18",
                    [usage(day="2026-07-18", total_tokens=1)],
                )
                reader.record_quota(quota())
                reader.upsert_provider_instance(provider_instance())
                reader.record_source_success(
                    "codex", "openusage.daily",
                    datetime.fromisoformat("2026-07-18T01:00:00+00:00"),
                )
                original = reader._resource_quota_states_locked

                def blocking_quota_select():
                    quota_select_entered.set()
                    if not release_quota_select.wait(timeout=2):
                        raise TimeoutError("test did not release quota read")
                    return original()

                with patch.object(
                    reader,
                    "_resource_quota_states_locked",
                    side_effect=blocking_quota_select,
                ):
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        future = executor.submit(
                            reader.snapshot_resource_state, "2026-07-18"
                        )
                        self.assertTrue(quota_select_entered.wait(timeout=2))
                        writer.replace_daily_usage(
                            "codex", "2026-07-18",
                            [usage(day="2026-07-18", total_tokens=2)],
                        )
                        writer.upsert_provider_instance(provider_instance(
                            provider_id="cursor", family_id="cursor",
                            display_name="Cursor",
                        ))
                        writer_cursor = writer.current_change_seq
                        release_quota_select.set()
                        snapshot = future.result(timeout=2)

                self.assertEqual(snapshot.local_day, "2026-07-18")
                self.assertEqual(snapshot.today_tokens, 1)
                self.assertEqual(snapshot.model_count, 1)
                self.assertEqual(snapshot.covered_day_count, 1)
                self.assertEqual(
                    [row.provider_id for row in snapshot.provider_instances],
                    ["codex"],
                )
                self.assertEqual(
                    [row.provider_id for row in snapshot.quota_states],
                    ["minimax"],
                )
                self.assertEqual(
                    [row.provider_id for row in snapshot.source_statuses],
                    ["codex"],
                )
                self.assertLess(snapshot.cursor, writer_cursor)
                self.assertTrue(reader.changes(snapshot.cursor))
            finally:
                release_quota_select.set()
                writer.close()
                reader.close()


if __name__ == "__main__":
    unittest.main()
