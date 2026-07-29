from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openusage_bar.performance_timing import (
    RefreshTimingRecorder,
    measure_source_call,
    write_timing_report,
)


class PerformanceTimingTests(unittest.TestCase):
    def test_recorder_aggregates_only_bounded_source_classes(self):
        ticks = iter((10.0, 10.25, 20.0, 20.75))
        recorder = RefreshTimingRecorder(monotonic=lambda: next(ticks))

        self.assertEqual(
            measure_source_call(
                recorder,
                "network",
                lambda: type("Result", (), {"ok": True})(),
            ).ok,
            True,
        )
        self.assertEqual(
            measure_source_call(
                recorder,
                "network",
                lambda: type(
                    "Result",
                    (),
                    {"ok": False, "error_code": "rate_limited"},
                )(),
            ).error_code,
            "rate_limited",
        )

        payload = recorder.snapshot()

        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["scope"], "source-class")
        self.assertEqual(
            payload["classes"],
            [
                {
                    "sourceClass": "network",
                    "sampleCount": 2,
                    "durationSecondsTotal": 1.0,
                    "durationSecondsMax": 0.75,
                    "outcomes": {
                        "success": 1,
                        "backoff": 1,
                        "timeout": 0,
                        "unavailable": 0,
                        "failed": 0,
                    },
                }
            ],
        )
        serialized = json.dumps(payload, sort_keys=True)
        for forbidden in (
            "provider",
            "account",
            "endpoint",
            "path",
            "command",
            "payload",
        ):
            self.assertNotIn(forbidden, serialized.lower())

    def test_exception_is_timed_and_reraised_without_error_text(self):
        ticks = iter((1.0, 1.5))
        recorder = RefreshTimingRecorder(monotonic=lambda: next(ticks))

        with self.assertRaisesRegex(RuntimeError, "private detail"):
            measure_source_call(
                recorder,
                "child_process",
                lambda: (_ for _ in ()).throw(RuntimeError("private detail")),
            )

        serialized = json.dumps(recorder.snapshot(), sort_keys=True)
        self.assertIn('"failed": 1', serialized)
        self.assertNotIn("private detail", serialized)

    def test_invalid_source_class_is_rejected(self):
        recorder = RefreshTimingRecorder()

        with self.assertRaises(ValueError):
            measure_source_call(recorder, "provider-name", lambda: None)

    def test_report_writer_is_exclusive_and_mode_0600(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "timing.json"
            recorder = RefreshTimingRecorder()

            write_timing_report(output, recorder.snapshot())

            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                write_timing_report(output, recorder.snapshot())

    def test_report_writer_rejects_extra_identity_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "timing.json"
            payload = RefreshTimingRecorder().snapshot()
            payload["provider"] = "private-provider"

            with self.assertRaises(ValueError):
                write_timing_report(output, payload)

            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
