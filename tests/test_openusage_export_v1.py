import json
import subprocess
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch


FIXTURES = Path(__file__).parent / "fixtures" / "openusage-export-v1"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class OpenUsageExportV1DecoderTests(unittest.TestCase):
    def test_decodes_frozen_capabilities(self):
        from openusage_bar.openusage_export_v1 import decode_capabilities

        result = decode_capabilities(fixture("capabilities.json"))
        self.assertEqual(result.contract, "openusage-export/v1")
        self.assertEqual(result.kinds, ("capabilities", "daily_usage"))
        self.assertEqual(result.max_range_days, 366)
        self.assertEqual(result.max_page_size, 1000)

    def test_decodes_daily_and_complete_empty_only_means_covered_zero(self):
        from openusage_bar.openusage_export_v1 import decode_daily

        page = decode_daily(fixture("daily-usage.json"))
        self.assertEqual(page.request.provider_id, "codex")
        self.assertEqual(page.rows[0].total_tokens, 3)
        self.assertFalse(page.covered_zero)
        empty = decode_daily(fixture("daily-usage-empty-complete.json"))
        self.assertTrue(empty.covered_zero)
        partial = decode_daily(fixture("daily-usage-partial.json"))
        self.assertEqual(partial.coverage.state, "partial")
        self.assertFalse(partial.covered_zero)

    def test_additive_fields_are_accepted(self):
        from openusage_bar.openusage_export_v1 import decode_daily

        payload = json.loads(fixture("daily-usage.json"))
        payload["future"] = {"safe_metric": 1}
        payload["rows"][0]["future_metric"] = "supported later"
        self.assertEqual(decode_daily(json.dumps(payload)).rows[0].model_id, "gpt-5")

    def test_rejects_missing_required_wrong_header_and_non_utc_time(self):
        from openusage_bar.openusage_export_v1 import ExportDecodeError, decode_daily

        payload = json.loads(fixture("daily-usage.json"))
        for path, value in (
            (("contract",), "wrong"),
            (("schema_version",), "2"),
            (("kind",), "capabilities"),
            (("generated_at",), "2026-08-01T01:00:00+01:00"),
        ):
            broken = dict(payload)
            broken[path[0]] = value
            with self.assertRaises(ExportDecodeError):
                decode_daily(json.dumps(broken))
        missing = dict(payload)
        del missing["coverage"]
        with self.assertRaises(ExportDecodeError):
            decode_daily(json.dumps(missing))

    def test_rejects_boolean_tokens_bad_totals_scope_and_duplicates(self):
        from openusage_bar.openusage_export_v1 import ExportDecodeError, decode_daily

        base = json.loads(fixture("daily-usage.json"))
        mutations = []
        boolean = json.loads(json.dumps(base)); boolean["rows"][0]["input_tokens"] = True; mutations.append(boolean)
        total = json.loads(json.dumps(base)); total["rows"][0]["total_tokens"] = 4; mutations.append(total)
        scope = json.loads(json.dumps(base)); scope["rows"][0]["provider_id"] = "other"; mutations.append(scope)
        duplicate = json.loads(json.dumps(base)); duplicate["rows"].append(dict(duplicate["rows"][0])); mutations.append(duplicate)
        for payload in mutations:
            with self.assertRaises(ExportDecodeError):
                decode_daily(json.dumps(payload))


class OpenUsageExportV1ProbeTests(unittest.TestCase):
    def test_probe_uses_exact_bounded_direct_command_and_safe_environment(self):
        from openusage_bar.daily_history import OpenUsageDailyImporter

        completed = subprocess.CompletedProcess(
            [], 0, fixture("capabilities.json"), "ignored private stderr"
        )
        with patch(
            "openusage_bar.daily_history.run_bounded", return_value=completed
        ) as runner:
            importer = OpenUsageDailyImporter(
                openusage_path="/opt/bin/openusage",
                environment={"PATH": "/usr/bin", "API_KEY": "must-not-pass"},
                path_exists=lambda _path: False,
            )
            first = importer.export_v1_capabilities()
            second = importer.export_v1_capabilities()

        self.assertTrue(first.supported)
        self.assertIs(first, second)
        runner.assert_called_once()
        self.assertEqual(
            runner.call_args.args[0],
            [
                "/opt/bin/openusage",
                "export",
                "--output",
                "-",
                "--format",
                "json",
                "--contract",
                "openusage-export/v1",
                "--kind",
                "capabilities",
            ],
        )
        options = runner.call_args.kwargs
        self.assertFalse(options["shell"])
        self.assertIs(options["stdin"], subprocess.DEVNULL)
        self.assertIs(options["stdout"], subprocess.PIPE)
        self.assertIs(options["stderr"], subprocess.PIPE)
        self.assertEqual(options["timeout"], 3)
        self.assertEqual(options["stdout_limit"], 128 * 1024)
        self.assertEqual(options["stderr_limit"], 128 * 1024)
        self.assertEqual(options["env"]["PATH"], "/usr/bin")
        self.assertNotIn("API_KEY", options["env"])

    def test_probe_failures_are_cached_as_sanitized_unsupported(self):
        from openusage_bar.bounded_process import BoundedProcessError
        from openusage_bar.daily_history import OpenUsageDailyImporter

        private = "Bearer secret cookie=session"
        cases = (
            Mock(side_effect=BoundedProcessError("output_overflow")),
            Mock(side_effect=subprocess.TimeoutExpired(private, 3)),
            Mock(side_effect=OSError(private)),
            Mock(return_value=subprocess.CompletedProcess([], 7, private, private)),
            Mock(return_value=subprocess.CompletedProcess([], 0, private, private)),
            Mock(
                return_value=subprocess.CompletedProcess(
                    [],
                    0,
                    fixture("capabilities.json").replace(
                        '"schema_version": "1"', '"schema_version": "2"'
                    ),
                    private,
                )
            ),
        )
        for runner in cases:
            with self.subTest(runner=runner):
                importer = OpenUsageDailyImporter(runner=runner)
                first = importer.export_v1_capabilities()
                second = importer.export_v1_capabilities()
                self.assertFalse(first.supported)
                self.assertIs(first, second)
                self.assertEqual(runner.call_count, 1)
                self.assertNotIn("secret", repr(first))
                self.assertNotIn("cookie", repr(first))
                self.assertNotIn("Bearer", repr(first))

    def test_fetch_probes_once_then_uses_legacy_for_unsupported_binary(self):
        from openusage_bar.daily_history import OpenUsageDailyImporter

        legacy = {"kind": "daily", "rows": [], "totals": {}}
        runner = Mock(
            side_effect=[
                subprocess.CompletedProcess([], 2, "", "unsupported"),
                subprocess.CompletedProcess([], 0, json.dumps(legacy), ""),
                subprocess.CompletedProcess([], 0, json.dumps(legacy), ""),
            ]
        )
        importer = OpenUsageDailyImporter(runner=runner)
        first = importer.fetch("codex", date(2026, 7, 1), date(2026, 7, 2))
        second = importer.fetch("codex", date(2026, 7, 1), date(2026, 7, 2))

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(runner.call_count, 3)
        self.assertEqual(runner.call_args_list[1].args[0][1], "daily")
        self.assertEqual(runner.call_args_list[2].args[0][1], "daily")

    def test_validates_range_page_cursor_enums_and_request_echo(self):
        from openusage_bar.openusage_export_v1 import ExportDecodeError, decode_daily

        base = json.loads(fixture("daily-usage.json"))
        mutations = []
        bad_limit = json.loads(json.dumps(base)); bad_limit["request"]["limit"] = 0; mutations.append(bad_limit)
        too_long = json.loads(json.dumps(base)); too_long["request"]["until"] = "2027-07-03"; mutations.append(too_long)
        wrong_coverage = json.loads(json.dumps(base)); wrong_coverage["coverage"]["until"] = "2026-07-03"; mutations.append(wrong_coverage)
        bad_quality = json.loads(json.dumps(base)); bad_quality["rows"][0]["quality"] = "fallback"; mutations.append(bad_quality)
        bad_convention = json.loads(json.dumps(base)); bad_convention["rows"][0]["token_counting_convention"] = "unknown"; mutations.append(bad_convention)
        bad_cursor = json.loads(json.dumps(base)); bad_cursor["page"] = {"next_cursor": "bad cursor", "complete": False}; mutations.append(bad_cursor)
        cursor_on_complete = json.loads(json.dumps(base)); cursor_on_complete["page"]["next_cursor"] = "opaque"; mutations.append(cursor_on_complete)
        for payload in mutations:
            with self.assertRaises(ExportDecodeError):
                decode_daily(json.dumps(payload))

    def test_rejects_forbidden_privacy_key_at_any_depth(self):
        from openusage_bar.openusage_export_v1 import ExportDecodeError, decode_daily

        base = json.loads(fixture("daily-usage.json"))
        for key in ("api_key", "cookie", "session_id", "prompt", "response", "raw_payload", "account_id"):
            payload = json.loads(json.dumps(base))
            payload["future"] = {"nested": {key: "secret"}}
            with self.assertRaises(ExportDecodeError):
                decode_daily(json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
