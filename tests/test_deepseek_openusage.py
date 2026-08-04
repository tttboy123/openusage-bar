from __future__ import annotations

import json
import subprocess
import unittest
from datetime import date, datetime, timezone

from openusage_bar.deepseek_openusage import OpenUsageDeepSeekAdapter
from openusage_bar.providers.builtins import default_registry
from openusage_bar.providers.contracts import (
    BalanceFetchSuccess,
    CostImportSuccess,
    ImportFailure,
)


FIXTURE = {
    "schema_version": "openusage-export/v1",
    "generated_at": "2026-08-04T04:34:14.000000Z",
    "snapshots": [
        {
            "provider_id": "deepseek",
            "account_id": "deepseek",
            "timestamp": "2026-08-04T04:34:12.503789Z",
            "status": "OK",
            "metrics": {
                "total_balance": {
                    "remaining": 305.98,
                    "unit": "CNY",
                    "window": "current",
                },
                "window_credit_spend": {
                    "used": 130.46999999999986,
                    "unit": "CNY",
                    "window": "30d",
                },
            },
            "attributes": {
                "currency": "CNY",
                "window_credit_spend_since": "2026-07-13T18:03:00Z",
            },
        }
    ],
}


def fake_runner(payload):
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload), stderr=""
        )

    return runner


class DeepSeekOpenUsageAdapterTests(unittest.TestCase):
    def _adapter(self, payload=FIXTURE):
        return OpenUsageDeepSeekAdapter(
            clock=lambda: datetime(2026, 8, 4, 6, 0, tzinfo=timezone.utc),
            runner=fake_runner(payload),
            environment={},
            path_exists=lambda _: True,
        )

    def test_balance_publishes_deepseek_balance_fact(self):
        adapter = self._adapter()
        result = adapter.fetch_balance()

        self.assertIsInstance(result.result, BalanceFetchSuccess)
        observation = result.result.observations[0]
        self.assertEqual(observation.provider_id, "deepseek")
        self.assertEqual(observation.currency, "CNY")
        self.assertEqual(observation.available, "305.98")
        self.assertEqual(observation.state, "ok")
        self.assertEqual(observation.quality, "derived")
        self.assertEqual(observation.source_id, "openusage.deepseek.balance")

    def test_costs_publishes_window_credit_spend_row(self):
        adapter = self._adapter()
        result = adapter.fetch_costs(date(2026, 7, 29), date(2026, 8, 4))

        self.assertIsInstance(result, CostImportSuccess)
        row = result.rows[0]
        self.assertEqual(row.day, "2026-08-04")
        self.assertEqual(row.provider_id, "deepseek")
        self.assertEqual(row.amount, "130.47")
        self.assertEqual(row.currency, "CNY")
        self.assertEqual(row.basis, "openusage.window_credit_spend")
        self.assertEqual(row.quality, "partial")

    def test_missing_snapshot_fails_without_exception(self):
        adapter = self._adapter({"schema_version": "v1", "snapshots": []})
        self.assertNotIsInstance(adapter.fetch_balance().result, BalanceFetchSuccess)
        self.assertIsInstance(
            adapter.fetch_costs(date(2026, 7, 29), date(2026, 8, 4)),
            ImportFailure,
        )

    def test_export_failure_is_sanitized(self):
        def failing_runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="boom")

        adapter = OpenUsageDeepSeekAdapter(
            clock=lambda: datetime(2026, 8, 4, tzinfo=timezone.utc),
            runner=failing_runner,
            environment={},
            path_exists=lambda _: True,
        )
        self.assertNotIsInstance(adapter.fetch_balance().result, BalanceFetchSuccess)
        self.assertIsInstance(
            adapter.fetch_costs(date(2026, 7, 29), date(2026, 8, 4)),
            ImportFailure,
        )

    def test_registry_builds_deepseek_binding_with_balance_and_cost_sources(self):
        registry = default_registry(
            clock=lambda: datetime.now(timezone.utc), keychain=object()
        )
        bindings = registry.build([])
        deepseek = next(b for b in bindings if b.provider_id == "deepseek")
        self.assertEqual(deepseek.family_id, "deepseek")
        self.assertEqual(len(deepseek.balance_sources), 1)
        self.assertEqual(len(deepseek.cost_sources), 1)
        self.assertTrue(hasattr(deepseek.balance_sources[0], "fetch_balance"))
        self.assertTrue(hasattr(deepseek.cost_sources[0], "fetch_costs"))


if __name__ == "__main__":
    unittest.main()
