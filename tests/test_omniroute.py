import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from openusage_bar.omniroute import OmniRouteCostImporter
from openusage_bar.providers.contracts import CostImportSuccess, ImportFailure


class OmniRouteCostImporterTests(unittest.TestCase):
    def test_maps_cost_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            path.write_text(
                json.dumps(
                    {
                        "usage": [
                            {"date": "2026-07-05", "total_cost_usd": "0.42"},
                            {"date": "2026-07-06", "cost_usd": 1.25},
                            {"date": "2026-07-09", "other": 1},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = OmniRouteCostImporter(usage_path=path).fetch_costs(
                date(2026, 7, 1), date(2026, 7, 31)
            )

            self.assertIsInstance(result, CostImportSuccess)
            self.assertEqual(len(result.rows), 2)
            self.assertEqual(result.rows[0].provider_id, "omniroute")
            self.assertEqual(result.rows[0].amount, "0.42")
            self.assertEqual(result.rows[1].amount, "1.25")

    def test_missing_file_reports_unavailable(self):
        result = OmniRouteCostImporter(
            usage_path=Path("/nonexistent/usage.json")
        ).fetch_costs(date(2026, 7, 1), date(2026, 7, 31))

        self.assertEqual(result, ImportFailure("source_unavailable"))

    def test_malformed_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            path.write_text("{not-json", encoding="utf-8")

            result = OmniRouteCostImporter(usage_path=path).fetch_costs(
                date(2026, 7, 1), date(2026, 7, 31)
            )

            self.assertEqual(result, ImportFailure("import_failed"))

    def test_invalid_range_is_rejected(self):
        result = OmniRouteCostImporter(
            usage_path=Path("/nonexistent/usage.json")
        ).fetch_costs(date(2026, 7, 31), date(2026, 7, 1))

        self.assertEqual(result, ImportFailure("invalid_request"))


class OmniRouteRegistryTests(unittest.TestCase):
    def test_default_registry_includes_omniroute(self):
        from datetime import datetime, timezone

        from openusage_bar.providers.builtins import default_registry

        registry = default_registry(
            clock=lambda: datetime.now(timezone.utc),
            keychain=object(),
        )
        bindings = registry.build([])

        self.assertIn("omniroute", {binding.provider_id for binding in bindings})
