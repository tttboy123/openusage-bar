import unittest

from tests.test_routing_engine import request, target


def quota(
    record_id: str,
    *,
    provider_id: str = "openai",
    account_ref: str | None = "account-1",
    remaining_ratio: float | None = 0.8,
    state: str = "ok",
    stale: bool = False,
    source_id: str = "openai.quota",
    applies_to_kind: str = "account",
    model_ids: tuple[str, ...] = (),
    price: str | None = None,
):
    from openusage_bar.query import CapacityProvider, QuotaAppliesTo

    return CapacityProvider(
        record_id=record_id,
        provider_id=provider_id,
        account_ref=account_ref,
        quota_name="Subscription",
        unit="percent",
        used=None,
        quota_limit=None,
        remaining=None,
        remaining_ratio=remaining_ratio,
        resets_at="2026-08-03T00:00:00Z",
        period_start="2026-08-02T00:00:00Z",
        period_end="2026-08-03T00:00:00Z",
        observed_at="2026-08-02T12:00:00Z",
        freshness_seconds=300,
        state=state,
        quality="provider_reported",
        stale=stale,
        revision=1,
        source_id=source_id,
        quota_window="subscription",
        applies_to=QuotaAppliesTo(applies_to_kind, model_ids),
        estimated_cost_per_million_tokens=price,
    )


def source(
    source_id: str,
    *,
    provider_id: str = "openai",
    state: str = "ok",
    error_code: str | None = None,
):
    from openusage_bar.query import SourceStatusItem

    return SourceStatusItem(
        provider_id=provider_id,
        source_id=source_id,
        state=state,
        last_attempt_at="2026-08-02T12:00:00Z",
        last_success_at=("2026-08-02T12:00:00Z" if state == "ok" else None),
        stale_at=None,
        error_code=error_code,
    )


def balance(
    record_id: str,
    *,
    provider_id: str = "openai",
    account_ref: str | None = "account-1",
    currency: str = "CNY",
    available: str | None = "5.00",
    state: str = "ok",
    stale: bool = False,
    source_id: str = "openai.balance",
):
    from openusage_bar.query import BalanceItem

    return BalanceItem(
        record_id=record_id,
        provider_id=provider_id,
        account_ref=account_ref,
        currency=currency,
        available=available,
        voucher=None,
        cash=None,
        observed_at="2026-08-02T12:00:00Z",
        freshness_seconds=300,
        state=state,
        quality="provider_reported",
        stale=stale,
        revision=1,
        source_id=source_id,
    )


def snapshot(*, quotas=(), balances=(), sources=()):
    from openusage_bar.query import ResourceSnapshotResult, SnapshotSummary

    return ResourceSnapshotResult(
        schema_version="1.0",
        data_revision=60_000,
        generated_at="2026-08-02T12:00:00Z",
        local_day="2026-08-02",
        summary=SnapshotSummary(None, 0, 0),
        balances=tuple(balances),
        quota_windows=tuple(quotas),
        providers=(),
        sources=tuple(sources),
        catalog_revision="fixture",
    )


def runtime_group(
    *,
    provider_id: str = "openai",
    model_id: str = "gpt-5",
    scope_ref: str = "anon_0123456789abcdef",
    observation_count: int = 10,
    completed: int = 8,
    errors: int = 2,
):
    from openusage_bar.runtime_store import RuntimeCostTotal, RuntimeLatencySummary, RuntimeSummaryGroup

    return RuntimeSummaryGroup(
        provider_id=provider_id,
        model_id=model_id,
        scope_ref=scope_ref,
        observation_count=observation_count,
        input_tokens=8_000,
        output_tokens=2_000,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        reasoning_tokens=None,
        total_tokens=10_000,
        token_counting_conventions=("input_includes_cache",),
        status_counts=(("completed", completed), ("error", errors)),
        cost_coverage_state="complete",
        costs=(RuntimeCostTotal("usd", 10_000),),
        latency=RuntimeLatencySummary(10, 900, 1_200, 8, 100, 180),
    )


def runtime_summary(*, groups=(), coverage_state: str = "complete", omitted: int = 0):
    from openusage_bar.runtime_store import RuntimeLatencySummary, RuntimeSummary

    return RuntimeSummary(
        schema_version=1,
        runtime_revision=120,
        generated_at="2026-08-02T12:00:00Z",
        window_start="2026-08-02T11:00:00Z",
        window_end="2026-08-02T12:00:00Z",
        coverage_state=coverage_state,
        omitted_group_count=omitted,
        observation_count=sum(group.observation_count for group in groups),
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        reasoning_tokens=None,
        total_tokens=0,
        token_counting_conventions=(),
        status_counts=(),
        cost_coverage_state="missing",
        costs=(),
        latency=RuntimeLatencySummary(0, None, None, 0, None, None),
        groups=tuple(groups),
    )


class RoutingFactTests(unittest.TestCase):
    def test_uses_most_constrained_applicable_quota_and_exact_runtime_scope(self):
        from openusage_bar.routing_facts import build_target_facts

        facts = build_target_facts(
            targets=(target("openai.work.gpt-5"),),
            route_request=request(),
            resource_snapshot=snapshot(
                quotas=(
                    quota("weekly", remaining_ratio=0.8, price="2.5"),
                    quota(
                        "model-window",
                        remaining_ratio=0.2,
                        source_id="openai.model-quota",
                        applies_to_kind="model",
                        model_ids=("gpt-5",),
                        price="3",
                    ),
                    quota(
                        "other-model",
                        remaining_ratio=0.01,
                        applies_to_kind="model",
                        model_ids=("gpt-4",),
                    ),
                ),
                sources=(source("openai.quota"), source("openai.model-quota")),
            ),
            runtime_summary=runtime_summary(groups=(runtime_group(),)),
            available_connections={"connection-1"},
        )

        self.assertEqual(len(facts), 1)
        value = facts[0]
        self.assertEqual(value.connection_state, "available")
        self.assertEqual(value.source_state, "ok")
        self.assertEqual(value.resource_state, "complete")
        self.assertEqual(value.headroom_bp, 2_000)
        self.assertEqual(value.runtime_state, "complete")
        self.assertEqual(value.observation_count, 10)
        self.assertEqual(value.error_count, 2)
        self.assertEqual(value.duration_p95_ms, 1_200)
        self.assertEqual(value.ttft_p95_ms, 180)
        self.assertEqual(value.estimated_cost_micros, 48_000)

    def test_unknown_and_stale_windows_fail_closed_without_turning_zero_into_missing(self):
        from openusage_bar.routing_facts import build_target_facts

        cases = (
            (
                (quota("known"), quota("unknown", remaining_ratio=None, state="unknown")),
                "partial",
                8_000,
            ),
            ((quota("stale", stale=True),), "stale", 8_000),
            ((quota("zero", remaining_ratio=0.0),), "complete", 0),
            ((quota("below-floor", remaining_ratio=0.04999),), "complete", 499),
            ((), "missing", None),
        )
        for quotas, expected_state, expected_headroom in cases:
            with self.subTest(state=expected_state, headroom=expected_headroom):
                source_ids = {row.source_id for row in quotas}
                values = build_target_facts(
                    targets=(target("candidate"),),
                    route_request=request(),
                    resource_snapshot=snapshot(
                        quotas=quotas,
                        sources=tuple(source(value) for value in source_ids),
                    ),
                    runtime_summary=None,
                    available_connections={"connection-1"},
                )
                self.assertEqual(values[0].resource_state, expected_state)
                self.assertEqual(values[0].headroom_bp, expected_headroom)

    def test_does_not_cross_account_connection_model_or_runtime_scope(self):
        from openusage_bar.routing_facts import build_target_facts

        values = build_target_facts(
            targets=(target("candidate"),),
            route_request=request(),
            resource_snapshot=snapshot(
                quotas=(quota("other-account", account_ref="account-2"),),
                sources=(source("openai.quota"),),
            ),
            runtime_summary=runtime_summary(
                groups=(runtime_group(scope_ref="anon_abcdef0123456789"),),
                coverage_state="partial",
                omitted=1,
            ),
            available_connections=set(),
        )

        self.assertEqual(values[0].connection_state, "unavailable")
        self.assertEqual(values[0].resource_state, "missing")
        self.assertEqual(values[0].source_state, "unknown")
        self.assertEqual(values[0].runtime_state, "partial")
        self.assertEqual(values[0].observation_count, 0)

    def test_authentication_failure_is_preserved_and_results_are_canonical(self):
        from openusage_bar.routing_facts import build_target_facts

        values = build_target_facts(
            targets=(target("z-target"), target("a-target")),
            route_request=request(),
            resource_snapshot=snapshot(
                quotas=(quota("quota"),),
                sources=(source("openai.quota", state="temporarily_unavailable", error_code="auth_required"),),
            ),
            runtime_summary=None,
            available_connections={"connection-1"},
        )

        self.assertEqual([value.target_id for value in values], ["a-target", "z-target"])
        self.assertTrue(all(value.source_state == "authentication_failed" for value in values))

    def test_balance_mode_preserves_native_currency_and_floors_microunits(self):
        from openusage_bar.routing_facts import build_target_facts

        values = build_target_facts(
            targets=(target(
                "moonshot.main.kimi",
                resource_mode="balance",
                balance_currency="CNY",
                cost_currency="CNY",
                input_cost_micros_per_million=2_000_000,
                output_cost_micros_per_million=8_000_000,
            ),),
            route_request=request(),
            resource_snapshot=snapshot(
                balances=(balance("moonshot.balance", available="5.0000009"),),
                sources=(source("openai.balance"),),
            ),
            runtime_summary=None,
            available_connections={"connection-1"},
        )

        value = values[0]
        self.assertEqual(value.resource_mode, "balance")
        self.assertEqual(value.resource_state, "complete")
        self.assertIsNone(value.headroom_bp)
        self.assertEqual(value.balance_micros, 5_000_000)
        self.assertEqual(value.balance_currency, "CNY")
        self.assertEqual(value.estimated_cost_micros, 56_000)
        self.assertEqual(value.estimated_cost_currency, "CNY")
        self.assertEqual(value.source_state, "ok")

    def test_balance_unknown_stale_duplicate_or_wrong_currency_fails_closed(self):
        from openusage_bar.routing_facts import build_target_facts

        cases = (
            ((balance("unknown", available=None, state="unknown"),), "missing"),
            ((balance("stale", stale=True),), "stale"),
            ((balance("a"), balance("b")), "partial"),
            ((balance("usd", currency="USD"),), "missing"),
        )
        for balances, expected in cases:
            with self.subTest(expected=expected):
                values = build_target_facts(
                    targets=(target(
                        "balance-target",
                        resource_mode="balance",
                        balance_currency="CNY",
                        cost_currency="CNY",
                    ),),
                    route_request=request(),
                    resource_snapshot=snapshot(
                        balances=balances,
                        sources=tuple(source(row.source_id) for row in balances),
                    ),
                    runtime_summary=None,
                    available_connections={"connection-1"},
                )
                self.assertEqual(values[0].resource_state, expected)


if __name__ == "__main__":
    unittest.main()
