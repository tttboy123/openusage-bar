from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock

from openusage_bar.activity_records import BalanceObservation
from openusage_bar.activity_store import ActivityStore
from openusage_bar.daily_history import ActivityCollector
from openusage_bar.local_api import LocalAPIRouter
from openusage_bar.models import Overview
from openusage_bar.providers.contracts import BalanceFetchFailure, BalanceFetchSuccess
from openusage_bar.query import QueryService, to_wire


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


def balance(
    *,
    observed_at: str = "2026-07-29T12:00:00Z",
    available: str = "123.45",
) -> BalanceObservation:
    return BalanceObservation(
        record_id="moonshot-main.balance",
        observed_at=observed_at,
        provider_id="moonshot-main",
        account_ref="main",
        currency="CNY",
        available=available,
        voucher="20",
        cash="103.45",
        state="ok",
        quality="direct",
        stale=False,
        source_id="moonshot.balance",
    )


class BalancePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ActivityStore(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_balance_is_revisioned_and_older_observations_do_not_replace_last_good(self):
        first = self.store.record_balance(balance())
        unchanged = self.store.record_balance(
            balance(observed_at="2026-07-29T12:05:00Z")
        )
        updated = self.store.record_balance(
            balance(observed_at="2026-07-29T12:10:00Z", available="100")
        )
        older = self.store.record_balance(
            balance(observed_at="2026-07-29T11:00:00Z", available="999")
        )

        self.assertEqual(first.revision, 1)
        self.assertEqual(unchanged.revision, 1)
        self.assertEqual(updated.revision, 2)
        self.assertEqual(older.available, "100")
        self.assertEqual(
            [row.record_type for row in self.store.changes(0)],
            ["balance", "balance"],
        )

    def test_failure_marks_source_stale_without_erasing_balance(self):
        collector = ActivityCollector(
            self.store, Mock(), official_importers={}, clock=lambda: NOW
        )
        success = BalanceFetchSuccess((balance(),))
        self.assertTrue(collector.refresh(
            Overview([]),
            balance_results=(
                ("moonshot-main", "moonshot.balance", success),
            ),
        ))
        self.assertTrue(collector.refresh(
            Overview([]),
            balance_results=(
                (
                    "moonshot-main",
                    "moonshot.balance",
                    BalanceFetchFailure("rate_limited"),
                ),
            ),
        ))

        stale_balance = self.store.balance_states()[0]
        self.assertEqual(stale_balance.available, "123.45")
        self.assertTrue(stale_balance.stale)
        self.assertEqual(stale_balance.revision, 2)
        self.assertTrue(to_wire(QueryService(
            self.store, clock=lambda: NOW
        ).balances())["balances"][0]["stale"])
        status = self.store.source_statuses()[0]
        self.assertEqual(status.state, "temporarily_unavailable")
        self.assertEqual(status.error_code, "rate_limited")
        self.assertEqual(status.last_success_at, "2026-07-29T12:00:00.000000Z")

        self.assertTrue(collector.refresh(
            Overview([]),
            balance_results=(
                ("moonshot-main", "moonshot.balance", success),
            ),
        ))
        recovered = self.store.balance_states()[0]
        self.assertFalse(recovered.stale)
        self.assertEqual(recovered.available, "123.45")
        self.assertEqual(recovered.revision, 3)

    def test_query_snapshot_and_balances_route_keep_balance_out_of_capacity(self):
        self.store.record_balance(balance())
        query = QueryService(self.store, clock=lambda: NOW)

        balances = to_wire(query.balances())
        snapshot = to_wire(query.resource_snapshot(date(2026, 7, 29)))
        capacity = to_wire(query.capacity())
        routed = LocalAPIRouter(query)._payload("/v1/balances", {})

        self.assertEqual(balances, routed)
        self.assertEqual(balances["balances"][0]["available"], "123.45")
        self.assertEqual(balances["balances"][0]["currency"], "CNY")
        self.assertEqual(snapshot["balances"], balances["balances"])
        self.assertEqual(capacity["providers"], [])
        self.assertEqual(snapshot["quotaWindows"], [])

    def test_snapshot_quota_hub_aggregates_balances_with_provenance(self):
        self.store.record_balance(balance())
        self.store.record_balance(
            BalanceObservation(
                record_id="deepseek.balance",
                observed_at="2026-07-29T12:00:00Z",
                provider_id="deepseek",
                account_ref="deepseek",
                currency="CNY",
                available="10.50",
                voucher=None,
                cash=None,
                state="ok",
                quality="direct",
                stale=False,
                source_id="deepseek.balance",
            )
        )
        self.store.record_balance(
            BalanceObservation(
                record_id="moonshot-usd.balance",
                observed_at="2026-07-29T12:00:00Z",
                provider_id="moonshot-usd",
                account_ref="usd",
                currency="USD",
                available="5",
                voucher=None,
                cash=None,
                state="ok",
                quality="direct",
                stale=False,
                source_id="moonshot.balance",
            )
        )
        query = QueryService(self.store, clock=lambda: NOW)

        hub = to_wire(query.resource_snapshot(date(2026, 7, 29)))["quotaHub"]

        self.assertEqual(len(hub), 2)
        cny = next(item for item in hub if item["currency"] == "CNY")
        usd = next(item for item in hub if item["currency"] == "USD")
        self.assertEqual(cny["totalAvailable"], "133.95")
        self.assertEqual(cny["providerCount"], 2)
        self.assertIn(
            ["deepseek", "deepseek.balance", "direct"],
            cny["provenance"],
        )
        self.assertEqual(usd["totalAvailable"], "5")
        self.assertEqual(usd["providerCount"], 1)

    def test_balance_value_rejects_negative_or_unknown_numeric_facts(self):
        with self.assertRaises(ValueError):
            balance(available="-1")
        with self.assertRaises(ValueError):
            BalanceObservation(
                **(balance().__dict__ | {"state": "unknown"})
            )


if __name__ == "__main__":
    unittest.main()
