from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from openusage_bar.routing_preferences import (
    RoutingPreferences,
    RoutingPreferencesConfigError,
    RoutingPreferencesStore,
)


class RoutingPreferencesStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "routing-preferences.json"
        self.store = RoutingPreferencesStore(self.path)

    def test_missing_configuration_has_safe_stable_defaults(self) -> None:
        self.assertEqual(
            self.store.load(),
            RoutingPreferences(
                revision=0,
                decision_api_enabled=True,
                default_policy_id="reliable",
            ),
        )
        self.assertFalse(self.path.exists())

    def test_round_trip_is_private_atomic_and_monotonic(self) -> None:
        value = RoutingPreferences(
            revision=1,
            decision_api_enabled=False,
            default_policy_id="custom_coding",
        )
        self.store.save(value)

        self.assertEqual(self.store.load(), value)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("secret", self.path.read_text(encoding="utf-8").lower())

        with self.assertRaises(RoutingPreferencesConfigError):
            self.store.save(value)

    def test_rejects_unknown_duplicate_private_and_invalid_values(self) -> None:
        cases = [
            b'{"schemaVersion":1,"revision":1,"decisionApiEnabled":true,'
            b'"defaultPolicyId":"reliable","prompt":"private"}',
            b'{"schemaVersion":1,"revision":1,"revision":2,'
            b'"decisionApiEnabled":true,"defaultPolicyId":"reliable"}',
            b'{"schemaVersion":1,"revision":true,"decisionApiEnabled":true,'
            b'"defaultPolicyId":"reliable"}',
            b'{"schemaVersion":1,"revision":1,"decisionApiEnabled":1,'
            b'"defaultPolicyId":"reliable"}',
            b'{"schemaVersion":1,"revision":1,"decisionApiEnabled":true,'
            b'"defaultPolicyId":"person@example.com"}',
        ]
        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                self.path.write_bytes(payload)
                os.chmod(self.path, 0o600)
                with self.assertRaises(RoutingPreferencesConfigError):
                    self.store.load()

        self.path.write_text(
            '{"schemaVersion":1,"revision":1,"decisionApiEnabled":true,'
            '"defaultPolicyId":"reliable"}',
            encoding="utf-8",
        )
        os.chmod(self.path, 0o644)
        with self.assertRaises(RoutingPreferencesConfigError):
            self.store.load()


if __name__ == "__main__":
    unittest.main()
