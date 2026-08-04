import tempfile
import unittest
from datetime import date
from pathlib import Path

from openusage_bar.activity_records import DailyCostRow, DailyUsageRow
from openusage_bar.providers.contracts import (
    CostImportSuccess,
    ImportFailure,
    UsageImportSuccess,
)
from openusage_bar.reconciliation import (
    build_reconciliation_report,
    reconciliation_from_local_sources,
)


class ReconciliationReportTests(unittest.TestCase):
    def test_builds_day_rows_with_discrepancy_notes(self):
        codex = UsageImportSuccess(
            date(2026, 7, 1),
            date(2026, 7, 2),
            (
                DailyUsageRow(
                    day="2026-07-01",
                    provider_id="codex",
                    model_id="gpt-5.5",
                    input_tokens=100,
                    output_tokens=50,
                    cache_read_tokens=0,
                    cache_creation_tokens=0,
                    reasoning_tokens=None,
                    total_tokens=150,
                    cost_amount=None,
                    cost_currency=None,
                    cost_basis=None,
                    quality="upstream_declared",
                ),
            ),
        )
        cc_switch = CostImportSuccess(
            date(2026, 7, 1),
            date(2026, 7, 2),
            (
                DailyCostRow(
                    day="2026-07-01",
                    provider_id="cc_switch",
                    cost_kind="actual",
                    currency="USD",
                    amount="0",
                    basis="cc_switch.codex._codex_session",
                    quality="upstream_declared",
                    account_ref="cc_switch",
                ),
            ),
        )

        report = build_reconciliation_report(
            since=date(2026, 7, 1),
            until=date(2026, 7, 2),
            codex=codex,
            cc_switch=cc_switch,
            omniroute=ImportFailure("source_unavailable"),
        )

        self.assertEqual(len(report.rows), 2)
        first = report.rows[0]
        self.assertEqual(first.day, "2026-07-01")
        self.assertEqual(first.codex_token_total, 150)
        self.assertEqual(first.cc_switch_cost_usd, "0")
        self.assertTrue(first.cc_switch_covered)
        self.assertFalse(first.omniroute_covered)
        self.assertIn("cc_switch_zero_cost_with_codex_tokens", first.notes)
        self.assertIn("omniroute_unavailable", first.notes)
        self.assertIsNone(report.rows[1].codex_token_total)
        self.assertEqual(
            report.source_statuses,
            (
                ("codex", "codex.local_sessions", "ok"),
                ("cc_switch", "cc_switch.rollups", "ok"),
                ("omniroute", "omniroute.usage", "error"),
            ),
        )

    def test_rejects_inverted_range(self):
        with self.assertRaises(ValueError):
            build_reconciliation_report(
                since=date(2026, 7, 2),
                until=date(2026, 7, 1),
                codex=ImportFailure("source_unavailable"),
                cc_switch=ImportFailure("source_unavailable"),
                omniroute=ImportFailure("source_unavailable"),
            )

    def test_from_local_sources_reports_missing_sources_without_throwing(self):
        with tempfile.TemporaryDirectory() as directory:
            report = reconciliation_from_local_sources(
                since=date(2026, 7, 1),
                until=date(2026, 7, 2),
                session_roots=(Path(directory),),
                cc_switch_db=Path(directory) / "missing.db",
                omniroute_usage=Path(directory) / "missing-usage.json",
            )

            self.assertEqual(len(report.rows), 2)
            self.assertFalse(report.rows[0].cc_switch_covered)
            self.assertFalse(report.rows[0].omniroute_covered)
            self.assertIn("cc_switch_unavailable", report.rows[0].notes)
            self.assertIn("omniroute_unavailable", report.rows[0].notes)
