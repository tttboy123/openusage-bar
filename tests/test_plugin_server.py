from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

from openusage_bar.plugin.api import PluginRouter
from openusage_bar.plugin.config import PluginPrincipalRegistry
from openusage_bar.plugin.server import create_plugin_server
from openusage_bar.plugin.store import PluginStore
from openusage_bar.collector_cli import main
from openusage_bar import platform_services


NOW = datetime(2026, 8, 10, tzinfo=timezone.utc)


class Unused:
    pass


class PluginServerTests(unittest.TestCase):
    def test_hidden_self_test_is_synthetic_and_exact(self) -> None:
        import io

        stdout = io.StringIO()
        stderr = io.StringIO()
        result = main(["__plugin-self-test", "--format", "json"], stdout=stdout, stderr=stderr)
        self.assertEqual(result, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue()), {
            "apiVersion": "plugin-self-test/v1",
            "object": "plugin.server_self_test",
            "ok": True,
            "synthetic": True,
            "checks": {
                "principalIsolation": True,
                "samePayloadReplay": True,
                "differentPayloadConflict": True,
                "restartReplay": True,
            },
        })

    def test_plugin_start_is_independent_and_does_not_open_activity_store(self) -> None:
        class FakeServer:
            def serve_forever(self) -> None:
                return

            def shutdown(self) -> None:
                return

            def server_close(self) -> None:
                return

        with tempfile.TemporaryDirectory() as temporary:
            stop = threading.Event()
            stop.set()

            def forbidden_store() -> object:
                raise AssertionError("Plugin must not open ActivityStore")

            result = main(
                ["plugin", "start", "--state-dir", str(Path(temporary) / "plugin")],
                stderr=__import__("io").StringIO(), stop_event=stop,
                store_factory=forbidden_store,
                plugin_server_factory=lambda **_kwargs: FakeServer(),
            )
            self.assertEqual(result, 0)

    def test_frozen_service_definition_uses_verified_packaged_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "openusage-collector"
            executable.write_bytes(b"packaged")
            with (
                patch.object(platform_services.sys, "frozen", True, create=True),
                patch.object(platform_services.sys, "executable", str(executable)),
            ):
                command = platform_services._plugin_command()
                unit = platform_services.plugin_systemd_unit(command=command)
            self.assertEqual(command, str(executable.resolve()))
            rendered_command = (
                command.replace("\\", "\\\\") if os.name == "nt" else command
            )
            self.assertIn(
                f'ExecStart="{rendered_command}" plugin start',
                unit.splitlines(),
            )
            self.assertNotIn("ExecStart=openusage-bar ", unit)

    def test_listener_authenticates_principal_from_token_and_rejects_assertion_header(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugin"
            registry = PluginPrincipalRegistry.load_or_create(root)
            store = PluginStore(root / "plugin.sqlite3", clock=lambda: NOW)
            router = PluginRouter(
                store=store, facts_client=Unused(), advice_client=Unused(),
                configured_principals=("loom",), clock=lambda: NOW,
            )
            server = create_plugin_server(router, registry=registry, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                connection.request("GET", "/plugin/v1/capabilities", headers={
                    "Authorization": f"Bearer {registry.token_path('loom').read_text(encoding='ascii').strip()}"
                })
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["principal"], "loom")

                connection.request("GET", "/plugin/v1/capabilities", headers={
                    "Authorization": f"Bearer {registry.token_path('loom').read_text(encoding='ascii').strip()}",
                    "X-OpenUsage-Plugin-Id": "codex",
                })
                rejected = connection.getresponse()
                problem = json.loads(rejected.read())
                self.assertEqual(rejected.status, 400)
                self.assertEqual(problem["error"]["code"], "invalid_header")

                connection.request("OPTIONS", "/plugin/v1/capabilities", headers={
                    "Authorization": f"Bearer {registry.token_path('loom').read_text(encoding='ascii')}"
                })
                options = connection.getresponse()
                options_body = json.loads(options.read())
                self.assertEqual(options.status, 405)
                self.assertEqual(options.getheader("Content-Type"), "application/json; charset=utf-8")
                self.assertEqual(options_body["error"]["code"], "method_not_allowed")
                self.assertNotIn("token", repr(options_body).casefold())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)


    def test_listener_rejects_malformed_requests_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugin"
            registry = PluginPrincipalRegistry.load_or_create(root)
            store = PluginStore(root / "plugin.sqlite3", clock=lambda: NOW)
            router = PluginRouter(
                store=store, facts_client=Unused(), advice_client=Unused(),
                configured_principals=("loom",), clock=lambda: NOW,
            )
            server = create_plugin_server(router, registry=registry, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            token = registry.token_path("loom").read_text(encoding="ascii").strip()
            try:
                cases = (
                    (("GET", "/plugin/v1/capabilities", {}), 401),
                    (("GET", "/plugin/v1/capabilities", {"Authorization": "Bearer bad"}), 401),
                    (("GET", "/plugin/v1/capabilities", {"Authorization": f"Bearer {token}", "Host": "evil.example"}), 403),
                    (("GET", "/plugin/v1/capabilities", {"Authorization": f"Bearer {token}", "Content-Length": "not-a-number"}), 400),
                    (("POST", "/plugin/v1/health/query", {"Authorization": f"Bearer {token}", "Content-Type": "text/plain"}), 400),
                    (("PATCH", "/plugin/v1/capabilities", {"Authorization": f"Bearer {token}"}), 405),
                    (("POST", "/plugin/v1/health/query", {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Idempotency-Key": "idem_0123456789abcdef0123456789abcdef"}), 400),
                    (("POST", "/plugin/v1/health/query", {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Content-Length": "99999"}), 413),
                )
                for (method, path, headers), expected in cases:
                    with self.subTest(method=method, path=path, headers=headers):
                        connection = http.client.HTTPConnection(
                            "127.0.0.1", server.server_port, timeout=2
                        )
                        connection.request(method, path, headers=headers)
                        response = connection.getresponse()
                        response.read()
                        connection.close()
                        self.assertEqual(response.status, expected)

                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=2
                )
                connection.request(
                    "POST", "/plugin/v1/health/query",
                    body=b"{bad json",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()
                self.assertEqual(response.status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)




    def test_create_server_rejects_invalid_configuration(self) -> None:
        from openusage_bar.plugin.server import create_plugin_server

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "plugin"
            registry = PluginPrincipalRegistry.load_or_create(root)
            store = PluginStore(root / "plugin.sqlite3", clock=lambda: NOW)
            router = PluginRouter(
                store=store, facts_client=Unused(), advice_client=Unused(),
                configured_principals=("loom",), clock=lambda: NOW,
            )
            with self.assertRaisesRegex(ValueError, "invalid Plugin server configuration"):
                create_plugin_server(object(), registry=registry)
            with self.assertRaisesRegex(ValueError, "invalid Plugin server configuration"):
                create_plugin_server(router, registry=object())
            with self.assertRaisesRegex(ValueError, "invalid Plugin server configuration"):
                create_plugin_server(router, registry=registry, host="0.0.0.0")
            with self.assertRaisesRegex(ValueError, "invalid Plugin server configuration"):
                create_plugin_server(router, registry=registry, port=70000)
            router.close()
if __name__ == "__main__":
    unittest.main()
