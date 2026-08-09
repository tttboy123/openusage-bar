from __future__ import annotations

import json
import unittest
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ModuleNotFoundError:  # pragma: no cover - optional release test oracle
    Draft202012Validator = None

from openusage_bar.activity_store import (
    ActivityStore,
    BalanceObservation,
    DailyCostRow,
    DailyUsageRow,
    ProviderInstance,
    QuotaObservation,
)
from openusage_bar.local_api import LocalAPIRouter
from openusage_bar.provider_catalog import ObserverPlatformResolver, catalog
from openusage_bar.query import QueryService, to_wire
from scripts.generate_local_api_schema import render_schema


ROOT = Path(__file__).parents[1]
SCHEMA = ROOT / "openusage_bar/resources/local-api-v1.schema.json"
NOW = datetime(2026, 7, 18, 1, 0, tzinfo=timezone.utc)
ROUTE_PARAMETERS = {
    "/v1/health": {},
    "/v1/schema": {},
    "/v1/schema.json": {},
    "/v1/summary": {"today": "2026-07-18"},
    "/v1/snapshot": {"today": "2026-07-18"},
    "/v1/capabilities": {},
    "/v1/providers": {},
    "/v1/capacity": {},
    "/v1/activity/daily": {
        "from": "2026-07-18", "to": "2026-07-18"
    },
    "/v1/balances": {},
    "/v1/costs/daily": {
        "from": "2026-07-18", "to": "2026-07-18"
    },
    "/v1/quotas/history": {"limit": "10"},
    "/v1/sources/status": {},
    "/v1/changes": {"after": "0", "limit": "100"},
    "/v1/quick-connect": {},
}


def seeded_router(*, runtime_platform: str = "darwin") -> tuple[
    ActivityStore, LocalAPIRouter
]:
    store = ActivityStore(":memory:")
    store.replace_daily_usage("codex", "2026-07-18", [DailyUsageRow(
        day="2026-07-18", provider_id="codex", model_id="gpt-5.5",
        input_tokens=60, output_tokens=20, cache_read_tokens=20,
        cache_creation_tokens=0, reasoning_tokens=None, total_tokens=100,
        cost_amount=None, cost_currency=None, cost_basis=None, quality="direct",
        imported_at="2026-07-18T00:30:00Z",
    )])
    store.replace_daily_costs("openai", "2026-07-18", [DailyCostRow(
        day="2026-07-18", provider_id="openai", cost_kind="actual",
        currency="USD", amount="12.34", basis="provider_reported",
        quality="direct", imported_at="2026-07-18T00:30:00Z",
    )])
    store.record_quota(QuotaObservation(
        record_id="minimax.five_hour", observed_at="2026-07-18T00:30:00Z",
        provider_id="minimax", quota_name="Five hour", unit="percent",
        used="82", quota_limit="100", remaining="18", remaining_ratio=0.18,
        resets_at="2026-07-18T04:00:00Z", period_start=None, period_end=None,
        state="ok", quality="direct", stale=False,
    ))
    store.record_balance(BalanceObservation(
        record_id="openai.balance", observed_at="2026-07-18T00:30:00Z",
        provider_id="openai", currency="USD", available="25.00",
        voucher=None, cash="25.00", state="ok", quality="direct",
        stale=False,
    ))
    store.record_source_success("minimax", "current.quota", NOW)
    store.upsert_provider_instance(ProviderInstance(
        provider_id="minimax-primary", family_id="minimax",
        display_name="MiniMax primary", category="subscription",
        credential_source="minimax_builtin_api", source_kind="builtin_api",
        observed_at="2026-07-18T00:30:00Z",
    ))
    query = QueryService(store, clock=lambda: NOW)
    return store, LocalAPIRouter(
        query,
        clock=lambda: NOW,
        observer_platform=ObserverPlatformResolver(
            catalog, runtime_platform=runtime_platform
        ),
    )


def validate_snapshot(payload: dict[str, object]) -> None:
    required = {
        "schemaVersion", "dataRevision", "generatedAt", "localDay", "summary",
        "quotaWindows", "providers", "sources", "catalogRevision",
    }
    if not required <= set(payload) or payload.get("schemaVersion") != "1.0":
        raise ValueError("invalid snapshot envelope")
    if isinstance(payload.get("dataRevision"), bool) or not isinstance(payload.get("dataRevision"), int):
        raise ValueError("invalid revision")
    balances = payload.get("balances", [])
    if not isinstance(balances, list):
        raise ValueError("invalid balances")
    forbidden = ("secret", "password", "cookie", "token", "authorization")
    for value in (
        payload,
        *balances,
        *payload["quotaWindows"],
        *payload["providers"],
        *payload["sources"],
    ):
        if any(any(term in key.lower() for term in forbidden) for key in value):
            raise ValueError("private field")
    for window in payload["quotaWindows"]:
        if window["state"] == "unknown" and any(
            window[name] is not None
            for name in ("used", "quotaLimit", "remaining", "remainingRatio")
        ):
            raise ValueError("unknown quota has a value")


def validate_changes(payload: dict[str, object]) -> None:
    required = {"schemaVersion", "dataRevision", "generatedAt", "records", "nextCursor", "hasMore"}
    if set(payload) != required or not isinstance(payload.get("nextCursor"), int):
        raise ValueError("invalid changes cursor")


class LocalAPISchemaTests(unittest.TestCase):
    def test_generated_schema_is_current_and_draft_2020_12(self):
        self.assertEqual(json.loads(SCHEMA.read_text()), render_schema())
        self.assertEqual(render_schema()["$schema"], "https://json-schema.org/draft/2020-12/schema")

    @unittest.skipIf(
        Draft202012Validator is None,
        "jsonschema is unavailable for schema meta-validation",
    )
    def test_generated_schema_is_draft_2020_12_meta_valid(self):
        Draft202012Validator.check_schema(render_schema())

    def test_snapshot_balance_extension_remains_optional_in_local_api_v1(self):
        snapshot = next(
            branch
            for branch in render_schema()["oneOf"]
            if "summary" in branch.get("properties", {})
        )

        self.assertIn("balances", snapshot["properties"])
        self.assertNotIn("balances", snapshot["required"])

    def test_snapshot_allows_unknown_today_tokens_but_keeps_counts_numeric(self):
        snapshot = next(
            branch
            for branch in render_schema()["oneOf"]
            if "summary" in branch.get("properties", {})
        )
        properties = snapshot["properties"]["summary"]["properties"]

        self.assertEqual(properties["todayTokens"]["type"], ["integer", "null"])
        self.assertEqual(properties["modelCount"]["type"], "integer")
        self.assertEqual(properties["coveredDayCount"]["type"], "integer")

    def test_top_level_summary_has_nullable_tokens_and_zero_coverage_invariants(self):
        summary = next(
            branch
            for branch in render_schema()["oneOf"]
            if "todayTokens" in branch.get("properties", {})
        )

        self.assertEqual(
            summary["properties"]["todayTokens"]["type"], ["integer", "null"]
        )
        invariant = summary["allOf"][0]
        self.assertEqual(
            invariant["then"]["properties"],
            {"modelCount": {"const": 0}, "coveredDayCount": {"const": 0}},
        )
        self.assertEqual(
            invariant["else"]["then"]["properties"],
            {"todayTokens": {"const": 0}, "coveredDayCount": {"minimum": 1}},
        )

    def test_activity_schema_declares_token_counting_convention(self):
        activity = next(
            branch
            for branch in render_schema()["oneOf"]
            if "rows" in branch.get("properties", {})
        )
        row = activity["properties"]["rows"]["items"]
        candidates = [
            *row.get("oneOf", []),
            *row.get("anyOf", []),
            row,
        ]
        activity_row = next(
            candidate
            for candidate in candidates
            if "tokenCountingConvention" in candidate.get("properties", {})
            and "tokenCountingConvention" in candidate.get("required", [])
        )

        self.assertEqual(
            activity_row["properties"]["tokenCountingConvention"]["enum"],
            [
                "input_includes_cache",
                "components_disjoint",
                "provider_reported",
                "unknown",
            ],
        )
        self.assertIn("tokenCountingConvention", activity_row["required"])

    def test_schema_has_one_closed_branch_for_every_live_response_shape(self):
        branch_keys = {
            frozenset(branch.get("properties", {}))
            for branch in render_schema()["oneOf"]
        }
        expected = {
            frozenset({
                "schemaVersion", "dataRevision", "generatedAt",
                "todayTokens", "modelCount", "coveredDayCount",
            }),
            frozenset({
                "schemaVersion", "dataRevision", "generatedAt", "localDay",
                "summary", "balances", "quotaWindows", "quotaHub",
                "providers", "sources", "catalogRevision",
            }),
            frozenset({
                "schemaVersion", "dataRevision", "generatedAt", "balances",
            }),
            frozenset({
                "schemaVersion", "dataRevision", "generatedAt", "rows",
                "coverage",
            }),
            frozenset({
                "schemaVersion", "dataRevision", "generatedAt", "records",
                "nextCursor", "hasMore",
            }),
            frozenset({
                "schemaVersion", "dataRevision", "generatedAt", "providers",
            }),
            frozenset({
                "schemaVersion", "dataRevision", "generatedAt", "upstream",
                "observerPlatform", "providers",
            }),
            frozenset({
                "schemaVersion", "dataRevision", "generatedAt", "snapshots",
            }),
            frozenset({
                "schemaVersion", "dataRevision", "generatedAt", "sources",
            }),
            frozenset({
                "schemaVersion", "dataRevision", "generatedAt", "sources",
                "health",
            }),
            frozenset({
                "schemaVersion", "dataRevision", "generatedAt", "routes",
                "errorShape",
            }),
            frozenset({
                "schemaVersion", "dataRevision", "generatedAt", "schema",
            }),
            frozenset({"schemaVersion", "providers"}),
            frozenset({"error"}),
        }
        self.assertTrue(expected <= branch_keys, expected - branch_keys)

    @unittest.skipIf(
        Draft202012Validator is None,
        "jsonschema is unavailable for live Local API response validation",
    )
    def test_every_advertised_read_route_matches_the_current_machine_schema(
        self,
    ):
        store, router = seeded_router()
        self.addCleanup(store.close)
        self.assertEqual(set(ROUTE_PARAMETERS), set(LocalAPIRouter.ROUTES))
        validator = Draft202012Validator(render_schema())

        for route in LocalAPIRouter.ROUTES:
            errors = list(validator.iter_errors(
                router._payload(route, ROUTE_PARAMETERS[route])
            ))
            with self.subTest(route=route):
                self.assertEqual(len(errors), 0, f"{route}: {len(errors)} errors")

    @unittest.skipIf(
        Draft202012Validator is None,
        "jsonschema is unavailable for empty Local API response validation",
    )
    def test_empty_live_route_responses_match_exactly_one_schema_branch(self):
        store = ActivityStore(":memory:")
        self.addCleanup(store.close)
        router = LocalAPIRouter(
            QueryService(store, clock=lambda: NOW),
            clock=lambda: NOW,
            observer_platform=ObserverPlatformResolver(
                catalog, runtime_platform="darwin"
            ),
        )
        validator = Draft202012Validator(render_schema())

        for route in LocalAPIRouter.ROUTES:
            errors = list(validator.iter_errors(
                router._payload(route, ROUTE_PARAMETERS[route])
            ))
            with self.subTest(route=route):
                self.assertEqual(len(errors), 0, f"{route}: {len(errors)} errors")

    @unittest.skipIf(
        Draft202012Validator is None,
        "jsonschema is unavailable for platform-state validation",
    )
    def test_capability_schema_closes_platform_states_and_private_fields(self):
        validator = Draft202012Validator(render_schema())
        payloads = {}
        for runtime in ("darwin", "win32", "freebsd"):
            store, router = seeded_router(runtime_platform=runtime)
            try:
                payloads[runtime] = router._payload("/v1/capabilities", {})
            finally:
                store.close()
            self.assertEqual(
                list(validator.iter_errors(payloads[runtime])),
                [],
                runtime,
            )

        mutations = {}
        mutations["private top-level"] = deepcopy(payloads["darwin"])
        mutations["private top-level"]["debugToken"] = "redacted"
        mutations["private capability provider"] = deepcopy(
            payloads["darwin"]
        )
        mutations["private capability provider"]["providers"][0][
            "secretToken"
        ] = "redacted"
        mutations["private observer platform"] = deepcopy(payloads["darwin"])
        mutations["private observer platform"]["observerPlatform"][
            "debugToken"
        ] = "redacted"
        mutations["summary state-reason mismatch"] = deepcopy(
            payloads["darwin"]
        )
        mutations["summary state-reason mismatch"]["observerPlatform"][
            "reasonCode"
        ] = "runtime_platform_unknown"
        mutations["supported zero"] = deepcopy(payloads["darwin"])
        mutations["supported zero"]["observerPlatform"][
            "supportedSourceCount"
        ] = 0
        mutations["unsupported null"] = deepcopy(payloads["win32"])
        mutations["unsupported null"]["observerPlatform"][
            "supportedSourceCount"
        ] = None
        mutations["unknown zero"] = deepcopy(payloads["freebsd"])
        mutations["unknown zero"]["observerPlatform"][
            "supportedSourceCount"
        ] = 0
        mutations["count exceeds total"] = deepcopy(payloads["darwin"])
        mutations["count exceeds total"]["observerPlatform"][
            "supportedSourceCount"
        ] = 50
        mutations["source state-reason mismatch"] = deepcopy(
            payloads["darwin"]
        )
        first_source = mutations["source state-reason mismatch"][
            "providers"
        ][0]["sources"][0]
        first_source["platformSupport"] = {
            "state": "unknown",
            "reasonCode": "supported_sources_available",
        }

        quick_store, quick_router = seeded_router()
        try:
            mutations["quick-connect private field"] = quick_router._payload(
                "/v1/quick-connect", {}
            )
        finally:
            quick_store.close()
        mutations["quick-connect private field"]["providers"][0][
            "secretToken"
        ] = "redacted"

        for name, payload in mutations.items():
            with self.subTest(name=name):
                self.assertNotEqual(
                    list(validator.iter_errors(payload)),
                    [],
                    name,
                )

    def test_snapshot_contract_rejects_missing_revision_private_fields_and_unknown_values(self):
        store = ActivityStore(":memory:")
        try:
            fixture = to_wire(QueryService(store, clock=lambda: NOW).resource_snapshot(date(2026, 7, 18)))
        finally:
            store.close()
        validate_snapshot(fixture)
        for mutation in ("missing_schema", "bool_revision", "private", "unknown_value"):
            changed = deepcopy(fixture)
            if mutation == "missing_schema":
                changed.pop("schemaVersion")
            elif mutation == "bool_revision":
                changed["dataRevision"] = True
            elif mutation == "private":
                changed["apiToken"] = "redacted"
            else:
                changed["quotaWindows"] = [{"state": "unknown", "used": None, "quotaLimit": None, "remaining": None, "remainingRatio": 0}]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_snapshot(changed)

    def test_changes_require_cursor_and_accept_unknown_record_types(self):
        fixture = {
            "schemaVersion": "1.0", "dataRevision": 9,
            "generatedAt": "2026-07-18T01:00:00Z",
            "records": [{"recordType": "future_fact"}],
            "nextCursor": 9, "hasMore": False,
        }
        validate_changes(fixture)
        changed = dict(fixture)
        changed.pop("nextCursor")
        with self.assertRaises(ValueError):
            validate_changes(changed)


if __name__ == "__main__":
    unittest.main()
