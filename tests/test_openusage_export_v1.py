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
        boolean = json.loads(json.dumps(base))
        boolean["rows"][0]["input_tokens"] = True
        mutations.append(boolean)
        total = json.loads(json.dumps(base))
        total["rows"][0]["total_tokens"] = 4
        mutations.append(total)
        scope = json.loads(json.dumps(base))
        scope["rows"][0]["provider_id"] = "other"
        mutations.append(scope)
        duplicate = json.loads(json.dumps(base))
        duplicate["rows"].append(dict(duplicate["rows"][0]))
        mutations.append(duplicate)
        huge = json.loads(json.dumps(base))
        huge["rows"][0]["input_tokens"] = 2**63
        huge["rows"][0]["total_tokens"] = 2**63 + 1
        mutations.append(huge)
        for payload in mutations:
            with self.assertRaises(ExportDecodeError):
                decode_daily(json.dumps(payload))

    def test_validates_range_page_cursor_enums_and_request_echo(self):
        from openusage_bar.openusage_export_v1 import ExportDecodeError, decode_daily

        base = json.loads(fixture("daily-usage.json"))
        mutations = []
        for path, value in (
            (("request", "limit"), 0),
            (("request", "until"), "2027-07-03"),
            (("coverage", "until"), "2026-07-03"),
            (("rows", 0, "quality"), "fallback"),
            (("rows", 0, "token_counting_convention"), "unknown"),
        ):
            payload = json.loads(json.dumps(base))
            target = payload
            for component in path[:-1]:
                target = target[component]
            target[path[-1]] = value
            mutations.append(payload)
        bad_cursor = json.loads(json.dumps(base))
        bad_cursor["page"] = {"next_cursor": "bad cursor", "complete": False}
        mutations.append(bad_cursor)
        cursor_on_complete = json.loads(json.dumps(base))
        cursor_on_complete["page"]["next_cursor"] = "opaque"
        mutations.append(cursor_on_complete)
        for payload in mutations:
            with self.assertRaises(ExportDecodeError):
                decode_daily(json.dumps(payload))

    def test_rejects_forbidden_privacy_key_at_any_depth(self):
        from openusage_bar.openusage_export_v1 import ExportDecodeError, decode_daily

        base = json.loads(fixture("daily-usage.json"))
        for key in (
            "api_key",
            "cookie",
            "session_id",
            "prompt_tokens",
            "response",
            "raw_payload",
            "account_id",
            "access_token",
            "endpoint_url",
            "device_id",
        ):
            with self.subTest(key=key):
                payload = json.loads(json.dumps(base))
                payload["future"] = {"nested": {key: "secret"}}
                with self.assertRaises(ExportDecodeError):
                    decode_daily(json.dumps(payload))


class OpenUsageExportV1ProbeTests(unittest.TestCase):
    @staticmethod
    def _completed(payload, returncode: int = 0):
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.CompletedProcess([], returncode, stdout, "private stderr")

    @staticmethod
    def _capabilities():
        return OpenUsageExportV1ProbeTests._completed(fixture("capabilities.json"))

    @staticmethod
    def _daily(name: str = "daily-usage.json") -> dict:
        return json.loads(fixture(name))

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

    def test_supported_v1_is_selected_without_running_or_summing_legacy(self):
        from openusage_bar.daily_history import (
            EXPORT_V1_SOURCE_ID,
            OpenUsageDailyImporter,
        )

        runner = Mock(
            side_effect=[self._capabilities(), self._completed(self._daily())]
        )
        importer = OpenUsageDailyImporter(runner=runner)
        result = importer.fetch("codex", date(2026, 7, 1), date(2026, 7, 2))

        self.assertTrue(result.ok)
        self.assertEqual(result.source_id, EXPORT_V1_SOURCE_ID)
        self.assertEqual([row.total_tokens for row in result.rows], [3])
        self.assertEqual(runner.call_count, 2)
        command = runner.call_args_list[1].args[0]
        self.assertEqual(
            command,
            [
                importer.openusage_path,
                "export",
                "--output",
                "-",
                "--format",
                "json",
                "--contract",
                "openusage-export/v1",
                "--kind",
                "daily_usage",
                "--provider",
                "codex",
                "--since",
                "2026-07-01",
                "--until",
                "2026-07-02",
                "--limit",
                "500",
            ],
        )

    def test_only_complete_empty_v1_can_report_covered_zero(self):
        from openusage_bar.daily_history import OpenUsageDailyImporter

        runner = Mock(
            side_effect=[
                self._capabilities(),
                self._completed(self._daily("daily-usage-empty-complete.json")),
            ]
        )
        result = OpenUsageDailyImporter(runner=runner).fetch(
            "codex", date(2026, 7, 1), date(2026, 7, 2)
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.rows, ())
        self.assertTrue(result.covered_zero)
        self.assertEqual(runner.call_count, 2)

    def test_multi_page_v1_is_validated_before_rows_are_published(self):
        from openusage_bar.daily_history import OpenUsageDailyImporter

        first = self._daily()
        first["page"] = {"next_cursor": "opaque-cursor", "complete": False}
        second = self._daily()
        second["request"]["cursor"] = "opaque-cursor"
        second["rows"][0].update(
            {"day": "2026-07-02", "model_id": "gpt-5.5"}
        )
        runner = Mock(
            side_effect=[
                self._capabilities(), self._completed(first), self._completed(second)
            ]
        )

        result = OpenUsageDailyImporter(runner=runner).fetch(
            "codex", date(2026, 7, 1), date(2026, 7, 2)
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            [(row.day, row.model_id) for row in result.rows],
            [("2026-07-01", "gpt-5"), ("2026-07-02", "gpt-5.5")],
        )
        self.assertEqual(
            runner.call_args_list[2].args[0][-2:], ["--cursor", "opaque-cursor"]
        )

    def test_invalid_or_partial_v1_falls_back_once_to_legacy(self):
        from openusage_bar.daily_history import DAILY_SOURCE_ID, OpenUsageDailyImporter

        legacy = {
            "kind": "daily",
            "rows": [
                {
                    "key": "2026-07-01",
                    "model_breakdown": [
                        {
                            "key": "gpt-legacy",
                            "input_tokens": 4,
                            "output_tokens": 1,
                            "cache_read_tokens": 0,
                            "cache_creation_tokens": 0,
                            "reasoning_tokens": None,
                            "total_tokens": 5,
                            "cost_usd": None,
                        }
                    ],
                }
            ],
            "totals": {},
        }
        partial = self._daily("daily-usage-partial.json")
        malformed = self._daily()
        malformed["request"]["provider_id"] = "other"
        loop = self._daily()
        loop["page"] = {"next_cursor": "same", "complete": False}
        loop_next = self._daily()
        loop_next["request"]["cursor"] = "same"
        loop_next["page"] = {"next_cursor": "same", "complete": False}
        cases = (
            [self._completed(partial)],
            [self._completed(malformed)],
            [self._completed(loop), self._completed(loop_next)],
            [self._completed(loop), self._completed("", returncode=9)],
        )
        for v1_calls in cases:
            with self.subTest(case=len(v1_calls)):
                runner = Mock(
                    side_effect=[
                        self._capabilities(), *v1_calls, self._completed(legacy)
                    ]
                )
                result = OpenUsageDailyImporter(runner=runner).fetch(
                    "codex", date(2026, 7, 1), date(2026, 7, 2)
                )
                self.assertTrue(result.ok)
                self.assertEqual(result.source_id, DAILY_SOURCE_ID)
                self.assertFalse(result.covered_zero)
                self.assertEqual([row.total_tokens for row in result.rows], [5])
                self.assertEqual(runner.call_args_list[-1].args[0][1], "daily")

if __name__ == "__main__":
    unittest.main()
