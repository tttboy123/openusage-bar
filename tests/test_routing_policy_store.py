from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from openusage_bar.routing_policy import RoutePolicy, built_in_policy
from openusage_bar.routing_policy_store import (
    RoutingPolicyConfigError,
    RoutingPolicyStore,
)


def custom_policy(*, policy_id: str = "custom_coding", revision: int = 1) -> RoutePolicy:
    return RoutePolicy(
        policy_id=policy_id,
        revision=revision,
        reliability_weight=45,
        headroom_weight=25,
        latency_weight=20,
        cost_weight=10,
        min_headroom_bp=1_000,
        max_error_rate_bp=1_500,
        min_runtime_samples=3,
        latency_reference_ms=8_000,
        cost_reference_micros=2_000,
        cost_currency="USD",
        min_balance_micros=2_000_000,
        balance_reference_micros=25_000_000,
        unknown_penalty=3_000,
        require_cost=False,
        require_runtime=False,
        allowed_execution_classes=frozenset({"direct_api", "openai_compatible"}),
        allowed_privacy_classes=frozenset({"direct_provider"}),
    )


class RoutingPolicyStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "routing-policies.json"
        self.store = RoutingPolicyStore(self.path)

    def test_round_trips_custom_policy_in_a_private_atomic_document(self) -> None:
        self.store.save((custom_policy(),), revision=1)

        loaded = self.store.load()

        self.assertEqual(loaded.revision, 1)
        self.assertEqual(loaded.policies, (custom_policy(),))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("prompt", self.path.read_text(encoding="utf-8").lower())

    def test_rejects_builtin_override_duplicate_and_non_private_file(self) -> None:
        with self.assertRaises(RoutingPolicyConfigError):
            self.store.save((built_in_policy("reliable"),), revision=1)
        with self.assertRaises(RoutingPolicyConfigError):
            self.store.save((custom_policy(), custom_policy()), revision=1)

        self.store.save((custom_policy(),), revision=1)
        os.chmod(self.path, 0o644)
        with self.assertRaises(RoutingPolicyConfigError):
            self.store.load()

    def test_rejects_invalid_public_policy_identifier(self) -> None:
        with self.assertRaises(ValueError):
            custom_policy(policy_id="person@example.com")


if __name__ == "__main__":
    unittest.main()
