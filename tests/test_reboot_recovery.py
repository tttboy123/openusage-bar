import contextlib
import importlib.util
import io
import json
import os
import plistlib
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
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


@unittest.skipIf(sys.platform == "win32", "macOS/POSIX reboot recovery test")
class RebootRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def baseline(self):
        return {
            "schemaVersion": 3,
            "capturedAt": "2026-07-18T20:31:29Z",
            "bootTimeSeconds": 1783987056,
            "app": {
                "bundleId": "com.lune.openusagebar",
                "version": "0.4.3",
                "build": "7",
                "signatureHash": "1a638387f6844d020e24e6f0db6d3edeca34c389",
                "statusProgramHash": "2b638387f6844d020e24e6f0db6d3edeca34c389",
                "statusRuntimeHash": "2c638387f6844d020e24e6f0db6d3edeca34c389",
                "collectorProgramHash": "3c638387f6844d020e24e6f0db6d3edeca34c389",
                "collectorRuntimeHash": "3d638387f6844d020e24e6f0db6d3edeca34c389",
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
                "signatureHash": "1a638387f6844d020e24e6f0db6d3edeca34c389",
                "statusProgramHash": "2b638387f6844d020e24e6f0db6d3edeca34c389",
                "statusRuntimeHash": "2c638387f6844d020e24e6f0db6d3edeca34c389",
                "collectorProgramHash": "3c638387f6844d020e24e6f0db6d3edeca34c389",
                "collectorRuntimeHash": "3d638387f6844d020e24e6f0db6d3edeca34c389",
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
        result = self.module.evaluate_recovery(
            self.baseline(), current, evaluated_at=self.verification_time()
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, reason)

    def verification_time(self):
        return datetime(2026, 7, 18, 20, 36, tzinfo=timezone.utc)

    def test_accepts_real_reboot_and_advanced_scheduled_collection(self):
        result = self.module.evaluate_recovery(
            self.baseline(), self.recovered(), evaluated_at=self.verification_time()
        )
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
        result = self.module.evaluate_recovery(
            baseline, self.recovered(), evaluated_at=self.verification_time()
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "baseline_too_old")

    def test_rejects_verification_reused_long_after_the_new_boot(self):
        result = self.module.evaluate_recovery(
            self.baseline(),
            self.recovered(),
            evaluated_at=datetime(2026, 7, 19, 20, 36, tzinfo=timezone.utc),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "verification_too_late")

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
        current["app"]["signatureHash"] = "f" * 40
        self.assert_failure(current, "app_metadata_changed")
        current = self.recovered()
        current["app"]["statusProgramHash"] = "d" * 40
        self.assert_failure(current, "app_metadata_changed")
        current = self.recovered()
        current["app"]["collectorProgramHash"] = "e" * 40
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

        malformed = self.baseline()
        malformed["app"]["signatureHash"] = "not-a-code-hash"
        with self.assertRaises(ValueError):
            self.module.validate_baseline(malformed)
        malformed = self.baseline()
        malformed["app"]["statusProgramHash"] = "not-a-code-hash"
        with self.assertRaises(ValueError):
            self.module.validate_baseline(malformed)
        malformed = self.baseline()
        malformed["app"]["collectorProgramHash"] = "not-a-code-hash"
        with self.assertRaises(ValueError):
            self.module.validate_baseline(malformed)

        malformed = self.baseline()
        malformed["app"]["statusRuntimeHash"] = "not-a-code-hash"
        with self.assertRaises(ValueError):
            self.module.validate_baseline(malformed)

        malformed = self.baseline()
        malformed["app"]["collectorRuntimeHash"] = "not-a-code-hash"
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
                "modelCount": 0,
                "coveredDayCount": 0,
            },
        }
        with mock.patch.object(
            self.module, "_api_get", side_effect=lambda _socket, route: valid[route]
        ):
            self.assertEqual(
                self.module._api_state(Path("/private/tmp/openusage.sock")),
                {"healthOk": True, "schemaVersion": "1.0", "dataRevision": 42},
            )

        valid["/v1/summary"]["todayTokens"] = 0
        with mock.patch.object(
            self.module, "_api_get", side_effect=lambda _socket, route: valid[route]
        ):
            self.assertFalse(
                self.module._api_state(Path("/private/tmp/openusage.sock"))[
                    "healthOk"
                ]
            )

        valid["/v1/summary"]["coveredDayCount"] = 1
        with mock.patch.object(
            self.module, "_api_get", side_effect=lambda _socket, route: valid[route]
        ):
            self.assertTrue(
                self.module._api_state(Path("/private/tmp/openusage.sock"))[
                    "healthOk"
                ]
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

    def test_bundle_socket_and_ledger_probes_return_only_bounded_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "OpenUsage Bar.app"
            info = app / "Contents/Info.plist"
            info.parent.mkdir(parents=True)
            info.write_bytes(
                plistlib.dumps(
                    {
                        "CFBundleIdentifier": "com.lune.openusagebar",
                        "CFBundleShortVersionString": "0.4.3",
                        "CFBundleVersion": "7",
                        "PrivateIgnoredField": "not-exported",
                    }
                )
            )
            self.assertEqual(
                self.module._bundle_metadata(app),
                {
                    "bundleId": "com.lune.openusagebar",
                    "version": "0.4.3",
                    "build": "7",
                },
            )

            socket_path = root / "openusage.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(socket_path))
                os.chmod(socket_path, 0o600)
                self.assertEqual(
                    self.module._socket_state(socket_path),
                    {"isSocket": True, "mode": 0o600, "ownerMatches": True},
                )
            finally:
                listener.close()

            ledger = root / "activity.sqlite3"
            connection = sqlite3.connect(ledger)
            connection.executescript(
                "CREATE TABLE change_log(change_seq INTEGER);"
                "CREATE TABLE source_status("
                "provider_id TEXT,source_id TEXT,state TEXT,"
                "last_attempt_at TEXT,last_success_at TEXT);"
                "INSERT INTO change_log VALUES(9);"
                "INSERT INTO source_status VALUES("
                "'one','minimax.coding_plan','ok',"
                "'2026-07-18T20:27:42Z','2026-07-18T20:27:42Z');"
                "INSERT INTO source_status VALUES("
                "'two','minimax.coding_plan','ok',"
                "'2026-07-18T20:28:42Z','2026-07-18T20:28:42Z');"
            )
            connection.commit()
            connection.close()
            state = self.module._ledger_state(ledger)
            self.assertEqual(state["quickCheck"], "ok")
            self.assertEqual(state["changeSeq"], 9)
            self.assertEqual(
                state["sourceStatus"]["minimax.coding_plan"],
                {
                    "state": "ok",
                    "lastAttemptAt": "2026-07-18T20:27:42Z",
                    "lastSuccessAt": "2026-07-18T20:27:42Z",
                },
            )

    def test_launch_agent_probe_checks_label_program_pid_and_start_time(self):
        with tempfile.TemporaryDirectory() as directory:
            agents = Path(directory)
            label = "com.lune.openusagebar"
            program = Path("/Applications/OpenUsage Bar.app/Contents/MacOS/OpenUsage Bar")
            (agents / f"{label}.plist").write_bytes(
                plistlib.dumps(
                    {
                        "Label": label,
                        "ProgramArguments": [str(program), "--background"],
                        "RunAtLoad": True,
                        "KeepAlive": True,
                    }
                )
            )
            filtered = (
                "state = running\n"
                f"program = {program}\n"
                "runs = 1\n"
                "pid = 123\n"
            )
            with mock.patch.object(
                self.module, "_filtered_launchctl", return_value=filtered
            ), mock.patch.object(
                self.module, "_process_started_after_boot", return_value=True
            ):
                self.assertEqual(
                    self.module._launch_agent_state(label, program, agents, 100),
                    {
                        "running": True,
                        "runAtLoad": True,
                        "keepAlive": True,
                        "programMatches": True,
                        "startedAfterBoot": True,
                    },
                )

            payload = plistlib.loads((agents / f"{label}.plist").read_bytes())
            payload["Label"] = "com.example.other"
            (agents / f"{label}.plist").write_bytes(plistlib.dumps(payload))
            with mock.patch.object(
                self.module, "_filtered_launchctl", return_value=filtered
            ), mock.patch.object(
                self.module, "_process_started_after_boot", return_value=True
            ):
                self.assertFalse(
                    self.module._launch_agent_state(label, program, agents, 100)[
                        "programMatches"
                    ]
                )

    def test_local_api_get_is_bounded_and_parses_one_private_socket_response(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api.sock"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen(1)

            def serve():
                connection, _ = server.accept()
                try:
                    request = connection.recv(4096)
                    self.assertIn(b"GET /v1/health HTTP/1.1", request)
                    body = b'{"schemaVersion":"1.0"}'
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 23\r\n\r\n" + body
                    )
                finally:
                    connection.close()

            worker = threading.Thread(target=serve)
            worker.start()
            try:
                self.assertEqual(
                    self.module._api_get(path, "/v1/health"),
                    {"schemaVersion": "1.0"},
                )
            finally:
                worker.join(timeout=2)
                server.close()
            self.assertFalse(worker.is_alive())

    def test_baseline_builder_selects_only_latest_healthy_cycle(self):
        snapshot = self.recovered()
        snapshot["ledger"]["sourceStatus"]["older.source"] = {
            "state": "ok",
            "lastAttemptAt": "2026-07-18T20:20:42Z",
            "lastSuccessAt": "2026-07-18T20:20:42Z",
        }
        snapshot["ledger"]["sourceStatus"]["same.cycle.skew"] = {
            "state": "ok",
            "lastAttemptAt": "2026-07-18T20:35:12Z",
            "lastSuccessAt": "2026-07-18T20:35:12Z",
        }
        baseline = self.module.baseline_from_snapshot(snapshot)
        self.assertEqual(
            set(baseline["ledger"]["sourceAttempts"]),
            {
                "codex.local_rate_limits",
                "kiro.codewhisperer",
                "minimax.coding_plan",
                "same.cycle.skew",
                "step_plan.quota",
            },
        )

        snapshot["signatureOk"] = False
        with self.assertRaises(self.module.ProbeUnavailable):
            self.module.baseline_from_snapshot(snapshot)

    def test_probe_runtime_composes_read_only_helpers(self):
        app = Path("/Applications/OpenUsage Bar.app")
        with mock.patch.object(
            self.module,
            "_run",
            return_value="{ sec = 1784406720, usec = 0 } Sat Jul 18 20:32:00 2026",
        ), mock.patch.object(
            self.module, "_signature_ok", return_value=True
        ), mock.patch.object(
            self.module, "_bundle_metadata", return_value=self.baseline()["app"]
        ), mock.patch.object(
            self.module,
            "_signature_hash",
            side_effect=(
                self.baseline()["app"]["signatureHash"],
                self.baseline()["app"]["statusProgramHash"],
                self.baseline()["app"]["statusRuntimeHash"],
                self.baseline()["app"]["collectorProgramHash"],
                self.baseline()["app"]["collectorRuntimeHash"],
            ),
        ) as signature_probe, mock.patch.object(
            self.module, "_launch_agent_state", return_value={"running": True}
        ) as launch_probe, mock.patch.object(
            self.module,
            "_socket_state",
            return_value={"isSocket": True, "mode": 0o600, "ownerMatches": True},
        ), mock.patch.object(
            self.module,
            "_api_state",
            return_value={"healthOk": True, "schemaVersion": "1.0", "dataRevision": 9},
        ), mock.patch.object(
            self.module,
            "_ledger_state",
            return_value={"quickCheck": "ok", "changeSeq": 9, "sourceStatus": {}},
        ):
            snapshot = self.module.probe_runtime(
                app=app,
                socket_path=Path("/tmp/api.sock"),
                ledger=Path("/tmp/activity.sqlite3"),
                launch_agents_directory=Path("/tmp/LaunchAgents"),
            )
        self.assertEqual(snapshot["bootTimeSeconds"], 1784406720)
        self.assertEqual(
            snapshot["app"]["signatureHash"],
            self.baseline()["app"]["signatureHash"],
        )
        self.assertEqual(
            snapshot["app"]["collectorProgramHash"],
            self.baseline()["app"]["collectorProgramHash"],
        )
        self.assertEqual(
            snapshot["app"]["statusRuntimeHash"],
            self.baseline()["app"]["statusRuntimeHash"],
        )
        self.assertEqual(
            snapshot["app"]["collectorRuntimeHash"],
            self.baseline()["app"]["collectorRuntimeHash"],
        )
        status_program = app / "Contents/MacOS/OpenUsage Bar"
        status_runtime = app / "Contents/MacOS/OpenUsage Bar.runtime"
        collector_program = app / "Contents/MacOS/OpenUsage Collector"
        collector_runtime = (
            app
            / "Contents/Helpers/OpenUsage Provider Settings.app/Contents/MacOS"
            / "OpenUsage Provider Settings"
        )
        signature_probe.assert_has_calls(
            [
                mock.call(app),
                mock.call(status_program),
                mock.call(status_runtime),
                mock.call(collector_program),
                mock.call(collector_runtime),
            ]
        )
        self.assertEqual(signature_probe.call_count, 5)
        self.assertEqual(launch_probe.call_count, 2)

    def test_probe_runtime_rejects_untrusted_socket_before_connecting(self):
        app = Path("/Applications/OpenUsage Bar.app")
        with mock.patch.object(
            self.module,
            "_run",
            return_value="{ sec = 1784406720, usec = 0 } Sat Jul 18 20:32:00 2026",
        ), mock.patch.object(
            self.module, "_signature_ok", return_value=True
        ), mock.patch.object(
            self.module, "_bundle_metadata", return_value=self.baseline()["app"]
        ), mock.patch.object(
            self.module, "_launch_agent_state", return_value={"running": True}
        ), mock.patch.object(
            self.module,
            "_socket_state",
            return_value={"isSocket": True, "mode": 0o666, "ownerMatches": False},
        ), mock.patch.object(
            self.module, "_api_state"
        ) as api_state, self.assertRaises(self.module.ProbeUnavailable):
            self.module.probe_runtime(
                app=app,
                socket_path=Path("/tmp/api.sock"),
                ledger=Path("/tmp/activity.sqlite3"),
                launch_agents_directory=Path("/tmp/LaunchAgents"),
            )
        api_state.assert_not_called()

    def test_command_and_process_helpers_fail_closed(self):
        completed = SimpleNamespace(returncode=0, stdout=b"filtered\n")
        with mock.patch.object(
            self.module.subprocess, "run", return_value=completed
        ) as runner:
            self.assertEqual(self.module._run(["/usr/bin/true"]), "filtered\n")
        self.assertEqual(runner.call_args.kwargs["stdin"], self.module.subprocess.DEVNULL)
        self.assertEqual(
            runner.call_args.kwargs["env"]["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin"
        )

        failed = SimpleNamespace(returncode=1, stdout=b"")
        with mock.patch.object(self.module.subprocess, "run", return_value=failed):
            with self.assertRaises(self.module.ProbeUnavailable):
                self.module._run(["/usr/bin/false"])

        with mock.patch.object(self.module, "_run", return_value="valid"):
            self.assertTrue(self.module._signature_ok(Path("/Applications/Test.app")))
        with mock.patch.object(
            self.module, "_run", side_effect=self.module.ProbeUnavailable("failed")
        ):
            self.assertFalse(self.module._signature_ok(Path("/Applications/Test.app")))

        started = "Sat Jul 18 20:32:00 2026"
        with mock.patch.object(self.module, "_run", return_value=started), mock.patch.object(
            self.module.time, "mktime", return_value=1784406720
        ):
            self.assertTrue(self.module._process_started_after_boot(123, 1784406720))
        with mock.patch.object(self.module, "_run", return_value="malformed"):
            self.assertFalse(self.module._process_started_after_boot(123, 1784406720))

        signature_hash = self.baseline()["app"]["signatureHash"]
        with mock.patch.object(
            self.module, "_run", return_value=f"CDHash={signature_hash}\n"
        ) as signature_probe:
            self.assertEqual(
                self.module._signature_hash(Path("/Applications/Test.app")),
                signature_hash,
            )
        self.assertIn("/bin/zsh", signature_probe.call_args.args[0])
        with mock.patch.object(self.module, "_run", return_value="CDHash=invalid\n"):
            with self.assertRaises(self.module.ProbeUnavailable):
                self.module._signature_hash(Path("/Applications/Test.app"))

    def test_cli_arguments_require_absolute_paths_and_bounded_timeout(self):
        absolute = self.module._absolute_path("/tmp/reboot-baseline.json")
        self.assertEqual(absolute, Path("/tmp/reboot-baseline.json"))
        with self.assertRaises(self.module.argparse.ArgumentTypeError):
            self.module._absolute_path("relative.json")

        argv = [str(SCRIPT), "verify", "--timeout", "60"]
        with mock.patch.object(sys, "argv", argv):
            self.assertEqual(self.module._arguments().timeout, 60)
        for timeout in ("-1", "601", "nan"):
            with self.subTest(timeout=timeout), mock.patch.object(
                sys, "argv", [str(SCRIPT), "verify", "--timeout", timeout]
            ), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.module._arguments()

    def test_main_probe_failures_are_sanitized(self):
        common = {
            "baseline": Path("/tmp/reboot-baseline.json"),
            "app": Path("/Applications/OpenUsage Bar.app"),
            "socket": Path("/tmp/api.sock"),
            "ledger": Path("/tmp/activity.sqlite3"),
            "timeout": 0.0,
        }
        output = io.StringIO()
        with mock.patch.object(
            self.module,
            "_arguments",
            return_value=SimpleNamespace(mode="capture", **common),
        ), mock.patch.object(
            self.module,
            "probe_runtime",
            side_effect=self.module.ProbeUnavailable("private detail"),
        ), contextlib.redirect_stdout(output):
            self.assertEqual(self.module.main(), 1)
        self.assertEqual(output.getvalue(), "reboot_baseline_failed\n")

        output = io.StringIO()
        with mock.patch.object(
            self.module,
            "_arguments",
            return_value=SimpleNamespace(mode="verify", **common),
        ), mock.patch.object(
            self.module, "load_baseline", side_effect=ValueError("private detail")
        ), contextlib.redirect_stdout(output):
            self.assertEqual(self.module.main(), 1)
        self.assertEqual(
            output.getvalue(),
            "reboot_recovery_failed reason=baseline_or_boot_unavailable\n",
        )

        output = io.StringIO()
        new_boot = "{ sec = 1784406720, usec = 0 } Sat Jul 18 20:32:00 2026"
        with mock.patch.object(
            self.module,
            "_arguments",
            return_value=SimpleNamespace(mode="verify", **common),
        ), mock.patch.object(
            self.module, "load_baseline", return_value=self.baseline()
        ), mock.patch.object(
            self.module, "_run", return_value=new_boot
        ), mock.patch.object(
            self.module,
            "probe_runtime",
            side_effect=self.module.ProbeUnavailable("private detail"),
        ), contextlib.redirect_stdout(output):
            self.assertEqual(self.module.main(), 1)
        self.assertEqual(
            output.getvalue(),
            "reboot_recovery_failed reason=runtime_unavailable\n",
        )

    def test_main_capture_verify_and_fail_closed_paths_are_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline_path = Path(directory) / "baseline.json"
            common = {
                "baseline": baseline_path,
                "app": Path("/Applications/OpenUsage Bar.app"),
                "socket": Path("/tmp/api.sock"),
                "ledger": Path("/tmp/activity.sqlite3"),
                "timeout": 0.0,
            }
            capture_args = SimpleNamespace(mode="capture", **common)
            with mock.patch.object(
                self.module, "_arguments", return_value=capture_args
            ), mock.patch.object(
                self.module, "probe_runtime", return_value=self.recovered()
            ), mock.patch.object(
                self.module, "baseline_from_snapshot", return_value=self.baseline()
            ), mock.patch.object(
                self.module, "write_baseline"
            ) as writer, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.module.main(), 0)
            writer.assert_called_once()

            verify_args = SimpleNamespace(mode="verify", **common)
            new_boot = (
                "{ sec = 1784406720, usec = 0 } Sat Jul 18 20:32:00 2026"
            )
            output = io.StringIO()
            with mock.patch.object(
                self.module, "_arguments", return_value=verify_args
            ), mock.patch.object(
                self.module, "load_baseline", return_value=self.baseline()
            ), mock.patch.object(
                self.module, "_run", return_value=new_boot
            ), mock.patch.object(
                self.module, "probe_runtime", return_value=self.recovered()
            ), mock.patch.object(
                self.module,
                "evaluate_recovery",
                return_value=self.module.RecoveryResult(True, "ok"),
            ), contextlib.redirect_stdout(output):
                self.assertEqual(self.module.main(), 0)
            self.assertIn("visualMenuCheck=pending", output.getvalue())

            old_boot = (
                "{ sec = 1783987056, usec = 0 } Tue Jul 14 07:57:36 2026"
            )
            output = io.StringIO()
            with mock.patch.object(
                self.module, "_arguments", return_value=verify_args
            ), mock.patch.object(
                self.module, "load_baseline", return_value=self.baseline()
            ), mock.patch.object(
                self.module, "_run", return_value=old_boot
            ), contextlib.redirect_stdout(output):
                self.assertEqual(self.module.main(), 1)
            self.assertEqual(
                output.getvalue(), "reboot_recovery_failed reason=boot_unchanged\n"
            )


if __name__ == "__main__":
    unittest.main()
