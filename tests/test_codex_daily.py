from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from openusage_bar.codex_daily import CodexLocalDailyImporter
from openusage_bar.providers.contracts import ImportFailure, UsageImportSuccess


NOW = datetime(2026, 7, 18, 6, 0, tzinfo=timezone.utc)
SGT = timezone(timedelta(hours=8))


def model_event(timestamp: str, model: str = "gpt-5.6-sol") -> dict:
    return {
        "timestamp": timestamp,
        "type": "turn_context",
        "payload": {"model": model, "cwd": "/private/not-persisted"},
    }


def token_event(
    timestamp: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int,
    total_tokens: int,
    cumulative_total: int | None = None,
) -> dict:
    last = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": 0,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": 5,
        "total_tokens": total_tokens,
    }
    cumulative = dict(last)
    cumulative["total_tokens"] = cumulative_total or total_tokens
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": last,
                "total_token_usage": cumulative,
            },
        },
    }


def write_events(path: Path, events: list[dict], *, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


class CodexLocalDailyImporterTests(unittest.TestCase):
    def importer(self, root: Path) -> CodexLocalDailyImporter:
        return CodexLocalDailyImporter(
            session_roots=(root,), local_timezone=SGT, clock=lambda: NOW
        )

    def test_usage_only_contract_explicitly_disables_cost_import(self):
        self.assertEqual(CodexLocalDailyImporter.usage_source_id, "codex.local_sessions")
        self.assertIsNone(CodexLocalDailyImporter.cost_source_id)

    def test_local_day_uses_last_usage_total_without_double_counting_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_events(root / "session.jsonl", [
                model_event("2026-07-16T16:01:00Z"),
                token_event(
                    "2026-07-16T16:02:00Z",
                    input_tokens=100,
                    output_tokens=20,
                    cached_input_tokens=80,
                    total_tokens=120,
                ),
            ])

            result = self.importer(root).fetch_usage(
                date(2026, 7, 17), date(2026, 7, 17)
            )

        self.assertIsInstance(result, UsageImportSuccess)
        row = result.rows[0]
        self.assertEqual(row.day, "2026-07-17")
        self.assertEqual(row.model_id, "gpt-5.6-sol")
        self.assertEqual(row.input_tokens, 100)
        self.assertEqual(row.cache_read_tokens, 80)
        self.assertEqual(row.output_tokens, 20)
        self.assertEqual(row.total_tokens, 120)

    def test_append_refresh_adds_only_new_events_and_truncation_rebuilds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session.jsonl"
            write_events(path, [
                model_event("2026-07-17T01:00:00Z"),
                token_event(
                    "2026-07-17T01:01:00Z", input_tokens=70,
                    output_tokens=10, cached_input_tokens=50, total_tokens=80,
                ),
            ])
            importer = self.importer(root)
            first = importer.fetch_usage(date(2026, 7, 17), date(2026, 7, 17))
            write_events(path, [token_event(
                "2026-07-17T01:02:00Z", input_tokens=30,
                output_tokens=10, cached_input_tokens=20, total_tokens=40,
            )], append=True)
            second = importer.fetch_usage(date(2026, 7, 17), date(2026, 7, 17))
            write_events(path, [
                model_event("2026-07-17T01:03:00Z", "gpt-5.6-terra"),
                token_event(
                    "2026-07-17T01:04:00Z", input_tokens=5,
                    output_tokens=1, cached_input_tokens=4, total_tokens=6,
                ),
            ])
            rebuilt = importer.fetch_usage(date(2026, 7, 17), date(2026, 7, 17))

        self.assertEqual(sum(row.total_tokens for row in first.rows), 80)
        self.assertEqual(sum(row.total_tokens for row in second.rows), 120)
        self.assertEqual(
            [(row.model_id, row.total_tokens) for row in rebuilt.rows],
            [("gpt-5.6-terra", 6)],
        )

    def test_duplicate_session_filename_across_roots_is_counted_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active = root / "active"
            archived = root / "archived"
            events = [
                model_event("2026-07-17T01:00:00Z"),
                token_event(
                    "2026-07-17T01:01:00Z", input_tokens=9,
                    output_tokens=1, cached_input_tokens=8, total_tokens=10,
                ),
            ]
            write_events(active / "same-session.jsonl", events)
            write_events(archived / "same-session.jsonl", events)
            importer = CodexLocalDailyImporter(
                session_roots=(active, archived),
                local_timezone=SGT,
                clock=lambda: NOW,
            )

            result = importer.fetch_usage(
                date(2026, 7, 17), date(2026, 7, 17)
            )

        self.assertEqual(sum(row.total_tokens for row in result.rows), 10)

    def test_legacy_total_only_event_preserves_provider_total(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_events(root / "legacy.jsonl", [
                model_event("2026-07-17T01:00:00Z"),
                {
                    "timestamp": "2026-07-17T01:01:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 0,
                                "cached_input_tokens": 0,
                                "output_tokens": 0,
                                "reasoning_output_tokens": 0,
                                "total_tokens": 17_981,
                            }
                        },
                    },
                },
            ])

            result = self.importer(root).fetch_usage(
                date(2026, 7, 17), date(2026, 7, 17)
            )

        self.assertEqual(result.rows[0].total_tokens, 17_981)
        self.assertEqual(result.rows[0].input_tokens, 0)

    def test_tokens_before_first_model_move_to_that_model_only_with_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_events(root / "later-model.jsonl", [
                token_event(
                    "2026-07-17T01:00:00Z", input_tokens=9,
                    output_tokens=1, cached_input_tokens=8, total_tokens=10,
                ),
                model_event("2026-07-17T01:01:00Z", "gpt-5.6-sol"),
            ])
            write_events(root / "no-model.jsonl", [
                token_event(
                    "2026-07-17T02:00:00Z", input_tokens=18,
                    output_tokens=2, cached_input_tokens=16, total_tokens=20,
                ),
            ])

            result = self.importer(root).fetch_usage(
                date(2026, 7, 17), date(2026, 7, 17)
            )

        self.assertEqual(
            [(row.model_id, row.total_tokens) for row in result.rows],
            [("gpt-5.6-sol", 10), ("unknown", 20)],
        )

    def test_tokens_before_first_model_stay_unknown_when_session_switches_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_events(root / "switch.jsonl", [
                token_event(
                    "2026-07-17T01:00:00Z", input_tokens=9,
                    output_tokens=1, cached_input_tokens=8, total_tokens=10,
                ),
                model_event("2026-07-17T01:01:00Z", "gpt-5.6-sol"),
                model_event("2026-07-17T01:02:00Z", "gpt-5.6-terra"),
            ])

            result = self.importer(root).fetch_usage(
                date(2026, 7, 17), date(2026, 7, 17)
            )

        self.assertEqual(
            [(row.model_id, row.total_tokens) for row in result.rows],
            [("unknown", 10)],
        )

    def test_missing_roots_and_relevant_malformed_lines_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = self.importer(root / "missing").fetch_usage(
                date(2026, 7, 17), date(2026, 7, 17)
            )
            path = root / "session.jsonl"
            path.write_text('{"type":"event_msg","payload":{"type":"token_count"}\n')
            malformed = self.importer(root).fetch_usage(
                date(2026, 7, 17), date(2026, 7, 17)
            )

        self.assertEqual(missing, ImportFailure("sessions_unavailable"))
        self.assertEqual(malformed, ImportFailure("sessions_invalid"))


if __name__ == "__main__":
    unittest.main()
