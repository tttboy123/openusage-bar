import tempfile
import unittest
from datetime import date, timedelta, timezone
from pathlib import Path

from openusage_bar.codex_attribution import (
    CodexAttributionResolver,
    _path_day,
    _timestamp,
)


class CodexAttributionBoundaryTests(unittest.TestCase):
    def test_timestamp_and_calendar_path_parsing_fail_closed(self):
        self.assertIsNone(_timestamp(None))
        self.assertIsNone(_timestamp("not-a-time"))
        self.assertIsNone(_timestamp("2026-07-18T10:00:00"))
        self.assertEqual(
            _timestamp("2026-07-18T10:00:00Z").tzinfo, timezone.utc
        )
        self.assertEqual(
            _path_day(Path("sessions/2026/07/18/fixture.jsonl")),
            date(2026, 7, 18),
        )
        self.assertIsNone(_path_day(Path("sessions/2026/7/18/fixture.jsonl")))

    def test_invalid_range_file_limit_and_missing_root_return_no_attribution(self):
        missing = Path("/tmp/openusage-fixture-root-that-does-not-exist")
        resolver = CodexAttributionResolver(missing, max_files=1)
        today = date(2026, 7, 18)

        self.assertEqual(resolver.target_models(today, today - timedelta(days=1)), {})
        self.assertEqual(
            CodexAttributionResolver(missing, max_files=0).target_models(today, today),
            {},
        )
        self.assertEqual(resolver.target_models(today, today), {})

    def test_malformed_matching_event_makes_dated_session_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "2026/07/18/fixture.jsonl"
            session.parent.mkdir(parents=True)
            session.write_bytes(b'{"type":"turn_context",broken}\n')

            result = CodexAttributionResolver(root).target_models(
                date(2026, 7, 18), date(2026, 7, 18)
            )

        self.assertEqual(result, {})

    def test_multiple_models_never_guess_one_model_for_the_day(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "2026/07/18/fixture.jsonl"
            session.parent.mkdir(parents=True)
            session.write_text(
                '{"type":"event_msg","timestamp":"2026-07-18T01:00:00Z",'
                '"payload":{"type":"token_count"}}\n'
                '{"type":"turn_context","payload":{"model":"gpt-a"}}\n'
                '{"type":"turn_context","payload":{"model":"gpt-b"}}\n',
                encoding="utf-8",
            )

            result = CodexAttributionResolver(root).target_models(
                date(2026, 7, 18), date(2026, 7, 18)
            )

        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
