from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from openusage_bar.opencode_daily import OpenCodeLocalDailyImporter


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


def _assistant_message(
    *,
    created_ms: int,
    provider_id: str,
    model_id: str,
    tokens: dict,
    cost: float | None = 0.001,
) -> dict:
    data = {
        "role": "assistant",
        "providerID": provider_id,
        "modelID": model_id,
        "tokens": tokens,
        "cost": cost,
        "time": {"created": created_ms},
    }
    return {"time_created": created_ms, "data": json.dumps(data)}


def _build_db(path: Path, messages: list[dict]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE message (time_created INTEGER NOT NULL, data TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO message (time_created, data) VALUES (?, ?)",
            [(m["time_created"], m["data"]) for m in messages],
        )
        connection.commit()
    finally:
        connection.close()


class OpenCodeLocalDailyImporterTests(unittest.TestCase):
    def test_aggregates_tokens_by_local_day_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "opencode.db"
            day_ms = int(datetime(2026, 8, 10, 3, 0, tzinfo=timezone.utc).timestamp() * 1000)
            _build_db(
                db,
                [
                    _assistant_message(
                        created_ms=day_ms,
                        provider_id="minimax",
                        model_id="MiniMax-M3",
                        tokens={
                            "total": 1000,
                            "input": 600,
                            "output": 100,
                            "reasoning": 0,
                            "cache": {"read": 300, "write": 0},
                        },
                        cost=0.01,
                    ),
                    _assistant_message(
                        created_ms=day_ms + 1,
                        provider_id="minimax",
                        model_id="MiniMax-M3",
                        tokens={
                            "total": 500,
                            "input": 300,
                            "output": 50,
                            "reasoning": 0,
                            "cache": {"read": 150, "write": 0},
                        },
                        cost=0.005,
                    ),
                    _assistant_message(
                        created_ms=day_ms,
                        provider_id="deepseek",
                        model_id="deepseek-chat",
                        tokens={
                            "total": 200,
                            "input": 150,
                            "output": 50,
                            "reasoning": 10,
                            "cache": {"read": 0, "write": 0},
                        },
                        cost=0.002,
                    ),
                ],
            )
            importer = OpenCodeLocalDailyImporter(
                db_path=db,
                clock=lambda: NOW,
                local_timezone=timezone.utc,
            )
            result = importer.fetch_usage(date(2026, 8, 10), date(2026, 8, 10))

            self.assertTrue(result.ok)
            rows = {row.model_id: row for row in result.rows}
            self.assertEqual(set(rows), {"MiniMax-M3", "deepseek-chat"})
            minimax = rows["MiniMax-M3"]
            self.assertEqual(minimax.day, "2026-08-10")
            self.assertEqual(minimax.provider_id, "opencode")
            self.assertEqual(minimax.input_tokens, 900)
            self.assertEqual(minimax.output_tokens, 150)
            self.assertEqual(minimax.cache_read_tokens, 450)
            self.assertEqual(minimax.total_tokens, 1500)
            self.assertEqual(minimax.cost_amount, "0.015")
            self.assertEqual(minimax.cost_currency, "USD")
            self.assertEqual(minimax.quality, "direct")

    def test_fails_closed_when_database_is_missing_or_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.db"
            importer = OpenCodeLocalDailyImporter(
                db_path=missing, clock=lambda: NOW
            )
            result = importer.fetch_usage(date(2026, 8, 1), date(2026, 8, 10))
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "sessions_unavailable")

            bad = Path(directory) / "bad.db"
            bad.write_bytes(b"not a sqlite database")
            importer = OpenCodeLocalDailyImporter(db_path=bad, clock=lambda: NOW)
            result = importer.fetch_usage(date(2026, 8, 1), date(2026, 8, 10))
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "sessions_invalid")

    def test_rejects_invalid_range_and_respects_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "opencode.db"
            _build_db(
                db,
                [
                    _assistant_message(
                        created_ms=int(
                            datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc).timestamp() * 1000
                        ),
                        provider_id="minimax",
                        model_id="MiniMax-M3",
                        tokens={"total": 10, "input": 5, "output": 5, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                    )
                ],
            )
            importer = OpenCodeLocalDailyImporter(
                db_path=db, clock=lambda: NOW, local_timezone=timezone.utc
            )
            self.assertFalse(importer.fetch_usage(date(2026, 8, 10), date(2026, 8, 1)).ok)
            outside = importer.fetch_usage(date(2026, 9, 1), date(2026, 9, 10))
            self.assertTrue(outside.ok)
            self.assertEqual(outside.rows, ())


if __name__ == "__main__":
    unittest.main()
