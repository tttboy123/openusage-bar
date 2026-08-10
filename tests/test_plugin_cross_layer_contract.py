from __future__ import annotations

import ast
import hashlib
import importlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
API_VERSION = "plugin.openusage/v1"
PLUGIN_ROUTES = (
    "GET /plugin/v1/capabilities",
    "GET /plugin/v1/connections",
    "GET /plugin/v1/decisions/{decisionId}",
    "GET /plugin/v1/schema",
    "POST /plugin/v1/health/query",
    "POST /plugin/v1/outcomes",
    "POST /plugin/v1/quotas/query",
    "POST /plugin/v1/route-advice",
    "POST /plugin/v1/usage/query",
)
PRINCIPALS = ("loom", "codex", "claude_code", "desktop")
SIDE_EFFECT_ROUTES = (
    "POST /plugin/v1/outcomes",
    "POST /plugin/v1/route-advice",
)
CAPABILITY_IDS = (
    "decision.lookup",
    "health.query",
    "outcome.record",
    "quotas.query",
    "route.advice",
    "usage.query",
)
BRIDGE_MODES = (
    "loom-stdio",
    "codex-stdio",
    "claude-code-stdio",
)
LOCAL_API_N_MINUS_ONE_SHA256 = {
    "tests/fixtures/local-api-v1/v0.4.2.schema.json": (
        "94b4e8d3d32270814482a6effd0366dd29535161ec759e2d81c15d85a9a30a6e"
    ),
    "tests/fixtures/local-api-v1/v0.4.2.snapshot.json": (
        "86b36bbc0f25fc1bae57c73ba898d57d15a1e88f46fdc798c5bfcd3180f9f498"
    ),
}


class PluginCrossLayerContractTests(unittest.TestCase):
    def test_committed_manifest_freezes_the_independent_plugin_surface(self) -> None:
        manifest_path = ROOT / "openusage_bar/resources/plugin-api-v1.schema.json"
        self.assertTrue(manifest_path.is_file(), "missing Plugin API v1 manifest")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["apiVersion"], API_VERSION)
        self.assertEqual(tuple(manifest["routes"]), PLUGIN_ROUTES)
        self.assertEqual(tuple(manifest["principals"]), PRINCIPALS)
        self.assertEqual(
            tuple(manifest["idempotency"]["requiredFor"]),
            SIDE_EFFECT_ROUTES,
        )
        self.assertEqual(
            manifest["idempotency"]["keyPattern"],
            r"^idem_[0-9a-f]{32}$",
        )
        self.assertEqual(manifest["listener"], {"host": "127.0.0.1", "port": 17_824})
        self.assertEqual(manifest["idempotency"]["retentionSeconds"], 604_800)
        self.assertEqual(
            manifest["idempotency"]["maxEntriesPerPrincipal"], 10_000
        )
        self.assertEqual(manifest["requestBodyMaxBytes"], 65_536)
        self.assertEqual(manifest["decisionIdPattern"], r"^decision_[0-9a-f]{32}$")
        self.assertEqual(tuple(manifest["capabilityIds"]), CAPABILITY_IDS)

    def test_plugin_namespace_does_not_expand_local_or_gateway_api_v1(self) -> None:
        for relative, expected in LOCAL_API_N_MINUS_ONE_SHA256.items():
            with self.subTest(relative=relative):
                self.assertEqual(
                    hashlib.sha256((ROOT / relative).read_bytes()).hexdigest(),
                    expected,
                )

        local_schema = (
            ROOT / "openusage_bar/resources/local-api-v1.schema.json"
        ).read_text(encoding="utf-8")
        local_router = (ROOT / "openusage_bar/local_api.py").read_text(
            encoding="utf-8"
        )
        gateway_manifest = json.loads(
            (
                ROOT / "openusage_bar/resources/gateway-api-v1.schema.json"
            ).read_text(encoding="utf-8")
        )

        self.assertNotIn("/plugin/", local_schema)
        self.assertNotIn("/plugin/", local_router)
        self.assertTrue(
            all("/plugin/" not in route for route in gateway_manifest["routes"])
        )

    def test_plugin_and_gateway_decision_identifiers_remain_distinct(self) -> None:
        gateway_trace_schema = json.loads(
            (
                ROOT
                / "openusage_bar/resources/gateway-decision-trace-v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        trace_pattern = gateway_trace_schema["$defs"]["traceBase"]["properties"][
            "traceId"
        ]["pattern"]

        self.assertEqual(trace_pattern, r"^trace_[0-9a-f]{32}$")
        self.assertNotEqual(trace_pattern, r"^decision_[0-9a-f]{32}$")

    def test_capability_order_is_identical_across_manifest_routes_and_bridge(self) -> None:
        from openusage_bar.plugin.contracts import (
            CAPABILITY_IDS as SERVER_CAPABILITY_IDS,
            sanitize_response,
        )

        self.assertEqual(CAPABILITY_IDS, tuple(sorted(CAPABILITY_IDS)))
        self.assertEqual(tuple(SERVER_CAPABILITY_IDS), CAPABILITY_IDS)
        manifest = json.loads(
            (
                ROOT / "openusage_bar/resources/plugin-api-v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(tuple(manifest["capabilityIds"]), CAPABILITY_IDS)

        capabilities = sanitize_response(
            "/plugin/v1/capabilities",
            200,
            {
                "apiVersion": API_VERSION,
                "object": "plugin.capabilities",
                "principal": "loom",
                "capabilities": list(CAPABILITY_IDS),
            },
        )
        self.assertEqual(tuple(capabilities["capabilities"]), CAPABILITY_IDS)

        timestamp = "2026-08-10T03:04:05.123456Z"
        connections = sanitize_response(
            "/plugin/v1/connections",
            200,
            {
                "apiVersion": API_VERSION,
                "object": "plugin.connections",
                "observedAt": timestamp,
                "connections": [
                    {
                        "pluginId": plugin_id,
                        "configuration": "configured",
                        "connection": "connected",
                        "capabilityState": "negotiated",
                        "capabilities": list(CAPABILITY_IDS),
                        "lastSeenAt": timestamp,
                        "lastSyncOutcome": "succeeded",
                        "lastSyncAt": timestamp,
                    }
                    for plugin_id in PRINCIPALS[:3]
                ],
            },
        )
        self.assertEqual(
            [tuple(row["capabilities"]) for row in connections["connections"]],
            [CAPABILITY_IDS, CAPABILITY_IDS, CAPABILITY_IDS],
        )

        bridge = importlib.import_module("openusage_bar.plugin_bridge")
        original_serve_stdio = bridge._serve_stdio
        observed: list[tuple[str, ...]] = []

        def capture_self_test(mode: str, **kwargs: object) -> int:
            request = kwargs["request"]

            def capture_request(*arguments: object):
                status, body = request(*arguments)
                observed.append(tuple(body["capabilities"]))
                return status, body

            return original_serve_stdio(
                mode,
                **{**kwargs, "request": capture_request},
            )

        with patch.object(bridge, "_serve_stdio", side_effect=capture_self_test):
            bridge._synthetic_self_test("loom-stdio")
        self.assertEqual(observed, [CAPABILITY_IDS])

    def test_distribution_exposes_three_bounded_bridges_without_private_authority(self) -> None:
        setup_source = (ROOT / "setup.py").read_text(encoding="utf-8")
        self.assertIn('"openusage_bar.plugin"', setup_source)
        self.assertIn(
            '"openusage-plugin-bridge = openusage_bar.plugin_bridge:main"',
            setup_source,
        )

        bridge_path = ROOT / "openusage_bar/plugin_bridge.py"
        self.assertTrue(bridge_path.is_file(), "missing packaged Plugin stdio bridge")
        bridge_source = bridge_path.read_text(encoding="utf-8")
        syntax = ast.parse(bridge_source)
        imported_modules: set[str] = set()
        for node in ast.walk(syntax):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules.add(node.module)

        forbidden_imports = sorted(
            name
            for name in imported_modules
            if name.startswith("openusage_bar.gateway")
            or "credential" in name.casefold()
            or "keychain" in name.casefold()
        )
        self.assertEqual(forbidden_imports, [])
        self.assertNotIn("api.token", bridge_source)
        self.assertNotIn("gateway.token", bridge_source)

        bridge = importlib.import_module("openusage_bar.plugin_bridge")
        self.assertEqual(tuple(bridge.BRIDGE_MODES), BRIDGE_MODES)
        self.assertTrue(callable(bridge.main))

    def test_registry_token_files_are_consumable_by_server_bridge_and_desktop(self) -> None:
        from openusage_bar.plugin.config import PluginPrincipalRegistry

        bridge = importlib.import_module("openusage_bar.plugin_bridge")
        # Keep the fixture under the physical worktree path. macOS aliases
        # /var to /private/var, and both private readers correctly reject an
        # aliased parent path before inspecting the token itself.
        with tempfile.TemporaryDirectory(
            dir=ROOT, prefix=".plugin-token-contract-"
        ) as temporary:
            plugin_dir = Path(temporary) / "plugin"
            registry = PluginPrincipalRegistry.load_or_create(plugin_dir)

            class AllowWindowsAcl:
                def __init__(self) -> None:
                    self.calls = 0

                def verify_file(self, _descriptor: int) -> None:
                    self.calls += 1

            for principal in PRINCIPALS[:3]:
                with self.subTest(principal=principal):
                    token_path = registry.token_path(principal)
                    token_bytes = token_path.read_bytes()
                    self.assertTrue(
                        43 <= len(token_bytes) <= 128
                        and all(0x21 <= byte <= 0x7E for byte in token_bytes),
                        "registry token files must contain only the bearer token",
                    )
                    native_windows_acl = AllowWindowsAcl()
                    token = bridge._read_principal_token(
                        principal,
                        platform_name="nt" if os.name == "nt" else "posix",
                        windows_security=(
                            native_windows_acl if os.name == "nt" else None
                        ),
                        token_path=token_path,
                    )
                    self.assertEqual(registry.authenticate(token), principal)
                    self.assertEqual(
                        native_windows_acl.calls, 1 if os.name == "nt" else 0
                    )

                    injected_windows_acl = AllowWindowsAcl()
                    windows_token = bridge._read_principal_token(
                        principal,
                        platform_name="nt",
                        windows_security=injected_windows_acl,
                        token_path=token_path,
                    )
                    self.assertEqual(injected_windows_acl.calls, 1)
                    self.assertEqual(
                        registry.authenticate(windows_token), principal
                    )

            desktop_token_path = registry.token_path("desktop")
            node_program = """
const { readPrivateToken } = require('./desktop/gateway_proxy.js');
const nativeOptions = process.platform === 'win32'
  ? { platform: 'win32', verifyWindowsAcl: () => true }
  : { platform: process.platform };
const nativeToken = readPrivateToken(process.argv[1], nativeOptions);
const windowsToken = readPrivateToken(process.argv[1], {
  platform: 'win32',
  verifyWindowsAcl: () => true,
});
process.stdout.write(JSON.stringify({ nativeToken, windowsToken }));
"""
            completed = subprocess.run(
                ["node", "-e", node_program, os.fspath(desktop_token_path)],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stderr, "")
            desktop_tokens = json.loads(completed.stdout)
            self.assertEqual(
                set(desktop_tokens), {"nativeToken", "windowsToken"}
            )
            self.assertEqual(
                registry.authenticate(desktop_tokens["nativeToken"]), "desktop"
            )
            self.assertEqual(
                registry.authenticate(desktop_tokens["windowsToken"]), "desktop"
            )

    def test_bridge_preserves_exact_result_too_large_422_errors(self) -> None:
        bridge = importlib.import_module("openusage_bar.plugin_bridge")
        error = {
            "apiVersion": API_VERSION,
            "object": "plugin.error",
            "error": {
                "code": "result_too_large",
                "message": "Query result exceeds the public response limit.",
                "retryable": False,
            },
        }
        encoded = json.dumps(error, separators=(",", ":")).encode("utf-8")
        bridge._validate_response_framing(
            422,
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(encoded))),
            ],
            encoded,
        )

        requests = []
        for digit in ("a", "b"):
            requests.append(
                json.dumps(
                    {
                        "requestId": f"request_{digit * 32}",
                        "method": "POST",
                        "route": "/plugin/v1/usage/query",
                        "body": {
                            "apiVersion": API_VERSION,
                            "from": "2026-08-01",
                            "to": "2026-08-10",
                        },
                        "idempotencyKey": None,
                    },
                    separators=(",", ":"),
                )
            )
        output = io.BytesIO()
        status = bridge._serve_stdio(
            "codex-stdio",
            stdin=io.BytesIO(("\n".join(requests) + "\n").encode("utf-8")),
            stdout=output,
            request=lambda *_arguments: (422, error),
        )
        self.assertEqual(status, 0)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 2)
        for index, response in enumerate(responses):
            self.assertEqual(
                response,
                {
                    "apiVersion": "plugin-bridge.openusage/v1",
                    "object": "plugin.bridge_response",
                    "requestId": f"request_{('a', 'b')[index] * 32}",
                    "status": 422,
                    "body": error,
                },
            )

    def test_desktop_build_runs_every_plugin_contract_and_packaged_smoke(self) -> None:
        workflow = (ROOT / ".github/workflows/desktop-build.yml").read_text(
            encoding="utf-8"
        )

        missing_paths = []
        for path in (
            "openusage_plugin_bridge.py",
            "scripts/smoke_plugin_api.py",
            "tests/test_plugin_*.py",
            "tests/test_smoke_plugin_api.py",
        ):
            if workflow.count(f'- "{path}"') != 2:
                missing_paths.append(path)
        self.assertEqual(
            missing_paths,
            [],
            "push and pull_request must both trigger every Plugin gate",
        )

        missing_modules = []
        for module in (
            "tests.test_plugin_api",
            "tests.test_plugin_store",
            "tests.test_plugin_clients",
            "tests.test_plugin_config",
            "tests.test_plugin_server",
            "tests.test_plugin_bridge",
            "tests.test_plugin_cross_layer_contract",
            "tests.test_smoke_plugin_api",
        ):
            if module not in workflow:
                missing_modules.append(module)
        self.assertEqual(missing_modules, [])

        missing_markers = [
            marker
            for marker in (
                "- name: Build bundled Plugin bridge",
                "id: plugin_bridge_build",
                "openusage_plugin_bridge.py",
                "dist-bridge",
                "${{ matrix.bridge }}",
                "- name: Smoke bundled Plugin API",
                "id: plugin_smoke",
                "python scripts/smoke_plugin_api.py",
                '--collector "./dist-collector/${{ matrix.collector }}"',
                '--bridge "./dist-bridge/${{ matrix.bridge }}"',
                "npm run test:e2e:plugin-health",
            )
            if marker not in workflow
        ]
        self.assertEqual(missing_markers, [])
        self.assertRegex(
            workflow,
            r"(?s)- name: Run Plugin Connections browser E2E\n"
            r"\s+id: plugin_connections_e2e\n"
            r"\s+if: matrix\.platform == 'linux' && matrix\.arch == 'x64'",
        )


if __name__ == "__main__":
    unittest.main()
