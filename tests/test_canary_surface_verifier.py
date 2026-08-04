from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_canary_surfaces.py"


def snapshot(
    revision: int,
    *,
    generated_at: str,
    source_state: str = "ok",
    freshness_seconds: int = 1,
) -> dict[str, object]:
    return {
        "schemaVersion": "1.0",
        "dataRevision": revision,
        "generatedAt": generated_at,
        "localDay": "2026-07-30",
        "summary": {
            "todayTokens": None,
            "modelCount": 0,
            "coveredDayCount": 0,
        },
        "quotaWindows": [{
            "recordId": "fixture.quota",
            "freshnessSeconds": freshness_seconds,
            "state": "ok",
        }],
        "balances": [],
        "providers": [],
        "sources": [
            {
                "providerId": "fixture",
                "sourceId": "fixture.source",
                "state": source_state,
                "errorCode": None,
                "lastAttemptAt": "2026-07-30T00:00:00Z",
                "lastSuccessAt": "2026-07-30T00:00:00Z",
                "staleAt": "2026-07-30T00:00:00Z",
            }
        ],
        "catalogRevision": "fixture",
    }


class CanarySurfaceVerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location(
            "verify_canary_surfaces", SCRIPT
        )
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_same_revision_ignores_only_render_time_fields_and_emits_no_usage(self):
        api_values = iter([
            snapshot(
                7,
                generated_at="2026-07-30T00:00:00Z",
                freshness_seconds=1,
            ),
            snapshot(
                7,
                generated_at="2026-07-30T00:00:02Z",
                freshness_seconds=3,
            ),
        ])
        cli_value = snapshot(
            7,
            generated_at="2026-07-30T00:00:01Z",
            freshness_seconds=2,
        )

        report = self.module.verify_surfaces(
            api_get=lambda: next(api_values),
            cli_get=lambda: cli_value,
            product={"version": "0.6.0", "build": "9"},
            captured_at="2026-07-30T00:00:03Z",
        )

        self.assertEqual(report, {
            "capturedAt": "2026-07-30T00:00:03Z",
            "checks": {
                "apiCliSnapshot": "pass",
                "sourceHealthAgreement": "pass",
                "visualMenu": "pending_manual",
            },
            "dataRevision": 7,
            "product": {"build": "9", "version": "0.6.0"},
            "schemaVersion": "openusage-canary-surfaces-1",
        })
        encoded = json.dumps(report, sort_keys=True).lower()
        for forbidden in (
            "todaytokens", "providerid", "sourceid", "accountref", "credential"
        ):
            self.assertNotIn(forbidden, encoded)

    def test_revision_drift_retries_the_whole_api_cli_api_sequence(self):
        api_values = iter([
            snapshot(7, generated_at="2026-07-30T00:00:00Z"),
            snapshot(8, generated_at="2026-07-30T00:00:01Z"),
            snapshot(9, generated_at="2026-07-30T00:00:02Z"),
            snapshot(9, generated_at="2026-07-30T00:00:04Z"),
        ])
        cli_values = iter([
            snapshot(7, generated_at="2026-07-30T00:00:00Z"),
            snapshot(9, generated_at="2026-07-30T00:00:03Z"),
        ])

        report = self.module.verify_surfaces(
            api_get=lambda: next(api_values),
            cli_get=lambda: next(cli_values),
            product={"version": "0.6.0", "build": "9"},
            captured_at="2026-07-30T00:00:05Z",
            attempts=2,
        )

        self.assertEqual(report["dataRevision"], 9)

    def test_same_revision_semantic_mismatch_fails_without_echoing_input(self):
        api_value = snapshot(7, generated_at="2026-07-30T00:00:00Z")
        cli_value = snapshot(
            7,
            generated_at="2026-07-30T00:00:01Z",
            source_state="stale",
        )

        with self.assertRaisesRegex(ValueError, "^surface mismatch$") as raised:
            self.module.verify_surfaces(
                api_get=lambda: api_value,
                cli_get=lambda: cli_value,
                product={"version": "0.6.0", "build": "9"},
                captured_at="2026-07-30T00:00:02Z",
            )

        self.assertNotIn("fixture", str(raised.exception))
        self.assertNotIn("stale", str(raised.exception))

    def test_permanent_revision_drift_fails_with_a_fixed_error(self):
        revisions = iter(range(1, 10))

        with self.assertRaisesRegex(ValueError, "^revision did not stabilize$"):
            self.module.verify_surfaces(
                api_get=lambda: snapshot(
                    next(revisions),
                    generated_at="2026-07-30T00:00:00Z",
                ),
                cli_get=lambda: snapshot(
                    next(revisions),
                    generated_at="2026-07-30T00:00:00Z",
                ),
                product={"version": "0.6.0", "build": "9"},
                captured_at="2026-07-30T00:00:02Z",
                attempts=2,
            )

    def test_private_report_writer_is_atomic_and_mode_0600(self):
        if sys.platform == "win32":
            self.skipTest("POSIX mode assertion")
        report = {
            "schemaVersion": "openusage-canary-surfaces-1",
            "capturedAt": "2026-07-30T00:00:03Z",
            "dataRevision": 7,
            "product": {"version": "0.6.0", "build": "9"},
            "checks": {
                "apiCliSnapshot": "pass",
                "sourceHealthAgreement": "pass",
                "visualMenu": "pending_manual",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "surface-check.json"

            self.module.write_private(output, report)

            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), report)
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)

    def test_signature_verification_is_fixed_shell_free_and_bounded(self):
        if sys.platform == "win32":
            self.skipTest("POSIX shell-free verifier test")
        process = mock.Mock()
        process.wait.return_value = 0
        process.poll.return_value = 0
        popen = mock.Mock(return_value=process)

        self.module.verify_app_signature(
            Path("/Applications/OpenUsage Bar.app"),
            popen=popen,
        )

        popen.assert_called_once_with(
            [
                "/usr/bin/codesign",
                "--verify",
                "--deep",
                "--strict",
                "/Applications/OpenUsage Bar.app",
            ],
            shell=False,
            stdin=self.module.subprocess.DEVNULL,
            stdout=self.module.subprocess.DEVNULL,
            stderr=self.module.subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin"},
            start_new_session=True,
        )
        process.wait.assert_called_once_with(
            timeout=self.module.CLI_TIMEOUT_SECONDS
        )


if __name__ == "__main__":
    unittest.main()
