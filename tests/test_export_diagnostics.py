from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import socket
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/export_diagnostics.py"


def load_module():
    spec = importlib.util.spec_from_file_location("export_diagnostics", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot() -> dict[str, object]:
    return {
        "schemaVersion": "1.0",
        "dataRevision": 19,
        "generatedAt": "2026-07-18T00:00:00Z",
        "localDay": "2026-07-18",
        "catalogRevision": "openusage-0.23.0",
        "summary": {"todayTokens": 42, "modelCount": 3, "coveredDayCount": 1},
        "providers": [
            {
                "providerId": "private-instance",
                "familyId": "minimax",
                "displayName": "Alice private account",
                "credentialSource": "keychain:private-label",
                "sourceKind": "provider_api",
            }
        ],
        "quotaWindows": [
            {
                "accountRef": "personal-account",
                "providerId": "private-instance",
                "state": "known",
                "quality": "live",
                "stale": False,
                "remaining": "secret-value-is-never-copied",
            }
        ],
        "sources": [
            {
                "providerId": "private-instance",
                "sourceId": "private-source",
                "state": "live",
                "errorCode": None,
            },
            {
                "providerId": "another-private-instance",
                "sourceId": "another-source",
                "state": "error",
                "errorCode": "AUTH_FAILED",
            },
        ],
    }


def capabilities() -> dict[str, object]:
    return {
        "schemaVersion": "1.0",
        "dataRevision": 19,
        "generatedAt": "2026-07-18T00:00:00Z",
        "upstream": {"name": "openusage", "version": "0.23.0", "revision": "abc"},
        "providers": [
            {
                "familyId": "minimax",
                "providerId": "minimax",
                "metricFamilies": ["quota", "tokens"],
                "regions": ["china", "international"],
                "supportsAccounts": True,
                "capabilities": {
                    "quotaWindows": {"state": "supported", "values": ["five_hour"]},
                    "tokenHistory": "supported",
                    "modelBreakdown": "supported",
                    "resetTimestamps": "supported",
                    "billing": "unknown",
                    "credits": "unknown",
                    "balance": "unknown",
                    "cost": "unknown",
                    "rateLimits": "unknown",
                    "serviceStatus": "unknown",
                },
                "sources": [
                    {
                        "kind": "provider_api",
                        "stability": "stable",
                        "provenance": "provider_official",
                    }
                ],
            }
        ],
    }


class TwoRouteServer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.path))
            server.listen(2)
            self.ready.set()
            for _ in range(2):
                client, _ = server.accept()
                with client:
                    request = client.recv(4096).decode("ascii", "replace")
                    route = request.split(" ", 2)[1]
                    payload = snapshot() if route == "/v1/snapshot" else capabilities()
                    body = json.dumps(payload).encode()
                    client.sendall(
                        f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n"
                        "Connection: close\r\n\r\n".encode()
                        + body
                    )
        finally:
            server.close()

    def __enter__(self) -> "TwoRouteServer":
        self.thread.start()
        self.ready.wait(2)
        return self

    def __exit__(self, *_: object) -> None:
        self.thread.join(2)


class ExportDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def test_builds_allowlisted_aggregate_diagnostics_only(self) -> None:
        payload = self.module.build_diagnostics(
            snapshot(),
            capabilities(),
            product={"version": "0.4.0", "build": "4"},
            runtime={"macOS": "26.0", "architecture": "arm64"},
            clock=lambda: datetime(2026, 7, 18, tzinfo=timezone.utc),
        )
        self.assertEqual(payload["schemaVersion"], "openusage-diagnostics-1")
        self.assertEqual(payload["localAPI"]["dataRevision"], 19)
        self.assertEqual(payload["aggregates"]["providerInstanceCount"], 1)
        self.assertEqual(payload["aggregates"]["sourceStates"], {"error": 1, "live": 1})
        self.assertEqual(payload["aggregates"]["sourceErrorCodes"], {"AUTH_FAILED": 1})
        self.assertEqual(payload["capabilityDeclarations"][0]["familyId"], "minimax")
        encoded = json.dumps(payload, sort_keys=True)
        for forbidden in (
            "Alice private account", "personal-account", "private-instance",
            "private-source", "keychain:private-label", "secret-value",
            "accountRef", "displayName", "credentialSource", "sourceId",
            "payloadJson", "cookie", "prompt", "response",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_sanitizes_untrusted_error_codes_and_rejects_revision_drift(self) -> None:
        unsafe = snapshot()
        unsafe["sources"][1]["errorCode"] = "Bearer secret-value-that-must-not-export"
        payload = self.module.build_diagnostics(
            unsafe,
            capabilities(),
            product={"version": "0.4.0", "build": "4"},
            runtime={"macOS": "26.0", "architecture": "arm64"},
        )
        self.assertEqual(payload["aggregates"]["sourceErrorCodes"], {"UNCLASSIFIED": 1})
        drift = capabilities()
        drift["dataRevision"] = 20
        with self.assertRaises(ValueError):
            self.module.build_diagnostics(
                snapshot(), drift,
                product={"version": "0.4.0", "build": "4"},
                runtime={"macOS": "26.0", "architecture": "arm64"},
            )
        forbidden = snapshot()
        forbidden["token"] = "secret-value"
        with self.assertRaises(ValueError):
            self.module.build_diagnostics(
                forbidden,
                capabilities(),
                product={"version": "0.4.0", "build": "4"},
                runtime={"macOS": "26.0", "architecture": "arm64"},
            )

    def test_cli_writes_atomic_private_json_from_two_read_only_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            socket_path = root / "openusage.sock"
            app = root / "OpenUsage Bar.app"
            info = app / "Contents/Info.plist"
            info.parent.mkdir(parents=True)
            with info.open("wb") as handle:
                plistlib.dump(
                    {"CFBundleShortVersionString": "0.4.0", "CFBundleVersion": "4"},
                    handle,
                )
            output = root / "diagnostics.json"
            with TwoRouteServer(socket_path):
                result = subprocess.run(
                    [
                        str(SCRIPT), "--socket", str(socket_path),
                        "--app", str(app), "--output", str(output),
                    ],
                    capture_output=True,
                    text=True,
                )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            exported = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(exported["product"], {"build": "4", "version": "0.4.0"})
            self.assertNotIn(str(Path.home()), output.read_text(encoding="utf-8"))
            privacy = subprocess.run(
                [str(ROOT / ".build-venv/bin/python"), str(ROOT / "scripts/privacy_scan.py"), str(output)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(privacy.returncode, 0, privacy.stderr)


if __name__ == "__main__":
    unittest.main()
