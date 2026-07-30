from __future__ import annotations

import http.client
import io
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from openusage_bar.activity_store import ActivityStore
from openusage_bar.collector_cli import main as collector_main
from openusage_bar.local_api import create_unix_server
from openusage_bar.query import QueryService, to_wire
from scripts.generate_cross_language_fixture import DAY, NOW, main


ACTIVITY_FACT_FIELDS = (
    "inputTokens",
    "outputTokens",
    "cacheReadTokens",
    "cacheCreationTokens",
    "reasoningTokens",
    "totalTokens",
    "sourceId",
    "quality",
    "tokenCountingConvention",
)


def activity_fact(row: dict[str, object]) -> dict[str, object]:
    return {field: row[field] for field in ACTIVITY_FACT_FIELDS}


def unix_json_request(socket_path: Path, target: str) -> tuple[int, dict[str, object]]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    client.connect(str(socket_path))
    client.sendall(
        f"GET {target} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode("ascii")
    )
    response = http.client.HTTPResponse(client)
    response._method = "GET"
    response.begin()
    body = response.read()
    status = response.status
    client.close()
    return status, json.loads(body)


class CrossLanguageFixtureTests(unittest.TestCase):
    def test_one_fixture_matches_sqlite_query_api_and_cli_json_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "activity.sqlite3"
            expected_path = root / "expected.json"
            with patch.object(
                sys,
                "argv",
                [
                    "generate_cross_language_fixture.py",
                    "--database",
                    str(database),
                    "--expected",
                    str(expected_path),
                ],
            ):
                self.assertEqual(main(), 0)

            expected = json.loads(expected_path.read_text(encoding="utf-8"))
            expected_activity = expected["activity"]
            expected_row = expected_activity["rows"][0]
            expected_fact = activity_fact(expected_row)
            self.assertEqual(
                expected_fact,
                {
                    "inputTokens": 30,
                    "outputTokens": 10,
                    "cacheReadTokens": 2,
                    "cacheCreationTokens": 4,
                    "reasoningTokens": 3,
                    "totalTokens": 49,
                    "sourceId": "openusage.daily",
                    "quality": "direct",
                    "tokenCountingConvention": "components_disjoint",
                },
            )

            with sqlite3.connect(database) as connection:
                connection.row_factory = sqlite3.Row
                usage = connection.execute(
                    """
                    SELECT input_tokens,output_tokens,cache_read_tokens,
                           cache_creation_tokens,reasoning_tokens,total_tokens,
                           source_id,quality,payload_hash
                    FROM daily_model_usage
                    WHERE day=? AND provider_id=? AND model_id=?
                    """,
                    (DAY.isoformat(), "codex", "gpt-5.6-sol"),
                ).fetchone()
                convention = connection.execute(
                    """
                    SELECT token_counting_convention,daily_payload_hash
                    FROM daily_token_conventions
                    WHERE day=? AND provider_id=? AND model_id=?
                    """,
                    (DAY.isoformat(), "codex", "gpt-5.6-sol"),
                ).fetchone()
            self.assertIsNotNone(usage)
            self.assertIsNotNone(convention)
            self.assertEqual(convention["daily_payload_hash"], usage["payload_hash"])
            self.assertEqual(
                expected_fact,
                {
                    "inputTokens": usage["input_tokens"],
                    "outputTokens": usage["output_tokens"],
                    "cacheReadTokens": usage["cache_read_tokens"],
                    "cacheCreationTokens": usage["cache_creation_tokens"],
                    "reasoningTokens": usage["reasoning_tokens"],
                    "totalTokens": usage["total_tokens"],
                    "sourceId": usage["source_id"],
                    "quality": usage["quality"],
                    "tokenCountingConvention": convention[
                        "token_counting_convention"
                    ],
                },
            )

            store = ActivityStore(database)
            try:
                query = QueryService(store, clock=lambda: NOW)
                query_wire = to_wire(query.activity(DAY, DAY))
                self.assertEqual(query_wire, expected_activity)

                socket_path = root / "api" / "openusage.sock"
                server = create_unix_server(socket_path, query, clock=lambda: NOW)
                server_thread = threading.Thread(
                    target=server.serve_forever, daemon=True
                )
                server_thread.start()
                try:
                    status, api_wire = unix_json_request(
                        socket_path,
                        "/v1/activity/daily?from=2026-07-18&to=2026-07-18",
                    )
                finally:
                    server.shutdown()
                    server.server_close()
                    server_thread.join(2)
                self.assertEqual(status, 200)
                self.assertEqual(api_wire, expected_activity)

                json_stdout, json_stderr = io.StringIO(), io.StringIO()
                self.assertEqual(
                    collector_main(
                        [
                            "usage",
                            "--from",
                            DAY.isoformat(),
                            "--to",
                            DAY.isoformat(),
                            "--format",
                            "json",
                            "--offline",
                        ],
                        stdout=json_stdout,
                        stderr=json_stderr,
                        store=store,
                        query=query,
                        clock=lambda: NOW,
                    ),
                    0,
                )
                self.assertEqual(json_stderr.getvalue(), "")
                self.assertEqual(json.loads(json_stdout.getvalue()), expected_activity)

                jsonl_stdout, jsonl_stderr = io.StringIO(), io.StringIO()
                self.assertEqual(
                    collector_main(
                        [
                            "usage",
                            "--from",
                            DAY.isoformat(),
                            "--to",
                            DAY.isoformat(),
                            "--format",
                            "jsonl",
                            "--offline",
                        ],
                        stdout=jsonl_stdout,
                        stderr=jsonl_stderr,
                        store=store,
                        query=query,
                        clock=lambda: NOW,
                    ),
                    0,
                )
                self.assertEqual(jsonl_stderr.getvalue(), "")
                jsonl = [
                    json.loads(line) for line in jsonl_stdout.getvalue().splitlines()
                ]
                usage_rows = [row for row in jsonl if row.get("type") == "usage"]
                self.assertEqual(len(usage_rows), 1)
                self.assertEqual(activity_fact(usage_rows[0]), expected_fact)
                self.assertEqual(jsonl[-1]["type"], "checkpoint")
                self.assertEqual(
                    jsonl[-1]["dataRevision"], expected_activity["dataRevision"]
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
