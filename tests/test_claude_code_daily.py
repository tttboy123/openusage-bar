import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from openusage_bar import claude_code_daily
from openusage_bar.claude_code_daily import ClaudeCodeLocalDailyImporter
from openusage_bar.providers.contracts import ImportFailure, UsageImportSuccess


def _assistant_line(
    *,
    timestamp: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_creation: int = 0,
) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": timestamp,
            "model": model,
            "message": {
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_creation,
                }
            },
        },
        ensure_ascii=False,
    )


def _build_session(project: Path, name: str, lines: list[str]) -> Path:
    path = project / f"{name}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class ClaudeCodeLocalDailyImporterTests(unittest.TestCase):
    def test_aggregates_assistant_usage_by_day_and_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            projects.mkdir()
            _build_session(
                projects,
                "session-a",
                [
                    _assistant_line(
                        timestamp="2026-07-05T04:00:00Z",
                        model="claude-sonnet-4-6",
                        input_tokens=100,
                        output_tokens=50,
                        cache_read=10,
                        cache_creation=5,
                    ),
                    _assistant_line(
                        timestamp="2026-07-05T05:00:00Z",
                        model="claude-sonnet-4-6",
                        input_tokens=20,
                        output_tokens=5,
                    ),
                    _assistant_line(
                        timestamp="2026-07-06T04:00:00Z",
                        model="claude-opus-4-8",
                        input_tokens=30,
                        output_tokens=4,
                    ),
                ],
            )
            importer = ClaudeCodeLocalDailyImporter(
                session_roots=(projects,),
                cache_path=root / "cache.json",
                local_timezone=timezone.utc,
                clock=lambda: datetime(2026, 7, 29, tzinfo=timezone.utc),
            )
            result = importer.fetch_usage(date(2026, 7, 1), date(2026, 7, 31))
            self.assertIsInstance(result, UsageImportSuccess)
            rows = {row.model_id: row for row in result.rows}
            self.assertEqual(rows["claude-sonnet-4-6"].total_tokens, 190)
            self.assertEqual(rows["claude-sonnet-4-6"].input_tokens, 120)
            self.assertEqual(rows["claude-sonnet-4-6"].cache_read_tokens, 10)
            self.assertEqual(rows["claude-opus-4-8"].total_tokens, 34)

    def test_incremental_append_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            projects.mkdir()
            path = _build_session(
                projects,
                "session-a",
                [
                    _assistant_line(
                        timestamp="2026-07-05T04:00:00Z",
                        model="claude-sonnet-4-6",
                        input_tokens=100,
                        output_tokens=50,
                    )
                ],
            )
            importer = ClaudeCodeLocalDailyImporter(
                session_roots=(projects,),
                cache_path=root / "cache.json",
                local_timezone=timezone.utc,
            )
            first = importer.fetch_usage(date(2026, 7, 1), date(2026, 7, 31))
            self.assertEqual(
                first.rows[0].total_tokens if first.rows else 0, 150
            )
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    _assistant_line(
                        timestamp="2026-07-05T06:00:00Z",
                        model="claude-sonnet-4-6",
                        input_tokens=10,
                        output_tokens=5,
                    )
                    + "\n"
                )
            second = importer.fetch_usage(date(2026, 7, 1), date(2026, 7, 31))
            row = second.rows[0]
            self.assertEqual(row.total_tokens, 165)
            self.assertEqual(row.input_tokens, 110)

    def test_missing_sessions_returns_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            importer = ClaudeCodeLocalDailyImporter(
                session_roots=(root / "missing",),
                cache_path=root / "cache.json",
            )
            result = importer.fetch_usage(date(2026, 7, 1), date(2026, 7, 31))
            self.assertIsInstance(result, ImportFailure)
            self.assertEqual(result.error_code, "sessions_unavailable")

    def test_filters_rows_outside_requested_range(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            projects.mkdir()
            _build_session(
                projects,
                "session-a",
                [
                    _assistant_line(
                        timestamp="2026-07-05T04:00:00Z",
                        model="claude-sonnet-4-6",
                        input_tokens=100,
                        output_tokens=50,
                    ),
                    _assistant_line(
                        timestamp="2026-07-20T04:00:00Z",
                        model="claude-opus-4-8",
                        input_tokens=30,
                        output_tokens=4,
                    ),
                ],
            )
            importer = ClaudeCodeLocalDailyImporter(
                session_roots=(projects,),
                cache_path=root / "cache.json",
                local_timezone=timezone.utc,
            )
            result = importer.fetch_usage(date(2026, 7, 1), date(2026, 7, 10))
            self.assertEqual({row.model_id for row in result.rows}, {"claude-sonnet-4-6"})

    def test_cache_round_trip_across_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            projects.mkdir()
            cache = root / "cache.json"
            _build_session(
                projects,
                "session-a",
                [
                    _assistant_line(
                        timestamp="2026-07-05T04:00:00Z",
                        model="claude-sonnet-4-6",
                        input_tokens=100,
                        output_tokens=50,
                    )
                ],
            )
            first = ClaudeCodeLocalDailyImporter(
                session_roots=(projects,),
                cache_path=cache,
                local_timezone=timezone.utc,
            )
            first_result = first.fetch_usage(date(2026, 7, 1), date(2026, 7, 31))
            self.assertEqual(first_result.rows[0].total_tokens, 150)
            self.assertTrue(cache.is_file())

            second = ClaudeCodeLocalDailyImporter(
                session_roots=(projects,),
                cache_path=cache,
                local_timezone=timezone.utc,
            )
            second_result = second.fetch_usage(date(2026, 7, 1), date(2026, 7, 31))
            self.assertEqual(second_result.rows[0].total_tokens, 150)

    def test_invalid_cache_payload_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            projects.mkdir()
            cache = root / "cache.json"
            cache.write_text("{not valid json", encoding="utf-8")
            _build_session(
                projects,
                "session-a",
                [
                    _assistant_line(
                        timestamp="2026-07-05T04:00:00Z",
                        model="claude-sonnet-4-6",
                        input_tokens=100,
                        output_tokens=50,
                    )
                ],
            )
            importer = ClaudeCodeLocalDailyImporter(
                session_roots=(projects,),
                cache_path=cache,
                local_timezone=timezone.utc,
            )
            result = importer.fetch_usage(date(2026, 7, 1), date(2026, 7, 31))
            self.assertEqual(result.rows[0].total_tokens, 150)

    def test_malformed_cache_state_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            projects.mkdir()
            cache = root / "cache.json"
            cache.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "states": {
                            "deadbeef": {
                                "key": "other",
                                "device": True,
                                "inode": 1,
                                "offset": 0,
                                "mtimeNs": 0,
                                "tailDigest": "",
                                "rows": [],
                                "pending": [],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            _build_session(
                projects,
                "session-a",
                [
                    _assistant_line(
                        timestamp="2026-07-05T04:00:00Z",
                        model="claude-sonnet-4-6",
                        input_tokens=100,
                        output_tokens=50,
                    )
                ],
            )
            importer = ClaudeCodeLocalDailyImporter(
                session_roots=(projects,),
                cache_path=cache,
                local_timezone=timezone.utc,
            )
            result = importer.fetch_usage(date(2026, 7, 1), date(2026, 7, 31))
            self.assertEqual(result.rows[0].total_tokens, 150)

    def test_relative_cache_path_rejected(self):
        with self.assertRaises(ValueError):
            ClaudeCodeLocalDailyImporter(cache_path=Path("relative.json"))

    def test_invalid_request_range(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            importer = ClaudeCodeLocalDailyImporter(
                session_roots=(root / "missing",),
                cache_path=root / "cache.json",
            )
            result = importer.fetch_usage(date(2026, 7, 31), date(2026, 7, 1))
            self.assertIsInstance(result, ImportFailure)
            self.assertEqual(result.error_code, "invalid_request")

    def test_oversized_line_returns_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            projects.mkdir()
            _build_session(
                projects,
                "session-a",
                [
                    _assistant_line(
                        timestamp="2026-07-05T04:00:00Z",
                        model="claude-sonnet-4-6",
                        input_tokens=100,
                        output_tokens=50,
                    )
                ],
            )
            importer = ClaudeCodeLocalDailyImporter(
                session_roots=(projects,),
                cache_path=root / "cache.json",
                local_timezone=timezone.utc,
            )
            with patch.object(claude_code_daily, "MAX_RELEVANT_LINE_BYTES", 64):
                result = importer.fetch_usage(date(2026, 7, 1), date(2026, 7, 31))
            self.assertIsInstance(result, UsageImportSuccess)
            self.assertEqual(result.rows, ())

    def test_malformed_json_line_returns_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            projects.mkdir()
            _build_session(projects, "session-a", ['{"type": "assistant", broken'])
            importer = ClaudeCodeLocalDailyImporter(
                session_roots=(projects,),
                cache_path=root / "cache.json",
                local_timezone=timezone.utc,
            )
            result = importer.fetch_usage(date(2026, 7, 1), date(2026, 7, 31))
            self.assertIsInstance(result, UsageImportSuccess)
            self.assertEqual(result.rows, ())

    def test_pending_rows_for_unknown_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            projects.mkdir()
            line = json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-07-05T04:00:00Z",
                    "message": {
                        "usage": {"input_tokens": 10, "output_tokens": 5}
                    },
                }
            )
            _build_session(projects, "session-a", [line])
            importer = ClaudeCodeLocalDailyImporter(
                session_roots=(projects,),
                cache_path=root / "cache.json",
                local_timezone=timezone.utc,
            )
            result = importer.fetch_usage(date(2026, 7, 1), date(2026, 7, 31))
            self.assertEqual({row.model_id for row in result.rows}, {"unknown"})
            self.assertEqual(result.rows[0].total_tokens, 15)

    def test_session_boundary_exceeded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            projects.mkdir()
            _build_session(
                projects,
                "session-a",
                [
                    _assistant_line(
                        timestamp="2026-07-05T04:00:00Z",
                        model="claude-sonnet-4-6",
                        input_tokens=100,
                        output_tokens=50,
                    )
                ],
            )
            _build_session(
                projects,
                "session-b",
                [
                    _assistant_line(
                        timestamp="2026-07-05T05:00:00Z",
                        model="claude-opus-4-8",
                        input_tokens=30,
                        output_tokens=4,
                    )
                ],
            )
            importer = ClaudeCodeLocalDailyImporter(
                session_roots=(projects,),
                cache_path=root / "cache.json",
                local_timezone=timezone.utc,
            )
            with patch.object(claude_code_daily, "MAX_SESSION_FILES", 1):
                result = importer.fetch_usage(date(2026, 7, 1), date(2026, 7, 31))
            self.assertIsInstance(result, ImportFailure)
            self.assertEqual(result.error_code, "sessions_invalid")


if __name__ == "__main__":
    unittest.main()
