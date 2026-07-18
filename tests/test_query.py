from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from openusage_bar.activity_store import (
    ActivityStore,
    DailyCostRow,
    DailyUsageRow,
    ProviderInstance,
    QuotaObservation,
)
from openusage_bar.query import QueryService, to_wire


NOW = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)


def usage(
    *,
    day: str = "2026-07-14",
    provider_id: str = "codex",
    model_id: str = "gpt-5.5",
    total_tokens: int = 74_200_000,
    account_ref: str = "",
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
        cost_amount="4.2371",
        cost_currency="USD",
        cost_basis="price_table_estimated",
        quality="derived",
        imported_at="2026-07-14T09:00:00Z",
    )


def cost(
    *,
    day: str = "2026-07-14",
    provider_id: str = "openai",
    account_ref: str = "",
    currency: str = "USD",
    cost_kind: str = "actual",
    amount: str = "12.34",
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
        imported_at="2026-07-14T09:00:00Z",
    )


def quota(
    record_id: str,
    provider_id: str,
    ratio: float | None,
    *,
    account_ref: str = "primary",
    observed_at: str = "2026-07-14T09:00:00Z",
    resets_at: str | None = "2026-07-14T12:00:00Z",
    quota_name: str = "Five hour",
    state: str = "ok",
    stale: bool = False,
) -> QuotaObservation:
    return QuotaObservation(
        record_id=record_id,
        observed_at=observed_at,
        provider_id=provider_id,
        account_ref=account_ref,
        quota_name=quota_name,
        unit="percent",
        used=None if ratio is None else str((1 - ratio) * 100),
        quota_limit=None if ratio is None else "100",
        remaining=None if ratio is None else str(ratio * 100),
        remaining_ratio=ratio,
        resets_at=resets_at,
        period_start=None,
        period_end=None,
        state=state,
        quality="direct",
        stale=stale,
    )


class QueryServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "activity.sqlite3"
        self.store = ActivityStore(self.path)
        self.query = QueryService(self.store, clock=lambda: NOW)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_exact_summary_fixture_is_immutable_and_has_high_water_revision(self):
        self.store.replace_daily_usage("codex", "2026-07-14", [usage()])

        result = self.query.summary(date(2026, 7, 14))

        self.assertEqual(result.today_tokens, 74_200_000)
        self.assertEqual(result.data_revision, self.store.current_change_seq)
        self.assertEqual(result.generated_at, "2026-07-14T10:00:00Z")
        with self.assertRaises(FrozenInstanceError):
            result.today_tokens = 0
        self.assertEqual(
            set(to_wire(result)),
            {"schemaVersion", "dataRevision", "generatedAt", "todayTokens", "modelCount", "coveredDayCount"},
        )

    def test_capacity_selects_most_urgent_window_per_account_and_sorts_zero_before_null(self):
        self.store.record_quota(quota("minimax.weekly", "minimax", 0.7, quota_name="Weekly"))
        self.store.record_quota(quota("minimax.five_hour", "minimax", 0.18))
        self.store.record_quota(quota("codex.weekly", "codex", None, account_ref=""))
        self.store.record_quota(quota("kiro.monthly", "kiro_cli", 0.0, account_ref=""))

        result = self.query.capacity()

        self.assertEqual([row.provider_id for row in result.providers], ["kiro_cli", "minimax", "codex"])
        self.assertEqual(result.providers[1].record_id, "minimax.five_hour")
        self.assertEqual(result.providers[1].remaining_ratio, 0.18)
        self.assertEqual(result.providers[1].state, "ok")
        self.assertIsNone(result.providers[2].remaining_ratio)
        self.assertIsNone(result.providers[2].account_ref)
        self.assertIsNone(result.providers[0].estimated_cost_per_million_tokens)
        self.assertEqual(result.providers[0].constraints, ())

    def test_exact_capacity_fixture_sorts_minimax_first(self):
        self.store.record_quota(quota("minimax.five_hour", "minimax", 0.18))
        self.store.record_quota(quota("codex.weekly", "codex", 0.8, account_ref=""))
        capacity = self.query.capacity()
        self.assertEqual(capacity.providers[0].provider_id, "minimax")
        self.assertEqual(capacity.providers[0].remaining_ratio, 0.18)
        self.assertEqual(capacity.providers[0].state, "ok")

    def test_capacity_tie_breaks_by_reset_quota_name_and_record_id(self):
        self.store.record_quota(quota("a.z", "a", 0.2, resets_at="2026-07-14T15:00:00Z", quota_name="Zed"))
        self.store.record_quota(quota("a.b", "a", 0.2, resets_at="2026-07-14T13:00:00Z", quota_name="Beta"))
        self.store.record_quota(quota("a.a", "a", 0.2, resets_at="2026-07-14T13:00:00Z", quota_name="Alpha"))
        self.assertEqual(self.query.capacity().providers[0].record_id, "a.a")

    def test_capacity_preserves_stale_and_observation_freshness(self):
        self.store.record_quota(quota("minimax.five_hour", "minimax", 0.18, stale=True))
        row = self.query.capacity().providers[0]
        self.assertTrue(row.stale)
        self.assertEqual(row.freshness_seconds, 3600)

    def test_capacity_and_snapshot_publish_conservative_quota_scope(self):
        self.store.record_quota(quota("codex.weekly", "codex", 0.8))

        capacity = to_wire(self.query.capacity())
        snapshot = to_wire(self.query.resource_snapshot(date(2026, 7, 14)))

        for row in (capacity["providers"][0], snapshot["quotaWindows"][0]):
            self.assertEqual(row["sourceId"], "current.quota")
            self.assertEqual(row["quotaWindow"], "subscription")
            self.assertEqual(
                row["appliesTo"], {"kind": "account", "modelIds": []}
            )

    def test_activity_preserves_zero_missing_token_fields_cost_and_coverage(self):
        row = usage(total_tokens=0)
        row = DailyUsageRow(**(row.__dict__ | {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cost_amount": None}))
        self.store.replace_daily_usage("codex", "2026-07-14", [row])
        self.store.replace_daily_usage("codex", "2026-07-13", [])

        result = self.query.activity(date(2026, 7, 13), date(2026, 7, 14))

        self.assertEqual(result.rows[0].total_tokens, 0)
        self.assertIsNone(result.rows[0].reasoning_tokens)
        self.assertIsNone(result.rows[0].cost_amount)
        self.assertEqual(result.rows[0].source_id, "openusage.daily")
        self.assertEqual([(x.day, x.covered) for x in result.coverage], [("2026-07-13", True), ("2026-07-14", True)])
        self.assertEqual(
            [x.source_id for x in result.coverage],
            ["openusage.daily", "openusage.daily"],
        )
        wire = to_wire(result)
        self.assertEqual(wire["rows"][0]["sourceId"], "openusage.daily")
        self.assertEqual(wire["coverage"][0]["sourceId"], "openusage.daily")

    def test_activity_filters_stable_ids_and_orders_chronologically(self):
        self.store.replace_daily_usage("codex", "2026-07-13", [usage(day="2026-07-13", model_id="z")])
        self.store.replace_daily_usage("minimax", "2026-07-14", [usage(provider_id="minimax", model_id="m")])
        result = self.query.activity(
            date(2026, 7, 13), date(2026, 7, 14), provider_ids=("minimax",), model_ids=("m",)
        )
        self.assertEqual([(x.provider_id, x.model_id) for x in result.rows], [("minimax", "m")])
        self.assertEqual(
            [(x.day, x.covered) for x in result.coverage],
            [("2026-07-13", False), ("2026-07-14", True)],
        )
        for bad in (("bad/id",), ("",)):
            with self.assertRaises(ValueError):
                self.query.activity(date(2026, 7, 13), date(2026, 7, 14), provider_ids=bad)

    def test_activity_uses_all_time_known_scope_outside_requested_range(self):
        self.store.replace_daily_usage("codex", "2026-07-13", [])
        result = self.query.activity(date(2026, 7, 14), date(2026, 7, 14))
        self.assertEqual(
            [(row.provider_id, row.day, row.covered) for row in result.coverage],
            [("codex", "2026-07-14", False)],
        )
        unknown = self.query.activity(
            date(2026, 7, 14), date(2026, 7, 14), provider_ids=("unknown",)
        )
        self.assertEqual(
            [(row.provider_id, row.covered) for row in unknown.coverage],
            [("unknown", False)],
        )

    def test_activity_known_scope_includes_only_daily_source_status(self):
        self.store.record_source_status(
            "generic", "current.quota", "temporarily_unavailable", NOW,
            "quota_unavailable",
        )
        self.store.record_source_failure(
            "codex", "openusage.daily", "command_failed", NOW
        )
        result = self.query.activity(date(2026, 7, 14), date(2026, 7, 14))
        self.assertEqual(
            [(row.provider_id, row.covered) for row in result.coverage],
            [("codex", False)],
        )

        self.store.record_source_success(
            "minimax", "openusage.daily", NOW, freshness_seconds=300
        )
        result = self.query.activity(date(2026, 7, 14), date(2026, 7, 14))
        self.assertEqual(
            [(row.provider_id, row.covered) for row in result.coverage],
            [("codex", False), ("minimax", False)],
        )

    def test_costs_preserve_decimal_rows_and_known_zero_coverage(self):
        self.store.replace_daily_costs(
            "openai", "2026-07-14",
            [cost(currency="EUR", amount="0.10"), cost()],
        )
        self.store.replace_daily_costs("openai", "2026-07-13", [])

        result = self.query.costs(date(2026, 7, 13), date(2026, 7, 14))

        self.assertEqual(
            [(row.cost_kind, row.amount, row.currency) for row in result.rows],
            [("actual", "0.1", "EUR"), ("actual", "12.34", "USD")],
        )
        self.assertEqual(
            [(row.day, row.covered) for row in result.coverage],
            [("2026-07-13", True), ("2026-07-14", True)],
        )
        self.assertEqual(result.data_revision, self.store.current_change_seq)
        self.assertEqual(
            set(to_wire(result)),
            {"schemaVersion", "dataRevision", "generatedAt", "rows", "coverage"},
        )
        self.assertEqual(
            set(to_wire(result)["rows"][0]),
            {
                "day", "providerId", "accountRef", "costKind", "amount",
                "currency", "basis", "quality", "importedAt", "revision", "recordId",
            },
        )

    def test_costs_filter_provider_currency_and_expose_unknown_scope_as_missing(self):
        self.store.replace_daily_costs("openai", "2026-07-14", [cost()])
        self.store.replace_daily_costs(
            "anthropic", "2026-07-14",
            [cost(provider_id="anthropic", currency="EUR", amount="2")],
        )

        result = self.query.costs(
            date(2026, 7, 14), date(2026, 7, 14),
            provider_ids=("anthropic",), currencies=("EUR",),
        )
        self.assertEqual([(row.provider_id, row.currency) for row in result.rows], [("anthropic", "EUR")])
        self.assertEqual([(row.provider_id, row.covered) for row in result.coverage], [("anthropic", True)])

        unknown = self.query.costs(
            date(2026, 7, 14), date(2026, 7, 14), provider_ids=("unknown",)
        )
        self.assertEqual(unknown.rows, ())
        self.assertEqual([(row.provider_id, row.covered) for row in unknown.coverage], [("unknown", False)])

    def test_costs_validate_range_and_filter_identifiers(self):
        with self.assertRaises(ValueError):
            self.query.costs(date(2026, 7, 15), date(2026, 7, 14))
        with self.assertRaises(ValueError):
            self.query.costs(date(2020, 1, 1), date(2026, 7, 14))
        for bad in ("bad/id", ""):
            with self.assertRaises(ValueError):
                self.query.costs(
                    date(2026, 7, 14), date(2026, 7, 14),
                    provider_ids=(bad,),
                )
            with self.assertRaises(ValueError):
                self.query.costs(
                    date(2026, 7, 14), date(2026, 7, 14),
                    currencies=(bad,),
                )

    def test_date_range_limit_and_bool_validation(self):
        with self.assertRaises(ValueError):
            self.query.activity(date(2026, 7, 15), date(2026, 7, 14))
        with self.assertRaises(ValueError):
            self.query.activity(date(2020, 1, 1), date(2026, 7, 14))
        for value in (True, 0, 1001):
            with self.assertRaises(ValueError):
                self.query.capacity(limit=value)

    def test_quota_history_filters_provider_account_and_dates(self):
        self.store.record_quota(quota("minimax.five_hour", "minimax", 0.18))
        self.store.record_quota(quota("codex.weekly", "codex", 0.8, account_ref=""))
        result = self.query.quota_history(
            provider_id="minimax", account_ref="primary", from_time="2026-07-14T08:00:00Z", to_time="2026-07-14T10:00:00Z"
        )
        self.assertEqual([x.provider_id for x in result.snapshots], ["minimax"])
        self.assertEqual(result.snapshots[0].remaining_ratio, 0.18)
        with self.assertRaises(ValueError):
            self.query.quota_history(
                from_time="2020-01-01T00:00:00Z",
                to_time="2026-07-14T10:00:00Z",
            )
        with self.assertRaises(ValueError):
            self.query.quota_history(limit=True)

    def test_quota_history_limit_returns_newest_n_in_chronological_order(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for offset in range(1001):
            observed = (start + timedelta(hours=offset)).isoformat().replace("+00:00", "Z")
            self.store.record_quota(quota(
                "minimax.five_hour", "minimax", 0.18,
                observed_at=observed, resets_at=None,
            ))
        self.store.record_quota(quota(
            "codex.weekly", "codex", 0.8, account_ref="",
            observed_at=start.isoformat().replace("+00:00", "Z"), resets_at=None,
        ))
        result = self.query.quota_history(
            provider_id="minimax", account_ref="primary", limit=1000
        )
        self.assertEqual(len(result.snapshots), 1000)
        self.assertEqual(
            result.snapshots[0].observed_at,
            (start + timedelta(hours=1)).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        )
        self.assertEqual(
            result.snapshots[-1].observed_at,
            (start + timedelta(hours=1000)).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        )
        self.assertEqual(
            list(result.snapshots),
            sorted(result.snapshots, key=lambda item: (item.observed_at, item.snapshot_id)),
        )

    def test_source_status_is_typed_sorted_and_never_contains_credentials(self):
        self.store.record_source_failure("minimax", "current.quota", "auth_expired", NOW)
        result = self.query.source_status()
        self.assertEqual((result.sources[0].provider_id, result.sources[0].error_code), ("minimax", "auth_expired"))
        self.assertNotIn("credential", json.dumps(to_wire(result)))

    def test_provider_instances_are_stable_filtered_and_privacy_bounded(self):
        self.store.upsert_provider_instance(ProviderInstance(
            provider_id="future_agent", family_id="future_agent",
            display_name="Future Agent", category="api",
            credential_source="openusage", source_kind="openusage",
            observed_at="2026-07-14T09:02:00Z",
        ))
        self.store.upsert_provider_instance(ProviderInstance(
            provider_id="minimax-primary", family_id="minimax",
            display_name="MiniMax primary", category="subscription",
            credential_source="minimax_builtin_api", source_kind="builtin_api",
            observed_at="2026-07-14T09:01:00Z",
        ))

        result = self.query.provider_instances()
        wire = to_wire(result)

        self.assertEqual([row.provider_id for row in result.providers], [
            "future_agent", "minimax-primary",
        ])
        self.assertEqual(result.data_revision, self.store.high_water_cursor())
        self.assertEqual(set(wire), {
            "schemaVersion", "dataRevision", "generatedAt", "providers",
        })
        self.assertEqual(set(wire["providers"][0]), {
            "providerId", "familyId", "displayName", "category",
            "credentialSource", "sourceKind", "observedAt", "revision",
        })
        serialized = json.dumps(wire).lower()
        for forbidden in (
            "email", "account", "path", "token", "cookie", "payloadhash",
            "payload_hash", "change_seq", "record_type", "raw", "attributes",
        ):
            self.assertNotIn(forbidden, serialized)

        selected = self.query.provider_instances(("minimax-primary",))
        self.assertEqual([row.provider_id for row in selected.providers], ["minimax-primary"])
        for invalid in (("bad/id",), ("",)):
            with self.assertRaises(ValueError):
                self.query.provider_instances(invalid)

    def test_provider_instances_use_catalog_brand_for_redundant_raw_label(self):
        self.store.upsert_provider_instance(ProviderInstance(
            provider_id="minimax-generated", family_id="minimax",
            display_name="minimax", category="subscription",
            credential_source="minimax_builtin_api", source_kind="builtin_api",
            observed_at="2026-07-14T09:00:00Z",
        ))
        self.store.upsert_provider_instance(ProviderInstance(
            provider_id="minimax-main", family_id="minimax",
            display_name="MiniMax Main", category="subscription",
            credential_source="minimax_builtin_api", source_kind="builtin_api",
            observed_at="2026-07-14T09:01:00Z",
        ))
        self.store.upsert_provider_instance(ProviderInstance(
            provider_id="future_agent", family_id="future_agent",
            display_name="future_agent", category="api",
            credential_source="openusage", source_kind="openusage",
            observed_at="2026-07-14T09:02:00Z",
        ))
        self.store.upsert_provider_instance(ProviderInstance(
            provider_id="mistral-custom", family_id="mistral",
            display_name="miſtral", category="api",
            credential_source="openusage", source_kind="openusage",
            observed_at="2026-07-14T09:03:00Z",
        ))

        providers = {
            item.provider_id: item for item in self.query.provider_instances().providers
        }

        self.assertEqual(providers["minimax-generated"].display_name, "MiniMax")
        self.assertEqual(providers["minimax-main"].display_name, "MiniMax Main")
        self.assertEqual(providers["future_agent"].display_name, "future_agent")
        self.assertEqual(providers["mistral-custom"].display_name, "miſtral")
        self.assertEqual(providers["minimax-generated"].family_id, "minimax")

    def test_provider_instances_empty_ledger_keeps_envelope(self):
        result = self.query.provider_instances()
        self.assertEqual(result.providers, ())
        self.assertEqual(result.data_revision, 0)

    def test_provider_instance_metadata_revision_advances_result_cursor(self):
        first = self.store.upsert_provider_instance(ProviderInstance(
            provider_id="codex", family_id="codex", display_name="Codex",
            category="subscription", credential_source="openusage",
            source_kind="openusage", observed_at="2026-07-14T09:00:00Z",
        ))
        first_result = self.query.provider_instances()
        corrected = self.store.upsert_provider_instance(ProviderInstance(
            provider_id="codex", family_id="codex", display_name="OpenAI Codex",
            category="subscription", credential_source="openusage",
            source_kind="openusage", observed_at="2026-07-14T09:05:00Z",
        ))
        corrected_result = self.query.provider_instances()
        self.assertEqual((first.revision, corrected.revision), (1, 2))
        self.assertGreater(
            corrected_result.data_revision, first_result.data_revision
        )

    def test_resource_snapshot_keeps_every_fact_on_one_public_revision(self):
        self.store.replace_daily_usage(
            "codex", "2026-07-14", [usage(total_tokens=42)]
        )
        self.store.record_quota(quota(
            "minimax.five_hour", "minimax", 0.18,
            quota_name="Five hour",
        ))
        self.store.record_quota(quota(
            "minimax.weekly", "minimax", 0.72,
            quota_name="Weekly",
        ))
        self.store.upsert_provider_instance(ProviderInstance(
            provider_id="minimax-main", family_id="minimax",
            display_name="MiniMax Main", category="subscription",
            credential_source="minimax_builtin_api", source_kind="builtin_api",
            observed_at="2026-07-14T09:00:00Z",
        ))
        self.store.record_source_success(
            "minimax-main", "current.quota", NOW,
        )

        result = self.query.resource_snapshot(date(2026, 7, 14))
        wire = to_wire(result)

        self.assertEqual(result.data_revision, self.store.high_water_cursor())
        self.assertEqual(result.local_day, "2026-07-14")
        self.assertEqual(result.summary.today_tokens, 42)
        self.assertEqual(result.summary.model_count, 1)
        self.assertEqual(result.summary.covered_day_count, 1)
        self.assertEqual(
            [window.record_id for window in result.quota_windows],
            ["minimax.five_hour", "minimax.weekly"],
        )
        self.assertEqual([item.provider_id for item in result.providers], ["minimax-main"])
        self.assertEqual([item.provider_id for item in result.sources], ["minimax-main"])
        self.assertEqual(result.catalog_revision, "3059f1b")
        self.assertEqual(set(wire), {
            "schemaVersion", "dataRevision", "generatedAt", "localDay",
            "summary", "quotaWindows", "providers", "sources",
            "catalogRevision",
        })

    def test_change_pages_have_deterministic_next_cursor_and_snapshot_revision(self):
        self.store.replace_daily_usage("codex", "2026-07-13", [usage(day="2026-07-13")])
        first_cursor = self.store.current_change_seq
        self.store.replace_daily_usage("codex", "2026-07-14", [usage()])
        page = self.query.changes(after=0, limit=1)
        self.assertEqual(page.next_cursor, page.records[-1].change_seq)
        self.assertEqual(page.data_revision, self.store.current_change_seq)
        self.assertTrue(page.has_more)
        empty = self.query.changes(after=self.store.current_change_seq, limit=10)
        self.assertEqual(empty.next_cursor, self.store.current_change_seq)
        self.assertFalse(empty.has_more)
        self.assertLessEqual(page.next_cursor, first_cursor)
        for bad in (True, -1):
            with self.assertRaises(ValueError):
                self.query.changes(after=bad)

    def test_known_zero_coverage_changes_are_preserved_on_the_public_wire(self):
        self.store.replace_daily_usage("codex", "2026-07-18", [])
        self.store.replace_daily_costs("openai", "2026-07-18", [])

        wire = to_wire(self.query.changes(after=0))

        coverage = [
            record for record in wire["records"]
            if record["recordType"] in {
                "daily_coverage", "daily_cost_coverage",
            }
        ]
        self.assertEqual(
            [record["recordType"] for record in coverage],
            ["daily_coverage", "daily_cost_coverage"],
        )
        for record in coverage:
            self.assertEqual(record["operation"], "insert")
            self.assertIsInstance(record["payloadJson"], str)
            payload = json.loads(record["payloadJson"])
            self.assertEqual(payload["day"], "2026-07-18")
            self.assertNotIn("total_tokens", payload)

    def test_changes_reject_cursor_ahead_of_same_snapshot_high_water(self):
        self.store.replace_daily_usage("codex", "2026-07-14", [usage()])
        high_water = self.store.high_water_cursor()
        equal = self.query.changes(after=high_water)
        self.assertEqual((equal.records, equal.next_cursor), ((), high_water))
        with self.assertRaisesRegex(ValueError, "cursor"):
            self.query.changes(after=high_water + 1)

    def test_read_snapshots_are_consistent_with_concurrent_connection(self):
        self.store.replace_daily_usage("codex", "2026-07-14", [usage(total_tokens=1)])
        other = ActivityStore(self.path)
        try:
            original = self.store._row_to_daily
            wrote = False

            def interleaving(row, *, stored):
                nonlocal wrote
                if not wrote:
                    wrote = True
                    other.replace_daily_usage("codex", "2026-07-13", [usage(day="2026-07-13", total_tokens=2)])
                return original(row, stored=stored)

            self.store._row_to_daily = interleaving
            result = self.query.activity(date(2026, 7, 14), date(2026, 7, 14))
            self.assertEqual(result.data_revision, 2)
            self.assertLess(result.data_revision, other.current_change_seq)
        finally:
            other.close()

    def test_wire_serializer_is_compact_version_stable_and_tuple_safe(self):
        self.store.record_quota(quota("minimax.five_hour", "minimax", 0.18))
        wire = to_wire(self.query.capacity())
        self.assertEqual(wire["schemaVersion"], "1.0")
        self.assertIsInstance(wire["providers"], list)
        self.assertNotIn("schema_version", json.dumps(wire))

    def test_naive_query_clock_is_rejected(self):
        query = QueryService(self.store, clock=lambda: datetime(2026, 7, 14, 10, 0))
        with self.assertRaises(ValueError):
            query.summary(date(2026, 7, 14))


if __name__ == "__main__":
    unittest.main()
