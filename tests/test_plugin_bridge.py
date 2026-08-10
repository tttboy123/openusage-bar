from __future__ import annotations

import io
import json
import inspect
from pathlib import Path
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


class PluginBridgeTests(unittest.TestCase):
    def bridge(self):
        from openusage_bar import plugin_bridge

        return plugin_bridge

    def test_all_modes_have_exact_secret_free_synthetic_self_test(self) -> None:
        bridge = self.bridge()
        self.assertEqual(
            bridge.BRIDGE_MODES,
            ("loom-stdio", "codex-stdio", "claude-code-stdio"),
        )
        for mode in bridge.BRIDGE_MODES:
            with self.subTest(mode=mode):
                output = io.StringIO()
                error = io.StringIO()
                with redirect_stdout(output), redirect_stderr(error):
                    status = bridge.main([mode, "--self-test", "--format", "json"])
                self.assertEqual(status, 0)
                self.assertEqual(
                    json.loads(output.getvalue()),
                    {
                        "apiVersion": "plugin-self-test/v1",
                        "object": "plugin.bridge_self_test",
                        "ok": True,
                        "synthetic": True,
                        "mode": mode,
                    },
                )
                self.assertEqual(error.getvalue(), "")

    def test_self_test_fails_if_the_synthetic_stdio_roundtrip_breaks(self) -> None:
        bridge = self.bridge()
        output = io.StringIO()
        error = io.StringIO()
        with (
            patch.object(bridge, "_serve_stdio", return_value=1),
            redirect_stdout(output),
            redirect_stderr(error),
        ):
            status = bridge.main(
                ["loom-stdio", "--self-test", "--format", "json"]
            )
        self.assertEqual(status, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(error.getvalue(), "plugin bridge failed\n")

    def test_dry_run_emits_an_unapplied_command_name_only_config_snippet(self) -> None:
        bridge = self.bridge()
        output = io.StringIO()
        error = io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            status = bridge.main(
                ["codex-stdio", "--dry-run", "--format", "json"]
            )
        self.assertEqual(status, 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "apiVersion": "plugin-bridge-config.openusage/v1",
                "object": "plugin.bridge_config_snippet",
                "mode": "codex-stdio",
                "command": "openusage-plugin-bridge",
                "args": ["codex-stdio"],
                "installed": False,
                "applied": False,
            },
        )
        self.assertEqual(error.getvalue(), "")
        self.assertNotIn("/Users/", output.getvalue())
        self.assertNotIn("C:\\", output.getvalue())
        self.assertNotIn("token", output.getvalue().casefold())

    def test_invalid_cli_is_collapsed_without_echoing_argv_or_environment(self) -> None:
        bridge = self.bridge()
        output = io.StringIO()
        error = io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            status = bridge.main(["/Users/private/CANARY", "--token", "secret"])
        self.assertEqual(status, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(error.getvalue(), "plugin bridge invocation failed\n")
        self.assertNotIn("CANARY", error.getvalue())
        self.assertNotIn("secret", error.getvalue())

    def test_jsonl_bridge_accepts_only_fixed_routes_and_outputs_exact_envelope(self) -> None:
        bridge = self.bridge()
        request_id = "request_" + "a" * 32
        source = io.BytesIO(
            (json.dumps({
                "requestId": request_id,
                "method": "GET",
                "route": "/plugin/v1/capabilities",
                "body": None,
                "idempotencyKey": None,
            }) + "\n").encode()
        )
        output = io.BytesIO()
        calls = []

        def request(principal, method, route, body, idempotency_key):
            calls.append((principal, method, route, body, idempotency_key))
            return 200, {
                "apiVersion": "plugin.openusage/v1",
                "object": "plugin.capabilities",
                "principal": "loom",
                "capabilities": [
                    "decision.lookup", "health.query", "outcome.record",
                    "quotas.query", "route.advice", "usage.query",
                ],
            }

        status = bridge._serve_stdio(
            "loom-stdio",
            stdin=source,
            stdout=output,
            request=request,
        )
        self.assertEqual(status, 0)
        self.assertEqual(
            calls,
            [("loom", "GET", "/plugin/v1/capabilities", None, None)],
        )
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "apiVersion": "plugin-bridge.openusage/v1",
                "object": "plugin.bridge_response",
                "requestId": request_id,
                "status": 200,
                "body": {
                    "apiVersion": "plugin.openusage/v1",
                    "object": "plugin.capabilities",
                    "principal": "loom",
                    "capabilities": [
                        "decision.lookup", "health.query", "outcome.record",
                        "quotas.query", "route.advice", "usage.query",
                    ],
                },
            },
        )

    def test_jsonl_bridge_rejects_query_private_fields_and_oversized_input_safely(self) -> None:
        bridge = self.bridge()
        invalid = (
            {
                "requestId": "request_" + "b" * 32,
                "method": "GET",
                "route": "/plugin/v1/schema?debug=1",
                "body": None,
                "idempotencyKey": None,
            },
            {
                "requestId": "request_" + "b" * 32,
                "method": "GET",
                "route": "/plugin/v1/schema",
                "body": None,
                "idempotencyKey": None,
                "token": "CANARY",
            },
        )
        for value in invalid:
            with self.subTest(value=value):
                output = io.BytesIO()
                status = bridge._serve_stdio(
                    "codex-stdio",
                    stdin=io.BytesIO((json.dumps(value) + "\n").encode()),
                    stdout=output,
                    request=lambda *_args: self.fail("transport must not run"),
                )
                self.assertEqual(status, 1)
                self.assertEqual(output.getvalue(), b"")

        oversized = io.BytesIO(b"{" + b"x" * (64 * 1024) + b"}\n")
        self.assertEqual(
            bridge._serve_stdio(
                "claude-code-stdio",
                stdin=oversized,
                stdout=io.BytesIO(),
                request=lambda *_args: self.fail("transport must not run"),
            ),
            1,
        )

    def test_response_framing_rejects_redirects_encoding_and_length_drift(self) -> None:
        bridge = self.bridge()
        body = b'{}'
        bridge._validate_response_framing(
            200,
            [("Content-Type", "application/json"), ("Content-Length", "2")],
            body,
        )
        query_body = b"x" * (128 * 1024)
        bridge._validate_response_framing(
            200,
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(query_body))),
            ],
            query_body,
        )
        invalid = (
            (302, [("Content-Type", "application/json")]),
            (200, [("Content-Type", "text/plain")]),
            (200, [("Content-Type", "application/json"), ("Content-Length", "3")]),
            (200, [("Content-Type", "application/json"), ("Transfer-Encoding", "chunked")]),
            (200, [("Content-Type", "application/json"), ("Content-Encoding", "gzip")]),
            (200, [("Content-Type", "application/json"), ("Content-Type", "application/json")]),
        )
        for status, headers in invalid:
            with self.subTest(status=status, headers=headers):
                with self.assertRaises(Exception):
                    bridge._validate_response_framing(status, headers, body)

    def test_bridge_drops_additive_upstream_response_instead_of_echoing_it(self) -> None:
        bridge = self.bridge()
        request_id = "request_" + "c" * 32
        submitted = io.BytesIO((json.dumps({
            "requestId": request_id,
            "method": "GET",
            "route": "/plugin/v1/capabilities",
            "body": None,
            "idempotencyKey": None,
        }) + "\n").encode())
        output = io.BytesIO()

        status = bridge._serve_stdio(
            "loom-stdio",
            stdin=submitted,
            stdout=output,
            request=lambda *_args: (200, {
                "apiVersion": "plugin.openusage/v1",
                "object": "plugin.capabilities",
                "principal": "loom",
                "capabilities": ["usage.query"],
                "privateToken": "CANARY_PRIVATE",
            }),
        )
        self.assertEqual(status, 1)
        self.assertEqual(output.getvalue(), b"")

    def test_windows_token_reader_uses_injected_acl_and_detects_file_id_drift(self) -> None:
        bridge = self.bridge()
        with tempfile.TemporaryDirectory() as directory:
            plugin_dir = Path(directory) / "plugin"
            plugin_dir.mkdir(mode=0o700)
            token_path = plugin_dir / "loom.token"
            token_path.write_text("w" * 43, encoding="ascii")
            token_path.chmod(0o600)

            class Security:
                calls = 0

                def verify_file(self, descriptor):
                    self.calls += 1
                    self.assert_descriptor = os.fstat(descriptor).st_ino

            security = Security()
            with patch.object(
                bridge.os.path,
                "realpath",
                side_effect=lambda value: os.path.abspath(value),
            ):
                self.assertEqual(
                    bridge._read_principal_token(
                        "loom",
                        platform_name="nt",
                        windows_security=security,
                        token_path=token_path,
                    ),
                    "w" * 43,
                )
            self.assertEqual(security.calls, 1)

            real_fstat = bridge.os.fstat
            calls = 0

            def drifting_fstat(descriptor):
                nonlocal calls
                calls += 1
                value = real_fstat(descriptor)
                if calls == 2:
                    fields = list(value)
                    fields[1] += 1
                    return os.stat_result(fields)
                return value

            with (
                patch.object(bridge.os, "fstat", side_effect=drifting_fstat),
                patch.object(
                    bridge.os.path,
                    "realpath",
                    side_effect=lambda value: os.path.abspath(value),
                ),
            ):
                with self.assertRaises(Exception):
                    bridge._read_principal_token(
                        "loom",
                        platform_name="posix",
                        token_path=token_path,
                    )

    def test_desktop_packages_and_audits_the_exact_bridge_binary(self) -> None:
        root = Path(__file__).resolve().parents[1]
        package = json.loads((root / "desktop/package.json").read_text())
        expected = {
            "mac": "openusage-plugin-bridge",
            "linux": "openusage-plugin-bridge",
            "win": "openusage-plugin-bridge.exe",
        }
        for platform, binary in expected.items():
            resources = package["build"][platform]["extraResources"]
            self.assertIn(
                {
                    "from": f"../dist-bridge/{binary}",
                    "to": f"bridge/{binary}",
                },
                resources,
            )

        from scripts import release_artifact_audit

        self.assertIn(
            "built_bridge",
            inspect.signature(
                release_artifact_audit.inspect_desktop_package
            ).parameters,
        )
        audit_source = (root / "scripts/release_artifact_audit.py").read_text()
        self.assertIn('"--built-bridge"', audit_source)


if __name__ == "__main__":
    unittest.main()
