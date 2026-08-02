from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from pathlib import Path

from openusage_bar.collector_cli import main
from openusage_bar.routing_api import RoutingController, create_routing_unix_server
from openusage_bar.routing_store import RoutingStore
from openusage_bar.routing_targets import RouteTargetConfiguration
from tests.test_routing_api import NOW, FakeQuery, request_payload, target


class RoutingCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = RoutingStore(root / "routing.sqlite3", clock=lambda: NOW)
        self.addCleanup(self.store.close)
        controller = RoutingController(
            query=FakeQuery(),
            target_loader=lambda: RouteTargetConfiguration(1, 7, (target(),)),
            evidence_store=self.store,
            runtime_reader=lambda _start, _end: None,
            available_connections=lambda: ("connection-1",),
            clock=lambda: NOW,
        )
        self.socket_path = root / "router.sock"
        self.server = create_routing_unix_server(self.socket_path, controller)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.running = True
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        if not self.running:
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.running = False

    def run_cli(self, command: list[str], input_value: str = ""):
        out = io.StringIO()
        err = io.StringIO()
        code = main(
            command + ["--socket", str(self.socket_path)],
            stdin=io.StringIO(input_value),
            stdout=out,
            stderr=err,
        )
        return code, out.getvalue(), err.getvalue()

    def test_decide_simulate_and_history_are_stable_json(self) -> None:
        encoded = json.dumps(request_payload())
        code, output, error = self.run_cli(
            ["route", "decide", "--format", "json"], encoded
        )
        self.assertEqual((code, error), (0, ""))
        decided = json.loads(output)
        self.assertEqual(decided["selected"]["targetId"], "openai.work.gpt-5")

        code, output, error = self.run_cli(
            ["route", "simulate", "--format", "json"], encoded
        )
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(json.loads(output)["simulated"])

        code, output, error = self.run_cli(
            ["route", "history", "--format", "json", "--limit", "10"]
        )
        self.assertEqual((code, error), (0, ""))
        history = json.loads(output)
        self.assertEqual(len(history["decisions"]), 1)
        material = json.dumps(history)
        self.assertNotIn("accountRef", material)
        self.assertNotIn("modelId", material)

    def test_server_validation_error_is_json_and_uses_input_exit_code(self) -> None:
        payload = request_payload()
        payload["prompt"] = "never persisted"
        code, output, error = self.run_cli(
            ["route", "decide", "--format", "json"], json.dumps(payload)
        )
        self.assertEqual((code, error), (2, ""))
        self.assertEqual(json.loads(output)["error"]["code"], "invalid_request")
        self.assertEqual(self.store.decision_count(), 0)

    def test_missing_socket_and_oversized_input_are_sanitized(self) -> None:
        code, output, error = self.run_cli(
            ["route", "decide", "--format", "json"], "x" * (64 * 1024 + 1)
        )
        self.assertEqual((code, output, error), (2, "", "invalid routing input\n"))
        self._stop()
        code, output, error = self.run_cli(
            ["route", "decide", "--format", "json"], json.dumps(request_payload())
        )
        self.assertEqual((code, output, error), (1, "", "routing unavailable\n"))


if __name__ == "__main__":
    unittest.main()
