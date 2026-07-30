from __future__ import annotations

import hashlib
import json
import socket
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from examples.local_api_v1_client import (
    LocalAPIClientError,
    read_snapshot,
)
from openusage_bar.local_api import create_unix_server
from tests.test_local_api import seeded_query, start
from tests.test_local_api_schema import validate_snapshot


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "local-api-v1"
N_MINUS_ONE_SCHEMA_SHA256 = (
    "94b4e8d3d32270814482a6effd0366dd29535161ec759e2d81c15d85a9a30a6e"
)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class OneShotUnixServer:
    def __init__(self, body: bytes) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "api.sock"
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(self.path))
        self._listener.listen(1)
        self._body = body
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        connection, _ = self._listener.accept()
        with connection:
            connection.recv(4096)
            header = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                + f"Content-Length: {len(self._body)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
            )
            try:
                connection.sendall(header + self._body)
            except BrokenPipeError:
                pass

    def close(self) -> None:
        self._listener.close()
        self._thread.join(timeout=1)
        self._directory.cleanup()

    def __enter__(self) -> OneShotUnixServer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class LocalAPIV1CompatibilityTests(unittest.TestCase):
    def test_frozen_n_minus_one_schema_is_exact_release_evidence(self):
        schema = fixture("v0.4.2.schema.json")

        self.assertEqual(hashlib.sha256(schema).hexdigest(), N_MINUS_ONE_SCHEMA_SHA256)
        self.assertEqual(
            json.loads(schema)["$id"],
            "https://openusage.bar/schemas/local-api-v1.schema.json",
        )

    def test_current_validator_accepts_the_n_minus_one_snapshot(self):
        payload = json.loads(fixture("v0.4.2.snapshot.json"))

        validate_snapshot(payload)
        self.assertEqual(payload["schemaVersion"], "1.0")
        self.assertEqual(payload["dataRevision"], 9)

    def test_current_validator_accepts_additive_snapshot_fields(self):
        payload = json.loads(fixture("current-additive.snapshot.json"))

        validate_snapshot(payload)
        self.assertIn("futureEnvelope", payload)
        self.assertIsInstance(payload["balances"], list)

    def test_minimal_client_reads_old_and_additive_current_snapshots(self):
        for name in ("v0.4.2.snapshot.json", "current-additive.snapshot.json"):
            with self.subTest(name=name):
                with OneShotUnixServer(fixture(name)) as server:
                    result = read_snapshot(server.path)
                self.assertEqual(result["schemaVersion"], "1.0")
                self.assertIsInstance(result["dataRevision"], int)
                self.assertNotIn("providers", result)
                self.assertNotIn("sources", result)

    def test_minimal_n_minus_one_projection_reads_the_current_server(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "api.sock"
            store, query = seeded_query()
            server = create_unix_server(
                path,
                query,
                clock=lambda: datetime(2026, 7, 14, 10, tzinfo=timezone.utc),
            )
            thread = start(server)
            try:
                result = read_snapshot(path)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=1)
                store.close()

        self.assertEqual(result["schemaVersion"], "1.0")
        self.assertGreater(result["dataRevision"], 0)
        self.assertEqual(result["localDay"], "2026-07-14")

    def test_minimal_client_enforces_default_one_mib_body_limit(self):
        oversized = b"{" + b"x" * 1_048_576 + b"}"
        with OneShotUnixServer(oversized) as server:
            with self.assertRaisesRegex(LocalAPIClientError, "^response_too_large$"):
                read_snapshot(server.path)

    def test_compatibility_policy_freezes_the_public_surface(self):
        policy = (ROOT / "docs" / "api" / "compatibility-v1.md").read_text(
            encoding="utf-8"
        )
        api_guide = (ROOT / "docs" / "api" / "local-api-v1.md").read_text(
            encoding="utf-8"
        )
        pull_request_template = (
            ROOT / ".github" / "pull_request_template.md"
        ).read_text(encoding="utf-8")

        for term in (
            "/v1/snapshot",
            "/v1/schema.json",
            "dataRevision",
            "additive",
            "deprecated",
            "breaking",
            "N-1",
            "1 MiB",
            "ignore unknown fields",
        ):
            self.assertIn(term, policy)
        self.assertIn("compatibility-v1.md", api_guide)
        self.assertIn("Local API compatibility impact", pull_request_template)
        self.assertIn("N-1", pull_request_template)


if __name__ == "__main__":
    unittest.main()
