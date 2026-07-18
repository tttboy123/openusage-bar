import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_reboot_recovery.py"


def load_module():
    spec = importlib.util.spec_from_file_location("verify_reboot_recovery", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("reboot recovery verifier is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RebootRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def baseline(self):
        return {
            "schemaVersion": 1,
            "capturedAt": "2026-07-18T20:31:29Z",
            "bootTimeSeconds": 1783987056,
            "app": {
                "bundleId": "com.lune.openusagebar",
                "version": "0.4.3",
                "build": "7",
            },
            "api": {"schemaVersion": "1.0", "dataRevision": 5826},
            "ledger": {
                "changeSeq": 5826,
                "sourceAttempts": {
                    "codex.local_rate_limits": "2026-07-18T20:27:42.947299Z",
                    "kiro.codewhisperer": "2026-07-18T20:27:42.947299Z",
                    "minimax.coding_plan": "2026-07-18T20:27:42.947299Z",
                    "step_plan.quota": "2026-07-18T20:27:42.947299Z",
                },
            },
        }

    def recovered(self):
        return {
            "bootTimeSeconds": 1784406720,
            "signatureOk": True,
            "app": {
                "bundleId": "com.lune.openusagebar",
                "version": "0.4.3",
                "build": "7",
            },
            "launchAgents": {
                "com.lune.openusagebar": {
                    "running": True,
                    "runAtLoad": True,
                    "keepAlive": True,
                    "programMatches": True,
                    "startedAfterBoot": True,
                },
                "com.lune.openusagebar.collector": {
                    "running": True,
                    "runAtLoad": True,
                    "keepAlive": True,
                    "programMatches": True,
                    "startedAfterBoot": True,
                },
            },
            "socket": {"isSocket": True, "mode": 0o600, "ownerMatches": True},
            "api": {
                "healthOk": True,
                "schemaVersion": "1.0",
                "dataRevision": 5840,
            },
            "ledger": {
                "quickCheck": "ok",
                "changeSeq": 5840,
                "sourceStatus": {
                    "codex.local_rate_limits": {
                        "state": "ok",
                        "lastAttemptAt": "2026-07-18T20:35:42.947299Z",
                        "lastSuccessAt": "2026-07-18T20:35:42.947299Z",
                    },
                    "kiro.codewhisperer": {
                        "state": "ok",
                        "lastAttemptAt": "2026-07-18T20:35:42.947299Z",
                        "lastSuccessAt": "2026-07-18T20:35:42.947299Z",
                    },
                    "minimax.coding_plan": {
                        "state": "ok",
                        "lastAttemptAt": "2026-07-18T20:35:42.947299Z",
                        "lastSuccessAt": "2026-07-18T20:35:42.947299Z",
                    },
                    "step_plan.quota": {
                        "state": "ok",
                        "lastAttemptAt": "2026-07-18T20:35:42.947299Z",
                        "lastSuccessAt": "2026-07-18T20:35:42.947299Z",
                    },
                },
            },
        }

    def assert_failure(self, current, reason):
        result = self.module.evaluate_recovery(self.baseline(), current)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, reason)

    def test_accepts_real_reboot_and_advanced_scheduled_collection(self):
        result = self.module.evaluate_recovery(self.baseline(), self.recovered())
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, "ok")

    def test_rejects_unchanged_boot_time(self):
        current = self.recovered()
        current["bootTimeSeconds"] = self.baseline()["bootTimeSeconds"]
        self.assert_failure(current, "boot_unchanged")

    def test_rejects_collection_after_baseline_but_before_new_boot(self):
        current = self.recovered()
        source = current["ledger"]["sourceStatus"]["minimax.coding_plan"]
        source["lastAttemptAt"] = "2026-07-18T20:31:45Z"
        source["lastSuccessAt"] = "2026-07-18T20:31:45Z"
        self.assert_failure(current, "scheduled_collection_before_boot")

    def test_rejects_boot_that_predates_the_saved_baseline(self):
        current = self.recovered()
        current["bootTimeSeconds"] = 1784406600
        self.assert_failure(current, "reboot_before_baseline")

    def test_rejects_stale_baseline_from_an_older_canary(self):
        baseline = self.baseline()
        baseline["capturedAt"] = "2026-07-18T13:00:00Z"
        result = self.module.evaluate_recovery(baseline, self.recovered())
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "baseline_too_old")

    def test_rejects_api_or_ledger_revision_regression(self):
        current = self.recovered()
        current["api"]["dataRevision"] = 5825
        self.assert_failure(current, "api_revision_regressed")
        current = self.recovered()
        current["ledger"]["changeSeq"] = 5825
        self.assert_failure(current, "ledger_revision_regressed")

    def test_rejects_collection_that_did_not_advance_after_baseline(self):
        current = self.recovered()
        current["ledger"]["sourceStatus"]["minimax.coding_plan"][
            "lastAttemptAt"
        ] = self.baseline()["ledger"]["sourceAttempts"]["minimax.coding_plan"]
        current["ledger"]["sourceStatus"]["minimax.coding_plan"][
            "lastSuccessAt"
        ] = self.baseline()["ledger"]["sourceAttempts"]["minimax.coding_plan"]
        self.assert_failure(current, "scheduled_collection_not_advanced")

    def test_rejects_direct_source_failure_or_missing_source(self):
        current = self.recovered()
        current["ledger"]["sourceStatus"]["step_plan.quota"]["state"] = "error"
        self.assert_failure(current, "direct_source_unhealthy")
        current = self.recovered()
        del current["ledger"]["sourceStatus"]["kiro.codewhisperer"]
        self.assert_failure(current, "direct_source_missing")

    def test_rejects_bad_signature_launch_agent_socket_or_api(self):
        cases = (
            ("signatureOk", False, "signature_invalid"),
            ("socket.mode", 0o666, "socket_permissions_invalid"),
            ("socket.ownerMatches", False, "socket_owner_invalid"),
            ("api.healthOk", False, "local_api_unhealthy"),
            ("ledger.quickCheck", "corrupt", "ledger_integrity_failed"),
        )
        for path, value, reason in cases:
            with self.subTest(path=path):
                current = self.recovered()
                target = current
                parts = path.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
                self.assert_failure(current, reason)

        current = self.recovered()
        current["launchAgents"]["com.lune.openusagebar.collector"][
            "programMatches"
        ] = False
        self.assert_failure(current, "launch_agent_invalid")

        current = self.recovered()
        current["launchAgents"]["com.lune.openusagebar"]["startedAfterBoot"] = False
        self.assert_failure(current, "launch_agent_invalid")

    def test_rejects_app_or_schema_drift(self):
        current = self.recovered()
        current["app"]["build"] = "8"
        self.assert_failure(current, "app_metadata_changed")
        current = self.recovered()
        current["api"]["schemaVersion"] = "2.0"
        self.assert_failure(current, "api_schema_changed")

    def test_baseline_round_trip_is_strict_and_mode_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "baseline.json"
            self.module.write_baseline(destination, self.baseline())
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.module.load_baseline(destination), self.baseline())
            serialized = destination.read_text("utf-8").lower()
            for forbidden in (
                "keychain",
                "credential",
                "api_key",
                "cookie",
                "prompt",
                "response",
                str(Path.home()).lower(),
            ):
                self.assertNotIn(forbidden, serialized)

    def test_baseline_rejects_unknown_or_malformed_fields(self):
        malformed = self.baseline()
        malformed["credential"] = "must-not-be-stored"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.json"
            path.write_text(json.dumps(malformed), encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaises(ValueError):
                self.module.load_baseline(path)

        malformed = self.baseline()
        malformed["bootTimeSeconds"] = "100"
        with self.assertRaises(ValueError):
            self.module.validate_baseline(malformed)

    def test_boot_time_parser_is_bounded_and_fail_closed(self):
        self.assertEqual(
            self.module.parse_boot_time(
                "{ sec = 1783987056, usec = 238046 } Tue Jul 14 07:57:36 2026"
            ),
            1783987056,
        )
        for malformed in ("", "1783987056", "{ sec = -1, usec = 0 }"):
            with self.subTest(malformed=malformed):
                with self.assertRaises(ValueError):
                    self.module.parse_boot_time(malformed)

    def test_api_probe_requires_health_schema_and_summary_contracts(self):
        valid = {
            "/v1/health": {
                "schemaVersion": "1.0",
                "dataRevision": 42,
                "health": {"ok": True, "status": "ok"},
            },
            "/v1/schema": {"schemaVersion": "1.0", "routes": []},
            "/v1/summary": {
                "schemaVersion": "1.0",
                "dataRevision": 42,
                "todayTokens": None,
            },
        }
        with mock.patch.object(
            self.module, "_api_get", side_effect=lambda _socket, route: valid[route]
        ):
            self.assertEqual(
                self.module._api_state(Path("/private/tmp/openusage.sock")),
                {"healthOk": True, "schemaVersion": "1.0", "dataRevision": 42},
            )

        del valid["/v1/summary"]["todayTokens"]
        with mock.patch.object(
            self.module, "_api_get", side_effect=lambda _socket, route: valid[route]
        ):
            self.assertFalse(
                self.module._api_state(Path("/private/tmp/openusage.sock"))[
                    "healthOk"
                ]
            )

    def test_verifier_contains_no_refresh_keychain_or_launch_mutation_command(self):
        source = SCRIPT.read_text("utf-8")
        for forbidden in (
            '"/usr/bin/security"',
            '"__refresh-once"',
            '"kickstart"',
            '"bootstrap"',
            '"bootout"',
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
