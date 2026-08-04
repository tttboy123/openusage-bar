import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from openusage_bar.activity_store import ActivityStore
from openusage_bar.cc_switch import (
    CC_SWITCH_COST_SOURCE_ID,
    CcSwitchCostImporter,
    CcSwitchStatusAdapter,
)
from openusage_bar.models import ProviderStatus
from openusage_bar.providers.contracts import (
    CostImportSuccess,
    ImportFailure,
    UsageImportSuccess,
)
from openusage_bar.query import QueryService


def _build_db(path: Path, *, current_codex: str = "DeepSeek") -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE providers ("
        "id TEXT, app_type TEXT, name TEXT, category TEXT, "
        "is_current INTEGER, sort_index INTEGER)"
    )
    connection.execute(
        "INSERT INTO providers VALUES "
        "('codex-official','codex','OpenAI Official','official',0,0)"
    )
    connection.execute(
        "INSERT INTO providers VALUES (?, 'codex', ?, 'cn_official', 1, 1)",
        ("provider-deepseek", current_codex),
    )
    connection.execute(
        "CREATE TABLE usage_daily_rollups ("
        "date TEXT, app_type TEXT, provider_id TEXT, model TEXT, "
        "request_count INTEGER, success_count INTEGER, "
        "input_tokens INTEGER, output_tokens INTEGER, "
        "cache_read_tokens INTEGER, cache_creation_tokens INTEGER, "
        "total_cost_usd TEXT, avg_latency_ms REAL, input_token_semantics TEXT)"
    )
    connection.execute(
        "INSERT INTO usage_daily_rollups VALUES "
        "('2026-07-05','codex','_codex_session','deepseek-v4-flash',"
        "10,10,100,50,0,0,'1.2500',12.3,'unknown')"
    )
    connection.commit()
    connection.close()


class CcSwitchCostImporterTests(unittest.TestCase):
    def test_maps_rollups_into_ledger_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cc-switch.db"
            _build_db(db)

            result = CcSwitchCostImporter(db_path=db).fetch_costs(
                date(2026, 7, 1), date(2026, 7, 31)
            )

            self.assertIsInstance(result, CostImportSuccess)
            self.assertEqual(len(result.rows), 1)
            row = result.rows[0]
            self.assertEqual(row.provider_id, "cc_switch")
            self.assertEqual(row.amount, "1.25")
            self.assertEqual(row.currency, "USD")
            self.assertEqual(row.basis, "cc_switch.rollups")
            self.assertEqual(row.account_ref, "cc_switch")

    def test_missing_database_reports_unavailable(self):
        result = CcSwitchCostImporter(
            db_path=Path("/nonexistent/cc-switch.db")
        ).fetch_costs(date(2026, 7, 1), date(2026, 7, 31))

        self.assertEqual(result, ImportFailure("source_unavailable"))

    def test_invalid_range_is_rejected(self):
        result = CcSwitchCostImporter(
            db_path=Path("/nonexistent/cc-switch.db")
        ).fetch_costs(date(2026, 7, 31), date(2026, 7, 1))

        self.assertEqual(result, ImportFailure("invalid_request"))

    def test_maps_rollups_into_usage_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cc-switch.db"
            _build_db(db)

            result = CcSwitchCostImporter(db_path=db).fetch_usage(
                date(2026, 7, 1), date(2026, 7, 31)
            )

            self.assertIsInstance(result, UsageImportSuccess)
            self.assertEqual(len(result.rows), 1)
            row = result.rows[0]
            self.assertEqual(row.provider_id, "cc_switch")
            self.assertEqual(row.model_id, "_codex_session.deepseek-v4-flash")
            self.assertEqual(row.input_tokens, 100)
            self.assertEqual(row.output_tokens, 50)
            self.assertEqual(row.total_tokens, 150)
            self.assertEqual(row.cost_amount, "1.25")
            self.assertEqual(row.cost_currency, "USD")
            self.assertEqual(row.cost_basis, "cc_switch.rollups")
            self.assertEqual(row.token_counting_convention, "components_disjoint")

    def test_committed_cost_source_appears_in_snapshot_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cc-switch.db"
            _build_db(db)
            importer = CcSwitchCostImporter(db_path=db)
            result = importer.fetch_costs(date(2026, 7, 1), date(2026, 7, 31))
            store = ActivityStore(Path(directory) / "ledger.sqlite3")
            try:
                committed = store.commit_cost_import_success(
                    "cc_switch",
                    CC_SWITCH_COST_SOURCE_ID,
                    date(2026, 7, 1),
                    date(2026, 7, 31),
                    result.rows,
                    datetime(2026, 7, 14, tzinfo=timezone.utc),
                    account_ref="cc_switch",
                )
                self.assertTrue(committed)
                snapshot = QueryService(store).resource_snapshot(date(2026, 7, 14))
                source_ids = {(s.provider_id, s.source_id) for s in snapshot.sources}
                self.assertIn(("cc_switch", CC_SWITCH_COST_SOURCE_ID), source_ids)
            finally:
                store.close()


class CcSwitchStatusAdapterTests(unittest.TestCase):
    def test_reports_active_codex_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "cc-switch.db"
            _build_db(db)

            card = CcSwitchStatusAdapter(db_path=db).fetch()

            self.assertEqual(card.provider_id, "cc_switch")
            self.assertEqual(card.primary, "DeepSeek")
            self.assertIn("DeepSeek", card.detail or "")
            self.assertEqual(card.status, ProviderStatus.OK)

    def test_missing_database_is_unknown(self):
        card = CcSwitchStatusAdapter(
            db_path=Path("/nonexistent/cc-switch.db")
        ).fetch()

        self.assertEqual(card.status, ProviderStatus.UNKNOWN)
        self.assertIsNone(card.primary)


class CcSwitchRegistryTests(unittest.TestCase):
    def test_default_registry_includes_cc_switch(self):
        from openusage_bar.providers.builtins import default_registry

        registry = default_registry(
            clock=lambda: datetime.now(timezone.utc),
            keychain=object(),
        )
        bindings = registry.build([])

        self.assertIn("cc_switch", {binding.provider_id for binding in bindings})
