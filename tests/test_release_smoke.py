from __future__ import annotations

import json
import os
import plistlib
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class UnixAPI:
    def __init__(self, path: Path, *, bad_route: str | None = None) -> None:
        self.path = path
        self.bad_route = bad_route
        self.routes: list[str] = []
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.path))
            server.listen(3)
            self.ready.set()
            for _ in range(3):
                client, _ = server.accept()
                with client:
                    request = client.recv(4096).decode("ascii", "replace")
                    route = request.split(" ", 2)[1]
                    self.routes.append(route)
                    status = "500 Error" if route == self.bad_route else "200 OK"
                    payload: dict[str, object] = {"schemaVersion": "1.0"}
                    if route == "/v1/health":
                        payload["health"] = {"ok": True, "status": "ok"}
                    elif route == "/v1/schema":
                        payload["routes"] = []
                    elif route == "/v1/summary":
                        payload["todayTokens"] = 0
                    body = json.dumps(payload).encode()
                    client.sendall(
                        f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n"
                        "Connection: close\r\n\r\n".encode()
                        + body
                    )
        finally:
            server.close()

    def __enter__(self) -> "UnixAPI":
        self.thread.start()
        self.ready.wait(2)
        return self

    def __exit__(self, *_: object) -> None:
        self.thread.join(2)


def make_signed_app(path: Path, version: str, build: str) -> None:
    executable = path / "Contents/MacOS/OpenUsage Bar"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    with (path / "Contents/Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": "com.lune.openusagebar",
                "CFBundleShortVersionString": version,
                "CFBundleVersion": build,
            },
            handle,
        )
    subprocess.run(
        ["/usr/bin/codesign", "--force", "--sign", "-", str(path)], check=True,
        capture_output=True,
    )


class ReleaseSmokeTests(unittest.TestCase):
    def test_local_api_probe_requires_health_schema_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            socket_path = Path(temp) / "api.sock"
            with UnixAPI(socket_path) as api:
                result = subprocess.run(
                    [
                        str(ROOT / "scripts/verify_local_api.py"),
                        "--timeout", "1", str(socket_path),
                    ],
                    capture_output=True,
                    text=True,
                )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(api.routes, ["/v1/health", "/v1/schema", "/v1/summary"])

    def test_local_api_probe_rejects_an_unhealthy_contract_route(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            socket_path = Path(temp) / "api.sock"
            with UnixAPI(socket_path, bad_route="/v1/schema"):
                result = subprocess.run(
                    [
                        str(ROOT / "scripts/verify_local_api.py"),
                        "--timeout", "0.2", str(socket_path),
                    ],
                    capture_output=True,
                    text=True,
                )
        self.assertNotEqual(result.returncode, 0)

    def test_backup_helpers_keep_only_two_complete_valid_bundles(self) -> None:
        transaction = ROOT / "scripts/install_app_transaction.sh"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "OpenUsage Bar.app"
            backups = root / "state/backups/app"
            for version, build, stamp in (
                ("0.3.0", "3", "20260718T000001Z"),
                ("0.4.0", "4", "20260718T000002Z"),
                ("0.4.1", "5", "20260718T000003Z"),
            ):
                if app.exists():
                    subprocess.run(["rm", "-rf", str(app)], check=True)
                make_signed_app(app, version, build)
                script = f'''source "{transaction}"
create_complete_app_backup "{app}" "{backups}" "{stamp}"
prune_complete_app_backups "{backups}" 2
'''
                subprocess.run(["/bin/zsh", "-c", script], check=True)
            complete = sorted(backups.glob("*/metadata.plist"))
            self.assertEqual(len(complete), 2)
            versions = []
            for path in complete:
                with path.open("rb") as handle:
                    versions.append(plistlib.load(handle)["version"])
            self.assertEqual(versions, ["0.4.0", "0.4.1"])
            for marker in complete:
                result = subprocess.run(
                    [
                        "/bin/zsh", "-c",
                        f'''source "{transaction}"
validate_complete_app_backup "{marker.parent}"
''',
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            tampered = complete[-1].parent / "OpenUsage Bar.app/Contents/MacOS/OpenUsage Bar"
            tampered.write_text("tampered\n", encoding="utf-8")
            invalid = subprocess.run(
                [
                    "/bin/zsh", "-c",
                    f'''source "{transaction}"
validate_complete_app_backup "{complete[-1].parent}"
''',
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(invalid.returncode, 0)

    def test_purge_rejects_a_state_directory_outside_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            home.mkdir()
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "OPENUSAGE_STATE_DIR": str(Path(temp).parent / "not-openusage"),
                    "OPENUSAGE_INSTALL_DIR": str(home / "Applications"),
                }
            )
            result = subprocess.run(
                [str(ROOT / "scripts/uninstall_app.sh"), "--purge-data"],
                env=environment,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("outside HOME", result.stderr)

    def test_installer_exposes_isolated_smoke_controls_and_failure_points(self) -> None:
        install = (ROOT / "scripts/install_app.sh").read_text(encoding="utf-8")
        transaction = (ROOT / "scripts/install_app_transaction.sh").read_text(encoding="utf-8")
        smoke = (ROOT / "scripts/release_smoke.sh").read_text(encoding="utf-8")
        rollback = (ROOT / "scripts/rollback_app.sh").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

        for name in (
            "OPENUSAGE_INSTALL_DIR", "OPENUSAGE_SOURCE_APP",
            "OPENUSAGE_LABEL_SUFFIX", "OPENUSAGE_LAUNCHCTL",
            "OPENUSAGE_HEALTH_PROBE", "OPENUSAGE_TEST_FAIL_STAGE",
        ):
            self.assertIn(name, install)
        self.assertIn("helper-copy", install)
        self.assertIn("launch-agent", install)
        self.assertIn("verify_local_api.py", transaction)
        self.assertIn("backups/app", install)
        self.assertIn("launchctl-spy", smoke)
        self.assertIn("OPENUSAGE_REAL_LAUNCH_AGENTS", smoke)
        self.assertIn("PRAGMA integrity_check", smoke)
        self.assertIn("--purge-data", smoke)
        self.assertNotIn("security ", rollback)
        self.assertIn("scripts/release_smoke.sh", workflow)


if __name__ == "__main__":
    unittest.main()
