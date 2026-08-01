import json
import unittest
from pathlib import Path


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
