from __future__ import annotations

import io
import json
import os
import shlex
import stat
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openusage_bar.activity_store import ActivityStore
from openusage_bar.openusage_catalog import (
    EXPECTED_PROVIDER_IDS,
    CatalogDiagnostic,
    OpenUsageCatalogDiscovery,
    parse_registered_providers,
)
from openusage_bar.daily_history import OpenUsageCatalogMonitor


NOW = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)


def detect_output(provider_ids=EXPECTED_PROVIDER_IDS) -> str:
    rows = "\n".join(f"  - {provider_id}" for provider_id in provider_ids)
    return (
        "Tools detected:\n"
        "  Demo CLI  cli  /private/example/tool\n\n"
        "Accounts detected:\n"
        "  PROVIDER ACCOUNT AUTH CREDENTIAL SOURCE\n"
        "  demo account api_key $DEMO_KEY=masked env\n\n"
        "All registered providers:\n"
        f"{rows}\n"
    )


def executable_script(directory: str, body: str) -> str:
    path = Path(directory) / "fake-openusage"
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class OpenUsageCatalogParserTests(unittest.TestCase):
    def test_parses_only_exact_provider_rows_inside_registered_section(self):
        text = detect_output()
        self.assertEqual(parse_registered_providers(text), EXPECTED_PROVIDER_IDS)
        self.assertNotIn("demo", parse_registered_providers(text))

    def test_section_boundary_does_not_consume_later_rows(self):
        text = detect_output() + "\nDiagnostics:\n  - credential_like_value\n"
        self.assertEqual(parse_registered_providers(text), EXPECTED_PROVIDER_IDS)

    def test_invalid_or_malicious_section_is_rejected_without_echoing_input(self):
        secret_shaped = "  credential=/private/example/token\n"
        with self.assertRaisesRegex(ValueError, "invalid detect output") as caught:
            parse_registered_providers(
                "All registered providers:\n  - openai\n" + secret_shaped
            )
        self.assertNotIn("credential", str(caught.exception))
        self.assertNotIn("/private", str(caught.exception))


class OpenUsageCatalogDiscoveryTests(unittest.TestCase):
    def _discover(self, script: str) -> CatalogDiagnostic:
        return OpenUsageCatalogDiscovery(openusage_path=script, clock=lambda: NOW).run()

    def test_exact_version_revision_and_set_are_ok(self):
        with tempfile.TemporaryDirectory() as temp:
            payload = json.dumps(detect_output())
            script = executable_script(temp, f"""
import sys
if sys.argv[1:] == ['version']:
    print('0.23.0 (3059f1b) built 2026-07-05T00:35:50Z')
elif sys.argv[1:] == ['detect', '--all']:
    print({payload})
else:
    raise SystemExit(2)
""")
            result = self._discover(script)
        self.assertEqual(result.outcome, "ok")
        self.assertEqual((result.expected_count, result.actual_count), (35, 35))
        self.assertEqual((result.missing_count, result.extra_count), (0, 0))

    def test_missing_and_extra_are_counted_without_persisting_names(self):
        ids = sorted((set(EXPECTED_PROVIDER_IDS) - {"amp"}) | {"future_provider"})
        with tempfile.TemporaryDirectory() as temp:
            payload = json.dumps(detect_output(ids))
            script = executable_script(temp, f"""
import sys
if sys.argv[1:] == ['version']:
    print('0.23.0 (3059f1b) built 2026-07-05T00:35:50Z')
elif sys.argv[1:] == ['detect', '--all']:
    print({payload})
""")
            result = self._discover(script)
        self.assertEqual(result.outcome, "provider_catalog_drift")
        self.assertEqual((result.missing_count, result.extra_count), (1, 1))
        self.assertNotIn("amp", repr(result))
        self.assertNotIn("future_provider", repr(result))

    def test_version_and_revision_drift_are_sanitized(self):
        for version in (
            "0.24.0 (3059f1b) built 2026-07-05T00:35:50Z",
            "0.23.0 (fffffff) built 2026-07-05T00:35:50Z",
            "0.23.0",
        ):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temp:
                script = executable_script(temp, f"""
import sys
if sys.argv[1:] == ['version']:
    print({version!r})
""")
                result = self._discover(script)
                self.assertEqual(result.outcome, "unsupported_openusage_version")
                self.assertNotIn(version, repr(result))

    def test_unavailable_invalid_detect_and_oversized_output_are_sanitized(self):
        unavailable = OpenUsageCatalogDiscovery(
            openusage_path="/definitely/not/openusage", clock=lambda: NOW
        ).run()
        self.assertEqual(unavailable.outcome, "openusage_unavailable")
        with tempfile.TemporaryDirectory() as temp:
            invalid = executable_script(temp, """
import sys
if sys.argv[1:] == ['version']:
    print('0.23.0 (3059f1b) built 2026-07-05T00:35:50Z')
else:
    print('credential=/private/example/value')
""")
            self.assertEqual(self._discover(invalid).outcome, "invalid_detect_output")
            oversized = executable_script(temp, """
import sys
if sys.argv[1:] == ['version']:
    print('0.23.0 (3059f1b) built 2026-07-05T00:35:50Z')
else:
    print('x' * 1000000)
""")
            result = self._discover(oversized)
            self.assertEqual(result.outcome, "invalid_detect_output")
            self.assertLess(len(repr(result)), 300)

    def test_timeout_kills_and_reaps_child(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "completed"
            script = executable_script(temp, f"""
import pathlib, sys, time
if sys.argv[1:] == ['version']:
    print('0.23.0 (3059f1b) built 2026-07-05T00:35:50Z')
else:
    time.sleep(1)
    pathlib.Path({str(marker)!r}).write_text('not killed')
""")
            result = OpenUsageCatalogDiscovery(
                openusage_path=script, timeout_seconds=0.05, clock=lambda: NOW
            ).run()
            self.assertEqual(result.outcome, "timeout")
            self.assertFalse(marker.exists())

    def test_timeout_kills_process_group_when_exited_parent_leaves_inherited_pipes(self):
        with tempfile.TemporaryDirectory() as temp:
            child_pid_path = Path(temp) / "child.pid"
            script_path = Path(temp) / "fake-openusage"
            child_code = (
                "import os,pathlib,time;"
                f"pathlib.Path({str(child_pid_path)!r}).write_text(str(os.getpid()));"
                "time.sleep(60)"
            )
            script_path.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "version" ]; then\n'
                "  printf '%s\\n' '0.23.0 (3059f1b) built 2026-07-05T00:35:50Z'\n"
                "else\n"
                f"  {shlex.quote(sys.executable)} -c {shlex.quote(child_code)} &\n"
                "  exit 0\n"
                "fi\n",
                encoding="utf-8",
            )
            script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)
            started = time.monotonic()
            result = OpenUsageCatalogDiscovery(
                openusage_path=str(script_path), timeout_seconds=3.0, clock=lambda: NOW
            ).run()
            elapsed = time.monotonic() - started
            ready_deadline = time.monotonic() + 1
            while not child_pid_path.exists() and time.monotonic() < ready_deadline:
                time.sleep(0.01)
            self.assertTrue(child_pid_path.exists(), "fixture child did not publish its PID")
            child_pid = int(child_pid_path.read_text())
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                self.fail("catalog discovery grandchild survived process-group timeout")
        self.assertEqual(result.outcome, "timeout")
        self.assertLess(elapsed, 4.5)

    def test_child_uses_allowlisted_environment_and_direct_argv(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "env.json"
            payload = json.dumps(detect_output())
            script = executable_script(temp, f"""
import json, os, pathlib, sys
if sys.argv[1:] == ['version']:
    print('0.23.0 (3059f1b) built 2026-07-05T00:35:50Z')
else:
    pathlib.Path({str(output)!r}).write_text(json.dumps(sorted(os.environ)))
    print({payload})
""")
            discovery = OpenUsageCatalogDiscovery(
                openusage_path=script,
                environment={"PATH": os.environ.get("PATH", ""), "PRIVATE_VALUE": "hidden"},
                clock=lambda: NOW,
            )
            self.assertEqual(discovery.run().outcome, "ok")
            names = json.loads(output.read_text())
            self.assertNotIn("PRIVATE_VALUE", names)


class OpenUsageCatalogMonitorTests(unittest.TestCase):
    def test_startup_then_at_most_once_per_24_hours_across_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ledger.sqlite3"
            calls: list[datetime] = []

            class Discovery:
                def run(self):
                    calls.append(NOW)
                    return CatalogDiagnostic("ok", 35, 35, 0, 0, NOW)

            store = ActivityStore(path)
            OpenUsageCatalogMonitor(store, Discovery(), clock=lambda: NOW).maybe_run()
            store.close()
            reopened = ActivityStore(path)
            OpenUsageCatalogMonitor(
                reopened, Discovery(), clock=lambda: NOW + timedelta(hours=23)
            ).maybe_run()
            OpenUsageCatalogMonitor(
                reopened, Discovery(), clock=lambda: NOW + timedelta(hours=24)
            ).maybe_run()
            statuses = reopened.source_statuses()
            reopened.close()
        self.assertEqual(len(calls), 2)
        status = next(row for row in statuses if row.source_id == "openusage.detect")
        self.assertEqual(status.state, "ok")

    def test_clock_rollback_is_due_and_replaces_future_attempt_time(self):
        store = ActivityStore(":memory:")
        future = NOW + timedelta(days=3)
        store.record_source_success(
            "openusage_catalog", "openusage.detect", future,
            freshness_seconds=48 * 60 * 60,
        )
        calls = 0

        class Discovery:
            def run(self):
                nonlocal calls
                calls += 1
                return CatalogDiagnostic("ok", 35, 35, 0, 0, NOW)

        OpenUsageCatalogMonitor(store, Discovery(), clock=lambda: NOW).maybe_run()
        status = next(row for row in store.source_statuses() if row.source_id == "openusage.detect")
        store.close()
        self.assertEqual(calls, 1)
        self.assertTrue(status.last_attempt_at.startswith("2026-07-15T02:00:00"))

    def test_persists_only_sanitized_outcome_and_counts(self):
        store = ActivityStore(":memory:")

        class Discovery:
            def run(self):
                return CatalogDiagnostic(
                    "provider_catalog_drift", 35, 36, 2, 3, NOW
                )

        OpenUsageCatalogMonitor(store, Discovery(), clock=lambda: NOW).maybe_run()
        status = next(row for row in store.source_statuses() if row.source_id == "openusage.detect")
        store.close()
        self.assertEqual(status.state, "temporarily_unavailable")
        self.assertEqual(
            status.error_code,
            "provider_catalog_drift_e35_a36_m2_x3",
        )
        self.assertNotIn("future_provider", status.error_code or "")
        self.assertNotIn("amp", status.error_code or "")


if __name__ == "__main__":
    unittest.main()
