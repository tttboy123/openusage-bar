from __future__ import annotations

import json
import plistlib
import sys
import tempfile
import time
import unittest
from pathlib import Path

from scripts.measure_performance import (
    Budget,
    RawProcessUsage,
    _bundle_metadata,
    _refresh_command,
    aggregate_rounds,
    build_report,
    delta_usage,
    evaluate_budgets,
    run_refresh,
    validate_measurement_plan,
)


MIB = 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]
BASELINE = (
    ROOT
    / "docs"
    / "performance-baselines"
    / "0.4.4-build8-2026-07-29.json"
)
GUIDE = ROOT / "docs" / "performance.md"


class PerformanceMeasurementTests(unittest.TestCase):
    def test_delta_usage_calculates_cpu_memory_and_wakeups(self):
        before = RawProcessUsage(
            cpu_nanoseconds=1_000_000_000,
            wakeups=10,
            footprint_bytes=20 * MIB,
        )
        after = RawProcessUsage(
            cpu_nanoseconds=1_250_000_000,
            wakeups=14,
            footprint_bytes=24 * MIB,
        )

        measured = delta_usage(before, after, elapsed_seconds=10)

        self.assertEqual(measured["cpuPercent"], 2.5)
        self.assertEqual(measured["wakeupsPerSecond"], 0.4)
        self.assertEqual(measured["footprintBytes"], 24 * MIB)

    def test_counter_regression_is_missing_instead_of_zero(self):
        before = RawProcessUsage(20, 20, 20)
        after = RawProcessUsage(10, 10, 10)

        measured = delta_usage(before, after, elapsed_seconds=10)

        self.assertIsNone(measured["cpuPercent"])
        self.assertIsNone(measured["wakeupsPerSecond"])
        self.assertEqual(measured["quality"], "unavailable")

    def test_three_round_aggregate_uses_median_and_nearest_rank_p95(self):
        rounds = [
            {"cpuPercent": 0.2, "wakeupsPerSecond": 0.5, "footprintBytes": 20},
            {"cpuPercent": 0.4, "wakeupsPerSecond": 0.7, "footprintBytes": 22},
            {"cpuPercent": 0.3, "wakeupsPerSecond": 0.6, "footprintBytes": 21},
        ]

        summary = aggregate_rounds(rounds)

        self.assertEqual(summary["cpuPercentMedian"], 0.3)
        self.assertEqual(summary["cpuPercentP95"], 0.4)
        self.assertEqual(summary["wakeupsPerSecondP95"], 0.7)
        self.assertEqual(summary["footprintBytesPeak"], 22)
        self.assertEqual(summary["sampleCount"], 3)

    def test_missing_roles_remain_unknown_in_budget_results(self):
        budgets = [
            Budget("residentCpuPercentP95", 1.0, "maximum"),
            Budget("activityFootprintBytesPeak", 80 * MIB, "maximum"),
        ]

        results = evaluate_budgets(
            {"residentCpuPercentP95": 0.4, "activityFootprintBytesPeak": None},
            budgets,
        )

        self.assertEqual(results[0]["status"], "pass")
        self.assertEqual(results[1]["status"], "unknown")
        self.assertIsNone(results[1]["observed"])

    def test_report_never_serializes_pid_or_local_path(self):
        report = build_report(
            product_version="0.4.4",
            product_build="8",
            architecture="arm64",
            macos_version="15.6",
            app_bytes=50 * MIB,
            idle_rounds=[{"role": "status", "cpuPercent": 0.1}],
            idle_summary={"residentCpuPercentP95": 0.1},
            refresh_rounds=[{"durationSeconds": 2.5, "status": "success"}],
            budget_results=[],
        )
        serialized = json.dumps(report, sort_keys=True)

        self.assertNotIn("pid", serialized.lower())
        self.assertNotIn(str(Path.home()), serialized)
        self.assertNotIn("command", serialized.lower())
        self.assertEqual(report["schemaVersion"], 1)

    def test_bundle_metadata_rejects_path_shaped_version(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp) / "OpenUsage Bar.app"
            contents = app / "Contents"
            contents.mkdir(parents=True)
            with (contents / "Info.plist").open("wb") as handle:
                plistlib.dump(
                    {
                        "CFBundleIdentifier": "com.lune.openusagebar",
                        "CFBundleShortVersionString": str(Path.home()),
                        "CFBundleVersion": "8",
                    },
                    handle,
                )

            with self.assertRaises(ValueError):
                _bundle_metadata(app)

    def test_measurement_plan_requires_three_nontrivial_rounds(self):
        with self.assertRaises(ValueError):
            validate_measurement_plan(rounds=2, idle_seconds=10)
        with self.assertRaises(ValueError):
            validate_measurement_plan(rounds=3, idle_seconds=0.5)

        validate_measurement_plan(rounds=3, idle_seconds=1)

    def test_refresh_command_uses_the_installed_cli_argument_contract(self):
        app = Path("/Applications/OpenUsage Bar.app")

        command = _refresh_command(app)

        self.assertEqual(
            command[1:],
            ["status", "--format", "json", "--fresh"],
        )

    def test_refresh_timeout_reaps_the_process_group(self):
        with tempfile.TemporaryDirectory() as temp:
            survived = Path(temp) / "survived"
            command = [
                sys.executable,
                "-c",
                (
                    "import subprocess, sys, time; "
                    "subprocess.Popen([sys.executable, '-c', "
                    f"\"import pathlib,time; time.sleep(1); pathlib.Path({str(survived)!r}).write_text('bad')\""
                    "]); time.sleep(30)"
                ),
            ]

            result = run_refresh(command, timeout_seconds=0.4)
            time.sleep(1.1)

            self.assertEqual(result["status"], "timeout")
            self.assertIsNone(result["exitCode"])
            self.assertLess(result["durationSeconds"], 3)
            self.assertFalse(survived.exists())

    def test_committed_baseline_is_three_rounds_and_privacy_safe(self):
        payload = json.loads(BASELINE.read_text(encoding="utf-8"))
        serialized = json.dumps(payload, sort_keys=True)

        self.assertEqual(len(payload["idle"]["rounds"]), 3)
        self.assertEqual(payload["refresh"]["summary"]["sampleCount"], 3)
        self.assertEqual(
            payload["refresh"]["perSourceTiming"], "not_observable"
        )
        self.assertTrue(
            all(item["status"] == "pass" for item in payload["budgets"])
        )
        self.assertFalse(any(payload["privacy"].values()))
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn('"pid"', serialized.lower())

    def test_guide_blocks_language_migration_without_repeated_evidence(self):
        guide = GUIDE.read_text(encoding="utf-8")

        for required in (
            "No resident-host migration is justified",
            "Python remains the sole ledger writer",
            "two same-version baselines after separate boots",
            "perSourceTiming",
            "scripts/measure_performance.py",
        ):
            self.assertIn(required, guide)


if __name__ == "__main__":
    unittest.main()
