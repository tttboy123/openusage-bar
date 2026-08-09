import json
import unittest
from concurrent.futures import ThreadPoolExecutor

from openusage_bar.gateway.accounts import (
    AccountCredentialError,
    AccountState,
    ProviderAccountRef,
    delete_provider_account_credential,
)
from openusage_bar.gateway.pools import (
    AccountCandidate,
    AccountPool,
    PoolMember,
    PoolSelector,
    PoolStrategy,
    account_pools_public_payload,
    validate_account_pools_public_payload,
)


def account(
    account_id: str,
    *,
    provider_id: str = "openai",
    alias: str | None = None,
) -> ProviderAccountRef:
    return ProviderAccountRef(
        provider_id=provider_id,
        account_id=account_id,
        alias=alias or account_id.replace("-", " ").title(),
        credential_account=f"{provider_id}.{account_id}.gateway-api-key",
    )


def candidate(
    value: ProviderAccountRef,
    *,
    health: float | None = 1.0,
    quota_ratio: float | None = 0.5,
    cost: float | None = 1.0,
    latency_ms: float | None = 100.0,
    reliability: float | None = 0.99,
    disabled: bool = False,
    cooldown: bool = False,
    model_id: str | None = "gpt-4.1",
    region: str | None = "global",
    backend_available: bool | None = None,
) -> AccountCandidate:
    return AccountCandidate(
        account=value,
        health=health,
        quota_ratio=quota_ratio,
        cost=cost,
        latency_ms=latency_ms,
        reliability=reliability,
        disabled=disabled,
        cooldown=cooldown,
        model_id=model_id,
        region=region,
        credential_backend_available=backend_available,
    )


class ProviderAccountRefTests(unittest.TestCase):
    def test_account_is_immutable_and_public_projection_is_renderer_safe(self) -> None:
        value = account("primary", alias="Work")

        self.assertEqual(
            value.to_public_dict(state=AccountState.UNKNOWN),
            {
                "alias": "Work",
                "displayId": value.display_id,
                "providerId": "openai",
                "state": "unknown",
            },
        )
        rendered = json.dumps(value.to_public_dict(), sort_keys=True).casefold()
        for forbidden in (
            "credential",
            "gateway-api-key",
            "account_id",
            "primary",
            "token",
            "path",
            "header",
        ):
            self.assertNotIn(forbidden, rendered)
        self.assertNotIn("gateway-api-key", repr(value))
        with self.assertRaises((AttributeError, TypeError)):
            value.alias = "Changed"  # type: ignore[misc]

    def test_account_uses_closed_identifier_alias_and_credential_boundaries(self) -> None:
        for field, kwargs in (
            ("provider", {"provider_id": "bad/provider"}),
            ("account", {"account_id": "bad account"}),
            ("alias", {"alias": ""}),
            ("credential", {"credential_account": ""}),
        ):
            with self.subTest(field=field):
                values = {
                    "provider_id": "openai",
                    "account_id": "primary",
                    "alias": "Work",
                    "credential_account": "openai.primary.gateway-api-key",
                    **kwargs,
                }
                with self.assertRaises((TypeError, ValueError)):
                    ProviderAccountRef(**values)

    def test_account_cleanup_uses_only_the_cross_platform_credential_boundary(self) -> None:
        class Boundary:
            def __init__(self, error: Exception | None = None) -> None:
                self.deleted: list[str] = []
                self.error = error

            def delete(self, account_name: str) -> None:
                self.deleted.append(account_name)
                if self.error is not None:
                    raise self.error

        value = account("primary", alias="Work")
        boundary = Boundary()
        delete_provider_account_credential(value, keychain=boundary)
        self.assertEqual(
            boundary.deleted,
            ["openai.primary.gateway-api-key"],
        )

        canary = "CANARY_PRIVATE_CREDENTIAL_ERROR"
        failing = Boundary(RuntimeError(canary))
        with self.assertRaises(AccountCredentialError) as caught:
            delete_provider_account_credential(value, keychain=failing)
        self.assertEqual(str(caught.exception), "account credential cleanup failed")
        self.assertNotIn(canary, repr(caught.exception))


class AccountPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.primary = account("primary")
        self.secondary = account("secondary", alias="Backup")

    def pool(
        self,
        strategy: PoolStrategy,
        *,
        members: tuple[PoolMember, ...] | None = None,
        **kwargs: object,
    ) -> AccountPool:
        return AccountPool(
            pool_id="daily-coding",
            revision=3,
            strategy=strategy,
            members=members
            or (
                PoolMember("primary", priority=10, weight=1),
                PoolMember("secondary", priority=20, weight=1),
            ),
            **kwargs,
        )

    def test_quota_aware_selects_verified_capacity_without_private_material(self) -> None:
        selection = PoolSelector().select(
            self.pool(PoolStrategy.QUOTA_AWARE),
            candidates=(
                candidate(self.primary, quota_ratio=0.02),
                candidate(self.secondary, quota_ratio=0.80),
            ),
            requested_provider_id="openai",
            requested_model_id="gpt-4.1",
            requested_region="global",
        )

        self.assertEqual(selection.selected_account_id, "secondary")
        self.assertEqual(selection.status, "selected")
        public = selection.to_public_dict()
        self.assertEqual(public["selectedAccountAlias"], "Backup")
        self.assertEqual(public["poolRevision"], 3)
        rendered = json.dumps(public, sort_keys=True).casefold()
        for forbidden in (
            "gateway-api-key",
            "credential",
            "account_id",
            "secondary",
            "token",
            "path",
            "header",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_unknown_facts_are_excluded_instead_of_becoming_zero_or_healthy(self) -> None:
        selection = PoolSelector().select(
            self.pool(PoolStrategy.QUOTA_AWARE),
            candidates=(
                candidate(self.primary, health=None, quota_ratio=None),
                candidate(self.secondary, health=1.0, quota_ratio=None),
            ),
            requested_provider_id="openai",
        )

        self.assertIsNone(selection.selected_account_id)
        self.assertEqual(selection.status, "unavailable")
        self.assertEqual(
            {item.reason for item in selection.exclusions},
            {"health_unknown", "quota_unknown"},
        )

    def test_fixed_first_honors_priority_after_disabled_and_cooldown(self) -> None:
        selection = PoolSelector().select(
            self.pool(PoolStrategy.FIXED_FIRST),
            candidates=(
                candidate(self.primary, disabled=True),
                candidate(self.secondary),
            ),
            requested_provider_id="openai",
        )
        self.assertEqual(selection.selected_account_id, "secondary")
        self.assertEqual(selection.exclusions[0].reason, "disabled")

        cooled = PoolSelector().select(
            self.pool(PoolStrategy.FIXED_FIRST),
            candidates=(
                candidate(self.primary, cooldown=True),
                candidate(self.secondary),
            ),
            requested_provider_id="openai",
        )
        self.assertEqual(cooled.selected_account_id, "secondary")
        self.assertEqual(cooled.exclusions[0].reason, "cooldown")

        backend_down = PoolSelector().select(
            self.pool(PoolStrategy.FIXED_FIRST),
            candidates=(
                candidate(self.primary, backend_available=False),
                candidate(self.secondary),
            ),
            requested_provider_id="openai",
        )
        self.assertEqual(backend_down.selected_account_id, "secondary")
        self.assertEqual(
            backend_down.exclusions[0].reason,
            "credential_backend_unavailable",
        )

    def test_sticky_reuses_only_an_eligible_member(self) -> None:
        pool = self.pool(PoolStrategy.STICKY)
        selector = PoolSelector()

        selected = selector.select(
            pool,
            candidates=(candidate(self.primary), candidate(self.secondary)),
            requested_provider_id="openai",
            sticky_account_id="secondary",
        )
        self.assertEqual(selected.selected_account_id, "secondary")

        fallback = selector.select(
            pool,
            candidates=(
                candidate(self.primary),
                candidate(self.secondary, cooldown=True),
            ),
            requested_provider_id="openai",
            sticky_account_id="secondary",
        )
        self.assertEqual(fallback.selected_account_id, "primary")

    def test_round_robin_is_thread_safe_and_weighted(self) -> None:
        selector = PoolSelector()
        pool = self.pool(
            PoolStrategy.ROUND_ROBIN,
            members=(
                PoolMember("primary", priority=10, weight=1),
                PoolMember("secondary", priority=20, weight=2),
            ),
        )
        candidates = (candidate(self.primary), candidate(self.secondary))

        with ThreadPoolExecutor(max_workers=12) as executor:
            selected = list(
                executor.map(
                    lambda _index: selector.select(
                        pool,
                        candidates=candidates,
                        requested_provider_id="openai",
                    ).selected_account_id,
                    range(60),
                )
            )

        self.assertEqual(selected.count("primary"), 20)
        self.assertEqual(selected.count("secondary"), 40)

    def test_cross_scope_fallback_requires_each_explicit_confirmation(self) -> None:
        other_provider = account(
            "other-provider",
            provider_id="anthropic",
            alias="Other Provider",
        )
        pool = self.pool(
            PoolStrategy.QUOTA_AWARE,
            members=(
                PoolMember("primary", priority=10, weight=1),
                PoolMember("other-provider", priority=20, weight=1),
            ),
        )
        candidates = (
            candidate(self.primary, health=0.0),
            candidate(
                other_provider,
                model_id="claude-sonnet",
                region="us",
                quota_ratio=0.9,
            ),
        )

        blocked = PoolSelector().select(
            pool,
            candidates=candidates,
            requested_provider_id="openai",
            requested_model_id="gpt-4.1",
            requested_region="global",
        )
        self.assertIsNone(blocked.selected_account_id)
        self.assertIn(
            "cross_provider_unconfirmed",
            {item.reason for item in blocked.exclusions},
        )

        provider_only = PoolSelector().select(
            self.pool(
                PoolStrategy.QUOTA_AWARE,
                members=pool.members,
                cross_provider_fallback=True,
            ),
            candidates=candidates,
            requested_provider_id="openai",
            requested_model_id="gpt-4.1",
            requested_region="global",
        )
        self.assertIsNone(provider_only.selected_account_id)
        self.assertIn(
            "cross_model_unconfirmed",
            {item.reason for item in provider_only.exclusions},
        )

        allowed = PoolSelector().select(
            self.pool(
                PoolStrategy.QUOTA_AWARE,
                members=pool.members,
                cross_provider_fallback=True,
                cross_model_fallback=True,
                cross_region_fallback=True,
            ),
            candidates=candidates,
            requested_provider_id="openai",
            requested_model_id="gpt-4.1",
            requested_region="global",
        )
        self.assertEqual(allowed.selected_account_id, "other-provider")

    def test_strategies_choose_only_closed_matching_facts(self) -> None:
        cases = (
            (PoolStrategy.COST, {"cost": (0.4, 0.2)}, "secondary"),
            (PoolStrategy.LATENCY, {"latency_ms": (90.0, 40.0)}, "secondary"),
            (PoolStrategy.RELIABILITY, {"reliability": (0.999, 0.95)}, "primary"),
        )
        for strategy, values, expected in cases:
            with self.subTest(strategy=strategy):
                key = next(iter(values))
                first, second = values[key]
                selection = PoolSelector().select(
                    self.pool(strategy),
                    candidates=(
                        candidate(self.primary, **{key: first}),
                        candidate(self.secondary, **{key: second}),
                    ),
                    requested_provider_id="openai",
                )
                self.assertEqual(selection.selected_account_id, expected)

    def test_pool_configuration_is_closed_and_bounded(self) -> None:
        for strategy in PoolStrategy:
            self.pool(strategy)

        invalid = (
            {"pool_id": "bad/pool"},
            {"revision": 0},
            {"strategy": "quota-aware"},
            {"members": ()},
            {"members": (PoolMember("primary", 10, 1),) * 2},
            {"members": tuple(PoolMember(f"acct-{i}", i, 1) for i in range(65))},
            {"cross_provider_fallback": 1},
        )
        for values in invalid:
            with self.subTest(values=values):
                kwargs = {
                    "pool_id": "daily-coding",
                    "revision": 1,
                    "strategy": PoolStrategy.FIXED_FIRST,
                    "members": (PoolMember("primary", 10, 1),),
                    **values,
                }
                with self.assertRaises((TypeError, ValueError)):
                    AccountPool(**kwargs)

        for weight in (0, 101, True):
            with self.subTest(weight=weight):
                with self.assertRaises((TypeError, ValueError)):
                    PoolMember("primary", priority=10, weight=weight)

    def test_public_account_pool_payload_matches_the_renderer_contract(self) -> None:
        pool = self.pool(PoolStrategy.QUOTA_AWARE)
        payload = account_pools_public_payload(
            accounts=(self.primary, self.secondary),
            pools=(pool,),
            candidates=(
                candidate(self.primary, quota_ratio=0.42),
                candidate(
                    self.secondary,
                    health=None,
                    quota_ratio=None,
                    cooldown=True,
                ),
            ),
        )

        self.assertEqual(
            payload,
            {
                "accounts": [
                    {
                        "alias": "Primary",
                        "displayId": self.primary.display_id,
                        "status": "ready",
                        "quota": {
                            "state": "available",
                            "remaining": None,
                            "limit": None,
                            "resetAt": None,
                        },
                        "cooldown": {"state": "inactive", "until": None},
                        "pools": [
                            {
                                "poolId": "daily-coding",
                                "priority": 10,
                                "weight": 1,
                            }
                        ],
                        "priority": 10,
                        "weight": 1,
                    },
                    {
                        "alias": "Backup",
                        "displayId": self.secondary.display_id,
                        "status": "cooldown",
                        "quota": {
                            "state": "unknown",
                            "remaining": None,
                            "limit": None,
                            "resetAt": None,
                        },
                        "cooldown": {"state": "active", "until": None},
                        "pools": [
                            {
                                "poolId": "daily-coding",
                                "priority": 20,
                                "weight": 1,
                            }
                        ],
                        "priority": 20,
                        "weight": 1,
                    },
                ]
            },
        )
        rendered = json.dumps(payload, sort_keys=True).casefold()
        self.assertNotIn("gateway-api-key", rendered)
        self.assertNotIn("credential", rendered)
        self.assertNotIn("account_id", rendered)

        hostile = json.loads(json.dumps(payload))
        hostile["accounts"][0]["displayId"] = (
            "openai.primary.gateway-api-key"
        )
        self.assertIsNone(validate_account_pools_public_payload(hostile))

        backend_down = account_pools_public_payload(
            accounts=(self.primary,),
            pools=(
                self.pool(
                    PoolStrategy.FIXED_FIRST,
                    members=(PoolMember("primary", priority=10, weight=1),),
                ),
            ),
            candidates=(candidate(self.primary, backend_available=False),),
        )
        self.assertEqual(
            backend_down["accounts"][0]["status"],
            "backend_unavailable",
        )


if __name__ == "__main__":
    unittest.main()
