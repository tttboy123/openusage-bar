import json
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock


FIXTURES = Path(__file__).parent / "fixtures" / "openusage-export-v1"
NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class OpenUsageDiscoveryTests(unittest.TestCase):
    @staticmethod
    def probe():
        from openusage_bar.openusage_export_v1 import (
            ExportCapabilityProbe,
            decode_capabilities,
        )

        return ExportCapabilityProbe(
            True, decode_capabilities(fixture("capabilities.json"))
        )

    def test_fetches_bounded_provider_facts_without_cards(self):
        from openusage_bar.openusage_adapter import OpenUsageDiscoveryAdapter
        from openusage_bar.providers.contracts import DiscoveryFetchSuccess

        runner = Mock(return_value=subprocess.CompletedProcess(
            [], 0, fixture("providers.json"), "private stderr"
        ))
        result = OpenUsageDiscoveryAdapter(
            clock=lambda: NOW,
            runner=runner,
            openusage_path="/opt/bin/openusage",
            capability_probe=self.probe,
            environment={"PATH": "/usr/bin", "API_KEY": "must-not-pass"},
            path_exists=lambda _path: False,
        ).fetch_discovery()

        self.assertIsInstance(result, DiscoveryFetchSuccess)
        self.assertEqual(
            [observation.descriptor.provider_id for observation in result.observations],
            ["codex"],
        )
        self.assertEqual(result.observations[0].state, "available")
        self.assertEqual(result.observations[0].source_id, "openusage.discovery")
        self.assertEqual(result.observations[0].attribution.source_kind, "openusage")
        self.assertEqual(
            runner.call_args.args[0],
            [
                "/opt/bin/openusage", "export", "--output", "-", "--format", "json",
                "--contract", "openusage-export/v1", "--kind", "providers",
            ],
        )
        options = runner.call_args.kwargs
        self.assertFalse(options["shell"])
        self.assertIs(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["timeout"], 75)
        self.assertNotIn("API_KEY", options["env"])

    def test_partial_or_malformed_discovery_fails_closed(self):
        from openusage_bar.openusage_adapter import OpenUsageDiscoveryAdapter
        from openusage_bar.providers.contracts import DiscoveryFetchFailure

        partial = json.loads(fixture("providers.json"))
        partial["coverage"]["state"] = "partial"
        private = json.loads(fixture("providers.json"))
        private["rows"][0]["account_id"] = "private"
        for payload, code in ((partial, "partial_coverage"), (private, "invalid_export_v1")):
            with self.subTest(code=code):
                runner = Mock(return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps(payload), "private stderr"
                ))
                result = OpenUsageDiscoveryAdapter(
                    runner=runner,
                    capability_probe=self.probe,
                ).fetch_discovery()
                self.assertIsInstance(result, DiscoveryFetchFailure)
                self.assertEqual(result.error_code, code)

    def test_unsupported_contract_fails_without_running_discovery(self):
        from openusage_bar.openusage_adapter import OpenUsageDiscoveryAdapter
        from openusage_bar.openusage_export_v1 import ExportCapabilityProbe
        from openusage_bar.providers.contracts import DiscoveryFetchFailure

        runner = Mock()
        result = OpenUsageDiscoveryAdapter(
            runner=runner,
            capability_probe=lambda: ExportCapabilityProbe(False),
        ).fetch_discovery()
        self.assertIsInstance(result, DiscoveryFetchFailure)
        self.assertEqual(result.error_code, "unsupported_contract")
        runner.assert_not_called()

    def test_discovery_facts_persist_identity_and_source_health(self):
        from openusage_bar.activity_store import ActivityStore
        from openusage_bar.daily_history import ActivityCollector, DailyImportResult
        from openusage_bar.openusage_adapter import OpenUsageDiscoveryAdapter

        payload = json.loads(fixture("providers.json"))
        payload["rows"].append({
            "provider_id": "cursor",
            "state": "auth_required",
            "observed_at": "2026-07-31T23:59:00Z",
        })
        result = OpenUsageDiscoveryAdapter(
            runner=Mock(return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(payload), ""
            )),
            capability_probe=self.probe,
        ).fetch_discovery()
        importer = Mock()
        importer.fetch.return_value = DailyImportResult(False, (), "no_data")
        store = ActivityStore(":memory:")
        try:
            ActivityCollector(store, importer, clock=lambda: NOW).refresh(
                provider_families={
                    observation.descriptor.provider_id:
                    observation.descriptor.family_id
                    for observation in result.observations
                },
                discovery_observations=result.observations,
            )
            instances = {
                row.provider_id: row for row in store.provider_instances()
            }
            self.assertEqual(instances["codex"].family_id, "codex")
            self.assertEqual(instances["codex"].source_kind, "openusage")
            statuses = {
                (row.provider_id, row.source_id): row
                for row in store.source_statuses()
            }
            self.assertEqual(
                statuses[("codex", "openusage.discovery")].state, "ok"
            )
            self.assertEqual(
                statuses[("cursor", "openusage.discovery")].state,
                "auth_required",
            )
        finally:
            store.close()
