from __future__ import annotations

import io
import json
import re
import tempfile
import threading
import unittest
import sys
import subprocess
import socket
import time
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from openusage_bar.activity_store import (
    ActivityStore,
    DailyCostRow,
    DailyUsageRow,
    ProviderInstance,
    QuotaObservation,
)
from openusage_bar.collector_cli import (
    CLIError,
    DEFAULT_FRESH_TIMEOUT_SECONDS,
    INTERNAL_REFRESH_COMMAND,
    main,
)
from openusage_bar.collector_cli import _default_refresh_command
from openusage_bar.daily_history import DAILY_TIMEOUT_SECONDS
from openusage_bar.lifecycle_state import LifecycleStatePaths
from openusage_bar.openusage_adapter import AUTO_TIMEOUT_SECONDS, DIRECT_TIMEOUT_SECONDS
from openusage_bar.query import QueryService, to_wire


NOW = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)


@unittest.skipIf(sys.platform == "win32", "POSIX process-group helper test")
class FrozenRefreshCommandTests(unittest.TestCase):
    def test_cursor_direct_export_has_measured_runtime_margin(self):
        # A sanitized standalone probe completed in 38.41 seconds, while the
        # installed cold background refresh reached the 60-second boundary,
        # while the immediately repeated foreground refresh succeeded. Keep
        # another 25% of runtime margin so cold source contention does not turn
        # a usable Cursor quota into a stale Last-good observation.
        self.assertGreaterEqual(DIRECT_TIMEOUT_SECONDS, 75)

    def test_interactive_attempt_covers_slowest_export_fallback_and_one_daily_import(self):
        required_seconds = (
            AUTO_TIMEOUT_SECONDS
            + DIRECT_TIMEOUT_SECONDS
            + DAILY_TIMEOUT_SECONDS
            + 5
        )
        self.assertGreaterEqual(DEFAULT_FRESH_TIMEOUT_SECONDS, required_seconds)

    def test_swift_and_python_share_the_interactive_refresh_timeout(self):
        source = (
            Path(__file__).parents[1]
            / "swift_app/Sources/OpenUsageBar/Refresh.swift"
        ).read_text(encoding="utf-8")
        match = re.search(
            r"interactiveTimeout:\s*TimeInterval\s*=\s*([0-9]+(?:\.[0-9]+)?)",
            source,
        )
        self.assertIsNotNone(match)
        self.assertEqual(float(match.group(1)), DEFAULT_FRESH_TIMEOUT_SECONDS)

    def test_frozen_helper_reexecutes_itself_without_missing_source_script(self):
        executable = "/Applications/OpenUsage Bar.app/Contents/MacOS/OpenUsage Provider Settings"
        interpreter = "/Applications/OpenUsage Bar.app/Contents/MacOS/python"
        resource_script = (
            "/Applications/OpenUsage Bar.app/Contents/Resources/"
            "openusage_settings.py"
        )
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", interpreter),
            patch.object(sys, "argv", [resource_script, "status", "--fresh"]),
            patch.dict(
                "os.environ",
                {
                    "EXECUTABLEPATH": executable,
                    "RESOURCEPATH": (
                        "/Applications/OpenUsage Bar.app/Contents/Resources"
                    ),
                },
            ),
        ):
            command = _default_refresh_command("/safe/ledger.sqlite3", None)
        self.assertEqual(command, [
            executable, "__refresh-once", "--ledger", "/safe/ledger.sqlite3",
        ])

    def test_pyinstaller_runtime_reexecutes_the_bundled_collector_itself(self):
        executable = "/opt/UsageHub/resources/collector/openusage-collector"
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", "/tmp/openusage-pyinstaller", create=True),
            patch.object(sys, "executable", executable),
            patch.dict(
                "os.environ",
                {
                    "EXECUTABLEPATH": "/tmp/untrusted-py2app-helper",
                    "RESOURCEPATH": "/tmp/untrusted-py2app-resources",
                },
            ),
        ):
            command = _default_refresh_command("/safe/ledger.sqlite3", None)

        self.assertEqual(command, [
            executable, "__refresh-once", "--ledger", "/safe/ledger.sqlite3",
        ])

    def test_pyinstaller_runtime_rejects_a_relative_self_executable(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", "/tmp/openusage-pyinstaller", create=True),
            patch.object(sys, "executable", "relative-openusage-collector"),
            self.assertRaises(CLIError),
        ):
            _default_refresh_command("/safe/ledger.sqlite3", None)

    def test_internal_refresh_writes_privacy_safe_source_class_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.sqlite3"
            timing = Path(directory) / "timing.json"

            def factory(_store, *, timing_recorder=None):
                class Refresher:
                    def refresh(self):
                        timing_recorder.record("local_file", "success", 0.25)

                    def performance_timing_snapshot(self):
                        return timing_recorder.snapshot()

                return Refresher()

            with patch(
                "openusage_bar.collector_cli.build_default_refresher",
                side_effect=factory,
            ):
                result = main(
                    [
                        INTERNAL_REFRESH_COMMAND,
                        "--ledger",
                        str(ledger),
                        "--performance-output",
                        str(timing),
                    ],
                    stderr=io.StringIO(),
                )

            self.assertEqual(result, 0)
            payload = json.loads(timing.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["classes"][0]["sourceClass"], "local_file"
            )
            self.assertEqual(timing.stat().st_mode & 0o777, 0o600)

    def test_frozen_helper_rejects_executable_outside_its_bundle(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(
                sys,
                "executable",
                "/Applications/OpenUsage Bar.app/Contents/MacOS/python",
            ),
            patch.dict(
                "os.environ",
                {
                    "EXECUTABLEPATH": "/tmp/untrusted-helper",
                    "RESOURCEPATH": (
                        "/Applications/OpenUsage Bar.app/Contents/Resources"
                    ),
                },
            ),
            self.assertRaises(CLIError),
        ):
            _default_refresh_command("/safe/ledger.sqlite3", None)

    def test_frozen_helper_rejects_consistent_external_fake_bundle(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(
                sys,
                "executable",
                "/Applications/OpenUsage Bar.app/Contents/MacOS/python",
            ),
            patch.dict(
                "os.environ",
                {
                    "EXECUTABLEPATH": (
                        "/tmp/Evil.app/Contents/MacOS/evil-helper"
                    ),
                    "RESOURCEPATH": "/tmp/Evil.app/Contents/Resources",
                },
            ),
            self.assertRaises(CLIError),
        ):
            _default_refresh_command("/safe/ledger.sqlite3", None)


def seeded_store(path=":memory:"):
    store = ActivityStore(path)
    store.replace_daily_usage(
        "codex",
        "2026-07-14",
        [DailyUsageRow(
            day="2026-07-14", provider_id="codex", model_id="gpt-5.5",
            input_tokens=60, output_tokens=20, cache_read_tokens=20,
            cache_creation_tokens=0, reasoning_tokens=None, total_tokens=100,
            cost_amount=None, cost_currency=None, cost_basis=None, quality="direct",
            imported_at="2026-07-14T09:00:00Z",
        )],
    )
    store.replace_daily_costs(
        "openai",
        "2026-07-14",
        [DailyCostRow(
            day="2026-07-14", provider_id="openai", cost_kind="actual",
            currency="USD", amount="12.34", basis="provider_reported",
            quality="direct", imported_at="2026-07-14T09:00:00Z",
        )],
    )
    store.record_quota(QuotaObservation(
        record_id="minimax.five_hour", observed_at="2026-07-14T09:00:00Z",
        provider_id="minimax", quota_name="Five hour", unit="percent", used="82",
        quota_limit="100", remaining="18", remaining_ratio=0.18,
        resets_at="2026-07-14T12:00:00Z", period_start=None, period_end=None,
        state="ok", quality="direct", stale=False,
    ))
    store.record_source_success("minimax", "current.quota", NOW)
    return store


class FakeRefresher:
    def __init__(self, *, error=None, blocker=None):
        self.calls = 0
        self.error = error
        self.blocker = blocker
        self.active = 0
        self.max_active = 0

    def refresh(self):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.active, self.max_active)
        try:
            if self.blocker:
                self.blocker.wait()
            if self.error:
                raise self.error
        finally:
            self.active -= 1


class MutatingRefresher:
    def __init__(self, store, *, blocker=None):
        self.store = store
        self.blocker = blocker
        self.mutated = threading.Event()
        self.post_block_read = threading.Event()
        self.finished = threading.Event()

    def refresh(self):
        self.store.replace_daily_usage(
            "codex",
            "2026-07-14",
            [DailyUsageRow(
                day="2026-07-14", provider_id="codex", model_id="gpt-5.5",
                input_tokens=999, output_tokens=0, cache_read_tokens=0,
                cache_creation_tokens=0, reasoning_tokens=None, total_tokens=999,
                cost_amount=None, cost_currency=None, cost_basis=None, quality="direct",
                imported_at="2026-07-14T09:30:00Z",
            )],
        )
        self.mutated.set()
        if self.blocker is not None:
            self.blocker.wait()
        self.store.high_water_cursor()
        self.post_block_read.set()
        self.finished.set()


class BlockingGatewayServer:
    def __init__(self):
        self.started = threading.Event()
        self.shutdown_requested = threading.Event()
        self.shutdown_calls = 0
        self.close_calls = 0

    def serve_forever(self):
        self.started.set()
        self.shutdown_requested.wait(3)

    def shutdown(self):
        self.shutdown_calls += 1
        self.shutdown_requested.set()

    def server_close(self):
        self.close_calls += 1


class CollectorCLITests(unittest.TestCase):
    def setUp(self):
        self.store = seeded_store()
        self.query = QueryService(self.store, clock=lambda: NOW)

    def tearDown(self):
        self.store.close()

    def run_cli(self, argv, **dependencies):
        stdout, stderr = io.StringIO(), io.StringIO()
        dependencies.setdefault("clock", lambda: NOW)
        code = main(argv, stdout=stdout, stderr=stderr, store=self.store, query=self.query, **dependencies)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_manual_windows_tcp_daemon_uses_shared_default_token_path_and_preserves_override(self):
        local_app_data = r"C:\Users\example\AppData\Local"

        class CatalogMonitor:
            def maybe_run(self):
                return None

        cases = (
            (
                "explicit",
                ["--api-token-path", r"D:\private\manual-api.token"],
                r"D:\private\manual-api.token",
            ),
            (
                "default",
                [],
                r"C:\Users\example\AppData\Local\openusage-bar\api.token",
            ),
        )

        for name, token_arguments, expected in cases:
            with self.subTest(name=name):
                received = {}
                server = BlockingGatewayServer()

                def create_server(query, **kwargs):
                    received["query"] = query
                    received.update(kwargs)
                    return server

                with (
                    patch.object(sys, "platform", "win32"),
                    patch.dict(
                        "os.environ",
                        {"LOCALAPPDATA": local_app_data},
                    ),
                    patch(
                        "openusage_bar.local_api.create_tcp_server",
                        side_effect=create_server,
                    ),
                ):
                    code, out, err = self.run_cli(
                        [
                            "daemon",
                            "--interval",
                            "60",
                            "--api-transport",
                            "tcp",
                            *token_arguments,
                        ],
                        refresher=FakeRefresher(),
                        stop_event=threading.Event(),
                        waiter=lambda _seconds: True,
                        catalog_monitor=CatalogMonitor(),
                    )

                self.assertEqual((code, out, err), (0, "", ""))
                self.assertIs(received["query"], self.query)
                self.assertEqual(received["token_path"], expected)

    def test_windows_gateway_start_uses_local_app_data_token_and_preserves_override(self):
        local_app_data = r"C:\Users\example\AppData\Local"
        cases = (
            (
                "default",
                {"LOCALAPPDATA": local_app_data},
                [],
                rf"{local_app_data}\openusage-bar\gateway.token",
            ),
            (
                "explicit",
                {},
                ["--token-path", r"D:\private\manual-gateway.token"],
                r"D:\private\manual-gateway.token",
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "gateway.json"
            config_path.write_text(
                json.dumps({"enabled": True, "mode": "advise"}),
                encoding="utf-8",
            )
            for name, environment, token_arguments, expected in cases:
                with self.subTest(name=name):
                    received = {}
                    server = BlockingGatewayServer()

                    def gateway_server_factory(*_args, **kwargs):
                        received.update(kwargs)
                        return server

                    stop = threading.Event()
                    stop.set()
                    with (
                        patch.object(sys, "platform", "win32"),
                        patch.dict("os.environ", environment, clear=True),
                    ):
                        code, out, err = self.run_cli(
                            [
                                "gateway",
                                "start",
                                "--config",
                                str(config_path),
                                *token_arguments,
                            ],
                            stop_event=stop,
                            gateway_server_factory=gateway_server_factory,
                        )

                    self.assertEqual((code, out, err), (0, "", ""))
                    self.assertEqual(str(received["token_path"]), expected)

    def test_windows_gateway_start_without_local_app_data_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "gateway.json"
            config_path.write_text(
                json.dumps({"enabled": True, "mode": "advise"}),
                encoding="utf-8",
            )
            posix_token = root / "home/.local/state/openusage-bar/gateway.token"
            factory_calls = []
            store_calls = []

            def gateway_server_factory(*_args, **kwargs):
                factory_calls.append(kwargs)
                return BlockingGatewayServer()

            def store_factory():
                store_calls.append(True)
                return self.store

            stop = threading.Event()
            stop.set()
            with (
                patch.object(sys, "platform", "win32"),
                patch.dict("os.environ", {}, clear=True),
                patch(
                    "openusage_bar.collector_cli.DEFAULT_GATEWAY_TOKEN_PATH",
                    posix_token,
                ),
            ):
                stdout, stderr = io.StringIO(), io.StringIO()
                code = main(
                    [
                        "gateway",
                        "start",
                        "--config",
                        str(config_path),
                    ],
                    stdout=stdout,
                    stderr=stderr,
                    store_factory=store_factory,
                    stop_event=stop,
                    gateway_server_factory=gateway_server_factory,
                )

            self.assertEqual(
                (code, stdout.getvalue(), stderr.getvalue()),
                (2, "", "invalid command input\n"),
            )
            self.assertEqual(store_calls, [])
            self.assertEqual(factory_calls, [])
            self.assertFalse(posix_token.exists())

    def test_gateway_print_config_emits_stable_secret_free_default_json(self):
        stdout, stderr = io.StringIO(), io.StringIO()

        def forbidden_dependency(*_args, **_kwargs):
            raise AssertionError("print-config must not construct runtime dependencies")

        with patch(
            "openusage_bar.keychain.default_keychain",
            side_effect=AssertionError("print-config must not access credentials"),
        ):
            code = main(
                ["gateway", "print-config"],
                stdout=stdout,
                stderr=stderr,
                store_factory=forbidden_dependency,
                gateway_server_factory=forbidden_dependency,
            )

        expected = {
            "cache_enabled": False,
            "enabled": False,
            "host": "127.0.0.1",
            "mode": "observe",
            "port": 17823,
            "proxy_enabled": False,
        }
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        self.assertEqual(
            stdout.getvalue(),
            json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n",
        )
        normalized = stdout.getvalue().casefold()
        for forbidden in ("api_key", "apikey", "credential", "password", "secret", "token"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, normalized)

    def test_gateway_print_config_ignores_an_invalid_or_secret_bearing_default_file(self):
        expected = {
            "cache_enabled": False,
            "enabled": False,
            "host": "127.0.0.1",
            "mode": "observe",
            "port": 17823,
            "proxy_enabled": False,
        }

        def forbidden_dependency(*_args, **_kwargs):
            raise AssertionError("print-config must not construct runtime dependencies")

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "gateway.json"
            cases = {
                "invalid": b'{"enabled":',
                "secret": b'{"enabled":true,"mode":"advise","token":"sk-private"}',
            }
            for name, raw in cases.items():
                with self.subTest(name=name):
                    config_path.write_bytes(raw)
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with (
                        patch(
                            "openusage_bar.collector_cli.DEFAULT_GATEWAY_CONFIG_PATH",
                            config_path,
                        ),
                        patch(
                            "openusage_bar.keychain.default_keychain",
                            side_effect=AssertionError(
                                "print-config must not access credentials"
                            ),
                        ),
                    ):
                        code = main(
                            ["gateway", "print-config"],
                            stdout=stdout,
                            stderr=stderr,
                            store_factory=forbidden_dependency,
                            gateway_server_factory=forbidden_dependency,
                        )

                    self.assertEqual((code, stderr.getvalue()), (0, ""))
                    self.assertEqual(json.loads(stdout.getvalue()), expected)
                    self.assertEqual(config_path.read_bytes(), raw)

    def test_gateway_status_reads_only_config_and_reports_startability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "gateway.json"
            token_path = root / "gateway.token"
            config_text = json.dumps({
                "enabled": True,
                "mode": "advise",
                "host": "127.0.0.1",
                "port": 17823,
                "proxy_enabled": False,
                "cache_enabled": False,
            })
            config_path.write_text(config_text, encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()

            def forbidden_dependency(*_args, **_kwargs):
                raise AssertionError("status must not construct runtime dependencies")

            with patch(
                "openusage_bar.keychain.default_keychain",
                side_effect=AssertionError("status must not access credentials"),
            ):
                code = main(
                    ["gateway", "status", "--config", str(config_path)],
                    stdout=stdout,
                    stderr=stderr,
                    store_factory=forbidden_dependency,
                    gateway_server_factory=forbidden_dependency,
                )

            expected = {
                "cache_enabled": False,
                "can_start": True,
                "enabled": True,
                "host": "127.0.0.1",
                "mode": "advise",
                "port": 17823,
                "proxy_enabled": False,
            }
            self.assertEqual((code, stderr.getvalue()), (0, ""))
            self.assertEqual(json.loads(stdout.getvalue()), expected)
            self.assertEqual(config_path.read_text(encoding="utf-8"), config_text)
            self.assertFalse(token_path.exists())

    def test_gateway_start_fails_closed_for_missing_observe_or_disabled_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = {
                "missing": None,
                "observe": {"enabled": True, "mode": "observe"},
                "disabled": {"enabled": False, "mode": "advise"},
            }
            for name, payload in cases.items():
                with self.subTest(name=name):
                    config_path = root / f"{name}.json"
                    token_path = root / f"{name}.token"
                    if payload is not None:
                        config_path.write_text(json.dumps(payload), encoding="utf-8")

                    def forbidden_dependency(*_args, **_kwargs):
                        raise AssertionError("disabled Gateway must not start dependencies")

                    stdout, stderr = io.StringIO(), io.StringIO()
                    code = main(
                        [
                            "gateway", "start",
                            "--config", str(config_path),
                            "--token-path", str(token_path),
                        ],
                        stdout=stdout,
                        stderr=stderr,
                        store_factory=forbidden_dependency,
                        gateway_server_factory=forbidden_dependency,
                    )

                    self.assertNotEqual(code, 0)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertEqual(stderr.getvalue(), "gateway_disabled\n")
                    self.assertFalse(token_path.exists())

    def test_gateway_start_runs_enabled_modes_with_an_independent_token_and_graceful_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mode in ("advise", "gateway"):
                with self.subTest(mode=mode):
                    mode_root = root / mode
                    mode_root.mkdir()
                    config_path = mode_root / "gateway.json"
                    config_path.write_text(
                        json.dumps({"enabled": True, "mode": mode}),
                        encoding="utf-8",
                    )
                    gateway_token_path = mode_root / "gateway.token"
                    local_api_token_path = mode_root / "api.token"
                    local_api_token = "local-api-sentinel-must-remain-private\n"
                    local_api_token_path.write_text(local_api_token, encoding="utf-8")
                    server = BlockingGatewayServer()
                    received = {}

                    def gateway_server_factory(*_args, **kwargs):
                        received.update(kwargs)
                        Path(kwargs["token_path"]).write_text(
                            "gateway-only-auth-material\n",
                            encoding="utf-8",
                        )
                        return server

                    stdout, stderr = io.StringIO(), io.StringIO()
                    stop = threading.Event()
                    result = []
                    errors = []

                    def run_gateway():
                        try:
                            result.append(main(
                                [
                                    "gateway", "start",
                                    "--config", str(config_path),
                                    "--token-path", str(gateway_token_path),
                                ],
                                stdout=stdout,
                                stderr=stderr,
                                store=self.store,
                                query=self.query,
                                stop_event=stop,
                                gateway_server_factory=gateway_server_factory,
                            ))
                        except BaseException as error:
                            errors.append(error)

                    with patch(
                        "openusage_bar.collector_cli.DEFAULT_API_TOKEN_PATH",
                        local_api_token_path,
                    ):
                        thread = threading.Thread(target=run_gateway)
                        thread.start()
                        deadline = time.monotonic() + 3
                        while (
                            not server.started.is_set()
                            and thread.is_alive()
                            and time.monotonic() < deadline
                        ):
                            server.started.wait(0.01)
                        if server.started.is_set():
                            stop.set()
                        thread.join(3)

                    self.assertEqual(errors, [])
                    self.assertTrue(server.started.is_set())
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(result, [0])
                    self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
                    self.assertEqual(received["config"].mode.value, mode)
                    self.assertTrue(received["config"].enabled)
                    self.assertEqual(Path(received["token_path"]), gateway_token_path)
                    self.assertTrue(gateway_token_path.exists())
                    self.assertEqual(
                        local_api_token_path.read_text(encoding="utf-8"),
                        local_api_token,
                    )
                    self.assertNotIn(local_api_token.strip(), repr(received))
                    self.assertNotIn(local_api_token.strip(), stdout.getvalue())
                    self.assertNotIn(local_api_token.strip(), stderr.getvalue())
                    self.assertEqual(server.shutdown_calls, 1)
                    self.assertEqual(server.close_calls, 1)

    def test_gateway_start_default_factory_serves_advise_and_isolates_local_api_token(self):
        import http.client

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]

            config_path = root / "gateway.json"
            config_path.write_text(
                json.dumps({
                    "enabled": True,
                    "mode": "advise",
                    "host": "127.0.0.1",
                    "port": port,
                    "proxy_enabled": False,
                    "cache_enabled": False,
                }),
                encoding="utf-8",
            )
            gateway_token_path = root / "gateway.token"
            local_api_token_path = root / "api.token"
            local_api_sentinel = b"local-api-sentinel-must-remain-private\n"
            local_api_token_path.write_bytes(local_api_sentinel)

            stdout, stderr = io.StringIO(), io.StringIO()
            stop = threading.Event()
            result = []
            errors = []

            def run_gateway():
                try:
                    result.append(main(
                        [
                            "gateway", "start",
                            "--config", str(config_path),
                            "--token-path", str(gateway_token_path),
                        ],
                        stdout=stdout,
                        stderr=stderr,
                        store=self.store,
                        query=self.query,
                        stop_event=stop,
                    ))
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=run_gateway)
            status = 0
            response_body = b""
            try:
                with patch(
                    "openusage_bar.collector_cli.DEFAULT_API_TOKEN_PATH",
                    local_api_token_path,
                ):
                    thread.start()
                    deadline = time.monotonic() + 3
                    while (
                        not gateway_token_path.exists()
                        and thread.is_alive()
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    token = gateway_token_path.read_text(encoding="ascii")

                    request_body = json.dumps({
                        "provider": "minimax",
                        "model": "MiniMax-M2.1",
                        "estimated_tokens": 8_000,
                        "window": "5m",
                    }).encode("utf-8")
                    while thread.is_alive() and time.monotonic() < deadline:
                        connection = http.client.HTTPConnection(
                            "127.0.0.1", port, timeout=1
                        )
                        try:
                            connection.request(
                                "POST",
                                "/gateway/v1/should-send",
                                body=request_body,
                                headers={
                                    "Authorization": f"Bearer {token}",
                                    "Content-Type": "application/json",
                                },
                            )
                            response = connection.getresponse()
                            status = response.status
                            response_body = response.read()
                            break
                        except OSError:
                            time.sleep(0.02)
                        finally:
                            connection.close()
            finally:
                stop.set()
                thread.join(3)

            self.assertEqual(errors, [])
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [0])
            self.assertEqual(status, 200)
            payload = json.loads(response_body)
            self.assertEqual(
                set(payload),
                {"decision", "confidence", "reason", "defer_until", "details"},
            )
            self.assertEqual(
                set(payload["details"]),
                {
                    "quota_remaining",
                    "burn_rate_per_min",
                    "predicted_exhaustion_minutes",
                },
            )
            self.assertEqual(local_api_token_path.read_bytes(), local_api_sentinel)
            observed = stdout.getvalue().encode() + stderr.getvalue().encode() + response_body
            self.assertNotIn(local_api_sentinel.strip(), observed)
            self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))

    def test_gateway_config_errors_are_stable_and_never_echo_secret_material(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = {
                "invalid": {"enabled": True, "mode": "unsupported"},
                "secret": {
                    "enabled": True,
                    "mode": "advise",
                    "token": "sk-sensitive-material-must-not-echo",
                },
            }
            for name, payload in cases.items():
                with self.subTest(name=name):
                    config_path = root / f"{name}.json"
                    config_path.write_text(json.dumps(payload), encoding="utf-8")
                    stdout, stderr = io.StringIO(), io.StringIO()

                    def forbidden_factory(*_args, **_kwargs):
                        raise AssertionError("invalid Gateway config must not start")

                    code = main(
                        ["gateway", "status", "--config", str(config_path)],
                        stdout=stdout,
                        stderr=stderr,
                        store_factory=forbidden_factory,
                        gateway_server_factory=forbidden_factory,
                    )

                    self.assertEqual(code, 2)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertEqual(stderr.getvalue(), "invalid gateway configuration\n")
                    combined = stdout.getvalue() + stderr.getvalue()
                    self.assertNotIn("sk-sensitive-material-must-not-echo", combined)


    def test_json_commands_write_only_canonical_payload(self):
        for command in (
            ["status", "--format", "json"],
            ["snapshot", "--today", "2026-07-14", "--format", "json"],
            ["quotas", "--format", "json"],
            ["sources", "--format", "json"],
            ["providers", "--format", "json"],
            ["doctor", "--format", "json"],
        ):
            with self.subTest(command=command):
                code, out, err = self.run_cli(command, offline=True)
                self.assertEqual((code, err), (0, ""))
                payload = json.loads(out)
                self.assertEqual(payload["schemaVersion"], "1.0")
                self.assertNotIn("\n\n", out)

    def test_snapshot_command_uses_the_canonical_resource_payload(self):
        code, out, err = self.run_cli(
            [
                "snapshot", "--today", "2026-07-14",
                "--format", "json", "--offline",
            ]
        )

        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            json.loads(out),
            to_wire(self.query.resource_snapshot(datetime(2026, 7, 14).date())),
        )

    def test_providers_json_reuses_query_boundary_and_whitelists_instance_fields(self):
        self.store.upsert_provider_instance(ProviderInstance(
            provider_id="minimax-primary", family_id="minimax",
            display_name="MiniMax primary", category="subscription",
            credential_source="minimax_builtin_api", source_kind="builtin_api",
            observed_at="2026-07-14T09:00:00Z",
        ))
        self.store.upsert_provider_instance(ProviderInstance(
            provider_id="minimax-generated", family_id="minimax",
            display_name="minimax", category="subscription",
            credential_source="minimax_builtin_api", source_kind="builtin_api",
            observed_at="2026-07-14T09:00:00Z",
        ))
        self.store.upsert_provider_instance(ProviderInstance(
            provider_id="mistral-custom", family_id="mistral",
            display_name="miſtral", category="api",
            credential_source="openusage", source_kind="openusage",
            observed_at="2026-07-14T09:00:00Z",
        ))
        code, out, err = self.run_cli(
            ["providers", "--format", "json", "--offline"]
        )
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(payload["generatedAt"], "2026-07-14T10:00:00Z")
        providers = {item["providerId"]: item for item in payload["providers"]}
        self.assertEqual(providers["minimax-generated"]["familyId"], "minimax")
        self.assertEqual(providers["minimax-generated"]["displayName"], "MiniMax")
        self.assertEqual(providers["minimax-primary"]["displayName"], "MiniMax primary")
        self.assertEqual(providers["mistral-custom"]["displayName"], "miſtral")
        self.assertEqual(set(providers["minimax-generated"]), {
            "providerId", "familyId", "displayName", "category",
            "credentialSource", "sourceKind", "observedAt", "revision",
        })

    def test_usage_jsonl_has_business_rows_then_checkpoint(self):
        code, out, err = self.run_cli([
            "usage", "--from", "2026-07-01", "--to", "2026-07-14", "--format", "jsonl"
        ], offline=True)
        lines = [json.loads(line) for line in out.splitlines()]
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(lines[0]["type"], "usage")
        self.assertEqual(lines[0]["providerId"], "codex")
        self.assertEqual(lines[0]["tokenCountingConvention"], "unknown")
        self.assertTrue(any(line.get("type") == "coverage" for line in lines))
        self.assertEqual(lines[-1]["type"], "checkpoint")
        self.assertIn("dataRevision", lines[-1])

    def test_costs_jsonl_has_cost_coverage_then_checkpoint(self):
        code, out, err = self.run_cli([
            "costs", "--from", "2026-07-13", "--to", "2026-07-14", "--format", "jsonl"
        ], offline=True)
        lines = [json.loads(line) for line in out.splitlines()]
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(lines[0]["type"], "cost")
        self.assertEqual((lines[0]["providerId"], lines[0]["amount"]), ("openai", "12.34"))
        self.assertEqual(
            [line["covered"] for line in lines if line.get("type") == "costCoverage"],
            [False, True],
        )
        self.assertEqual(lines[-1]["type"], "checkpoint")
        self.assertEqual(lines[-1]["nextCursor"], lines[-1]["dataRevision"])

    def test_costs_json_keeps_query_envelope(self):
        code, out, err = self.run_cli([
            "costs", "--from", "2026-07-14", "--to", "2026-07-14", "--format", "json"
        ], offline=True)
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(payload["schemaVersion"], "1.0")
        self.assertEqual(payload["rows"][0]["costKind"], "actual")

    def test_usage_jsonl_emits_covered_zero_and_missing_coverage_records(self):
        self.store.replace_daily_usage("codex", "2026-07-13", [])
        code, out, err = self.run_cli([
            "usage", "--from", "2026-07-13", "--to", "2026-07-15", "--format", "jsonl"
        ], offline=True)
        lines = [json.loads(line) for line in out.splitlines()]
        coverage = {
            line["day"]: line["covered"]
            for line in lines if line.get("type") == "coverage" and line["providerId"] == "codex"
        }
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(coverage, {
            "2026-07-13": True,
            "2026-07-14": True,
            "2026-07-15": False,
        })

    def test_usage_jsonl_empty_unfiltered_ledger_invents_no_provider_scope(self):
        empty = ActivityStore(":memory:")
        try:
            query = QueryService(empty, clock=lambda: NOW)
            stdout, stderr = io.StringIO(), io.StringIO()
            code = main([
                "usage", "--from", "2026-07-13", "--to", "2026-07-15", "--format", "jsonl", "--offline"
            ], stdout=stdout, stderr=stderr, store=empty, query=query)
            lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
            self.assertEqual((code, stderr.getvalue()), (0, ""))
            self.assertEqual([line["type"] for line in lines], ["checkpoint"])
        finally:
            empty.close()

    def test_usage_and_changes_json_formats_keep_envelopes(self):
        code, out, err = self.run_cli([
            "usage", "--from", "2026-07-14", "--to", "2026-07-14", "--format", "json"
        ], offline=True)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["schemaVersion"], "1.0")
        code, out, err = self.run_cli([
            "changes", "--after", "0", "--limit", "1", "--format", "json"
        ], offline=True)
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertIn("nextCursor", payload)
        self.assertTrue(payload["hasMore"])

    def test_changes_jsonl_checkpoint_uses_next_cursor(self):
        code, out, _ = self.run_cli(["changes", "--after", "0", "--limit", "100", "--format", "jsonl"], offline=True)
        lines = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1]["nextCursor"], self.store.current_change_seq)
        self.assertFalse(lines[-1]["hasMore"])

    def test_changes_ahead_cursor_is_invalid_without_business_stdout(self):
        code, out, err = self.run_cli([
            "changes", "--after", str(self.store.high_water_cursor() + 1),
            "--limit", "100", "--format", "jsonl", "--offline",
        ])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(err, "invalid query input\n")

    def test_offline_never_invokes_refresh_even_with_fresh(self):
        refresher = FakeRefresher()
        code, out, err = self.run_cli(["status", "--format", "json", "--offline", "--fresh"], refresher=refresher)
        self.assertEqual(code, 0)
        self.assertTrue(out)
        self.assertEqual((err, refresher.calls), ("", 0))

    def test_common_flags_are_also_accepted_before_subcommand(self):
        refresher = FakeRefresher()
        code, out, err = self.run_cli(
            ["--offline", "--fresh", "status", "--format", "json"],
            refresher=refresher,
        )
        self.assertEqual((code, err, refresher.calls), (0, "", 0))
        self.assertTrue(out)

    def test_offline_daemon_serves_only_last_good_ledger_data(self):
        injected_refresher = FakeRefresher(
            error=AssertionError("offline daemon must not refresh")
        )
        factory_calls = []
        runner_calls = []

        def refresher_factory(_store):
            factory_calls.append(True)
            return FakeRefresher(
                error=AssertionError("offline daemon factory result must not refresh")
            )

        class CatalogMonitor:
            def maybe_run(self):
                raise AssertionError("offline daemon must not monitor the catalog")

        def run_daemon_with_api(
            interval, daemon_refresher, active_query, api_socket, **options
        ):
            runner_calls.append(
                (interval, daemon_refresher, active_query, api_socket, options)
            )
            return 0

        command = [
            "--offline",
            "daemon",
            "--interval",
            "60",
            "--api-transport",
            "tcp",
            "--api-port",
            "17821",
            "--api-token-path",
            "unused.token",
        ]
        cases = (
            {
                "refresher": injected_refresher,
                "refresher_factory": refresher_factory,
                "catalog_monitor": CatalogMonitor(),
            },
            {"refresher_factory": refresher_factory},
        )
        with (
            patch(
                "openusage_bar.collector_cli._run_daemon_with_api",
                side_effect=run_daemon_with_api,
            ),
            patch(
                "openusage_bar.daily_history.OpenUsageCatalogMonitor",
                return_value=CatalogMonitor(),
            ) as monitor_type,
        ):
            for dependencies in cases:
                with self.subTest(dependencies=tuple(dependencies)):
                    code, out, err = self.run_cli(command, **dependencies)
                    self.assertEqual((code, out, err), (0, "", ""))

        self.assertEqual(len(runner_calls), len(cases))
        for index, (_, daemon_refresher, _, _, options) in enumerate(runner_calls):
            with self.subTest(case=index, boundary="catalog monitor"):
                self.assertIsNone(options["catalog_monitor"])
            with self.subTest(case=index, boundary="refresher"):
                try:
                    result = daemon_refresher.refresh()
                except Exception as error:
                    self.fail(f"offline daemon refresher was not a no-op: {error}")
                self.assertIsNone(result)
        self.assertEqual(injected_refresher.calls, 0)
        self.assertEqual(factory_calls, [])
        self.assertEqual(monitor_type.call_count, 0)

    @unittest.skipIf(sys.platform == "win32", "Windows uses loopback TCP transport")
    def test_daemon_serves_private_unix_api_and_cleans_socket_on_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "openusage.sock"
            stop = threading.Event()
            result = []

            thread = threading.Thread(
                target=lambda: result.append(main(
                    ["daemon", "--interval", "60", "--api-socket", str(socket_path)],
                    stderr=io.StringIO(), store=self.store, query=self.query,
                    refresher=FakeRefresher(), stop_event=stop,
                ))
            )
            thread.start()
            deadline = time.monotonic() + 3
            while not socket_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(socket_path.exists())
            mode = socket_path.stat().st_mode & 0o777
            deadline = time.monotonic() + 3
            while mode != 0o600 and time.monotonic() < deadline:
                time.sleep(0.01)
                mode = socket_path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)

            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(2)
            client.connect(str(socket_path))
            client.sendall(b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            response = b""
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                response += chunk
            client.close()
            self.assertIn(b"HTTP/1.1 200", response)
            self.assertIn(b'"schemaVersion":"1.0"', response)

            stop.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [0])
            self.assertFalse(socket_path.exists())

    def test_service_print_renders_current_platform(self):
        with patch(
            "openusage_bar.platform_services.render_current_platform",
            return_value="native-service-definition",
        ) as render:
            code, out, err = self.run_cli(
                ["service", "print", "--interval", "300"],
                refresher=FakeRefresher(),
            )

        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "native-service-definition\n")
        render.assert_called_once_with(interval=300, command=None)

    def test_service_print_passes_one_absolute_packaged_collector_command(self):
        command = "/opt/Usage Hub/resources/collector/openusage-collector"
        with patch(
            "openusage_bar.platform_services.render_current_platform",
            return_value="[Unit]",
        ) as render:
            code, out, err = self.run_cli(
                [
                    "service",
                    "print",
                    "--interval",
                    "300",
                    "--command",
                    command,
                ],
                refresher=FakeRefresher(),
            )

        self.assertEqual((code, out, err), (0, "[Unit]\n", ""))
        render.assert_called_once_with(interval=300, command=command)

    def test_service_print_does_not_open_the_activity_ledger(self):
        stdout, stderr = io.StringIO(), io.StringIO()

        def forbidden_store_factory():
            raise AssertionError("service commands must not open the activity ledger")

        with patch(
            "openusage_bar.platform_services.render_current_platform",
            return_value="[Unit]",
        ):
            code = main(
                ["service", "print", "--interval", "300"],
                stdout=stdout,
                stderr=stderr,
                store_factory=forbidden_store_factory,
            )

        self.assertEqual((code, stdout.getvalue(), stderr.getvalue()), (
            0,
            "[Unit]\n",
            "",
        ))

    def test_desktop_service_dispatches_fixed_actions_before_opening_the_ledger(self):
        calls = []
        managed = SimpleNamespace(
            install_managed_collector=lambda *, interval=300: calls.append(
                ("install", interval)
            ),
            uninstall_managed_collector=lambda: calls.append(("uninstall",)),
        )

        def forbidden_store_factory():
            raise AssertionError("desktop-service must not open the activity ledger")

        with patch.dict(
            sys.modules,
            {"openusage_bar.managed_collector": managed},
        ):
            results = []
            for argv in (
                ["desktop-service", "install", "--interval", "300"],
                ["desktop-service", "uninstall"],
            ):
                stdout, stderr = io.StringIO(), io.StringIO()
                code = main(
                    argv,
                    stdout=stdout,
                    stderr=stderr,
                    store_factory=forbidden_store_factory,
                )
                results.append((code, stdout.getvalue(), stderr.getvalue()))

        self.assertEqual(results, [(0, "", ""), (0, "", "")])
        self.assertEqual(calls, [("install", 300), ("uninstall",)])

    def test_desktop_service_rejects_caller_paths_without_reflecting_them(self):
        private_path = "/private/canary/openusage-collector"
        code, out, err = self.run_cli(
            [
                "desktop-service",
                "install",
                "--interval",
                "300",
                "--command",
                private_path,
            ],
        )

        self.assertEqual((code, out, err), (2, "", "invalid command input\n"))
        self.assertNotIn(private_path, err)

    def test_desktop_service_install_requires_the_exact_explicit_interval(self):
        for argv in (
            ["desktop-service", "install"],
            ["desktop-service", "install", "--interval", "301"],
        ):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(argv)
                self.assertEqual(
                    (code, out, err),
                    (2, "", "invalid command input\n"),
                )
                self.assertNotIn("301", err)

    def test_desktop_service_failure_is_fixed_and_path_free(self):
        private_error = "/private/canary/managed-collector"

        def fail_install(*, interval=300):
            raise RuntimeError(private_error)

        managed = SimpleNamespace(
            install_managed_collector=fail_install,
            uninstall_managed_collector=lambda: None,
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(
            sys.modules,
            {"openusage_bar.managed_collector": managed},
        ):
            code = main(
                ["desktop-service", "install", "--interval", "300"],
                stdout=stdout,
                stderr=stderr,
                store_factory=lambda: (_ for _ in ()).throw(
                    AssertionError("desktop-service must dispatch before ledger")
                ),
            )

        self.assertEqual(
            (code, stdout.getvalue(), stderr.getvalue()),
            (1, "", "desktop service action failed\n"),
        )
        self.assertNotIn(private_error, stderr.getvalue())

    def test_confirmed_state_delete_removes_owned_roots_without_opening_ledger(self):
        stdout, stderr = io.StringIO(), io.StringIO()

        def forbidden_store_factory():
            raise AssertionError("state commands must not open the activity ledger")

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            state_root = home / ".local" / "state" / "openusage-bar"
            config_root = home / ".config" / "openusage-bar"
            sibling = home / ".local" / "state" / "keep.txt"
            state_root.mkdir(parents=True)
            config_root.mkdir(parents=True)
            sibling.write_text("keep", encoding="utf-8")
            (state_root / "activity.sqlite3").write_bytes(b"ledger")
            (config_root / "providers.json").write_text("[]", encoding="utf-8")
            paths = LifecycleStatePaths(platform="linux", home=home)

            with patch(
                "openusage_bar.lifecycle_state.LifecycleStatePaths.for_current_user",
                return_value=paths,
            ), patch(
                "openusage_bar.lifecycle_state.current_user_runtime_is_active",
                return_value=False,
            ):
                code = main(
                    [
                        "state",
                        "delete",
                        "--confirm",
                        "DELETE-LOCAL-USAGEHUB-STATE",
                        "--format",
                        "json",
                    ],
                    stdout=stdout,
                    stderr=stderr,
                    store_factory=forbidden_store_factory,
                )

            self.assertFalse(state_root.exists())
            self.assertFalse(config_root.exists())
            self.assertEqual(sibling.read_text(encoding="utf-8"), "keep")

        self.assertEqual(
            (code, stdout.getvalue(), stderr.getvalue()),
            (
                0,
                '{"apiVersion":"local-state-lifecycle/v1","deleted":true,'
                '"object":"local.state_delete"}\n',
                "",
            ),
        )

    def test_state_delete_rejects_unconfirmed_private_input_without_echoing_it(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        private_input = "/private/canary/DELETE"
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            state_root = home / ".local" / "state" / "openusage-bar"
            state_root.mkdir(parents=True)
            preserved = state_root / "activity.sqlite3"
            preserved.write_bytes(b"ledger")
            paths = LifecycleStatePaths(platform="linux", home=home)
            with patch(
                "openusage_bar.lifecycle_state.LifecycleStatePaths.for_current_user",
                return_value=paths,
            ):
                code = main(
                    [
                        "state",
                        "delete",
                        "--confirm",
                        private_input,
                        "--format",
                        "json",
                    ],
                    stdout=stdout,
                    stderr=stderr,
                    store_factory=lambda: (_ for _ in ()).throw(
                        AssertionError("state delete must not open the ledger")
                    ),
                )

            self.assertEqual(preserved.read_bytes(), b"ledger")

        self.assertEqual(
            (code, stdout.getvalue(), stderr.getvalue()),
            (1, "", "local state delete failed\n"),
        )
        self.assertNotIn(private_input, stderr.getvalue())

    def test_executor_list_returns_both_plugins(self):
        code, out, err = self.run_cli(
            ["executor", "list", "--format", "json"],
            refresher=FakeRefresher(),
        )

        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(payload["schemaVersion"], "1.0")
        self.assertEqual(
            {item["pluginId"] for item in payload["executors"]},
            {"cc_switch", "omniroute"},
        )

    def test_connect_returns_console_url(self):
        code, out, err = self.run_cli(
            ["connect", "--family", "deepseek", "--format", "json"]
        )

        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(payload["consoleUrl"], "https://platform.deepseek.com")
        self.assertIn("api_key", payload["authModes"])

    def test_connect_unknown_family_fails(self):
        code, out, err = self.run_cli(
            ["connect", "--family", "not-a-family", "--format", "json"]
        )

        self.assertEqual((code, err), (2, "invalid provider family\n"))

    def test_dashboard_invalid_port_fails(self):
        code, out, err = self.run_cli(["dashboard", "--port", "70000"])

        self.assertEqual((code, err), (2, "invalid dashboard port\n"))

    def test_daemon_serves_loopback_tcp_api_with_bearer_token(self):
        import http.client

        daemon_cleanup_timeout_seconds = 7
        with tempfile.TemporaryDirectory() as directory:
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
            token_path = Path(directory) / "api.token"
            stop = threading.Event()
            result = []

            thread = threading.Thread(
                target=lambda: result.append(main(
                    [
                        "daemon", "--interval", "60",
                        "--api-transport", "tcp",
                        "--api-port", str(port),
                        "--api-token-path", str(token_path),
                    ],
                    stderr=io.StringIO(), store=self.store, query=self.query,
                    refresher=FakeRefresher(), stop_event=stop,
                ))
            )
            thread.start()
            deadline = time.monotonic() + 3
            token = None
            while not token_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            token = token_path.read_text(encoding="utf-8").strip()
            self.assertGreaterEqual(len(token), 43)

            body = b""
            status = 0
            while time.monotonic() < deadline:
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                    connection.request(
                        "GET", "/v1/health",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    response = connection.getresponse()
                    status = response.status
                    body = response.read()
                    connection.close()
                    break
                except OSError:
                    time.sleep(0.05)
            self.assertEqual(status, 200)
            self.assertIn(b'"schemaVersion":"1.0"', body)

            stop.set()
            thread.join(daemon_cleanup_timeout_seconds)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [0])

    def test_fresh_timeout_is_real_and_returns_last_good_without_sleep(self):
        blocker = threading.Event()
        refresher = FakeRefresher(blocker=blocker)
        timeout_seen = threading.Event()

        def join_thread(thread, timeout):
            self.assertGreater(timeout, 0)
            timeout_seen.set()
            return False

        try:
            code, out, err = self.run_cli(
                ["status", "--format", "json", "--fresh"],
                refresher=refresher, thread_joiner=join_thread,
            )
        finally:
            blocker.set()
        self.assertTrue(timeout_seen.is_set())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["todayTokens"], 100)
        self.assertIn("refresh unavailable", err)

    def test_fresh_timeout_freezes_last_good_and_defers_owned_store_close(self):
        blocker = threading.Event()
        owned = seeded_store()
        refresher = MutatingRefresher(owned, blocker=blocker)
        closed = threading.Event()
        original_close = owned.close

        def close():
            closed.set()
            original_close()

        owned.close = close
        stdout, stderr = io.StringIO(), io.StringIO()

        def joiner(_thread, _timeout):
            self.assertTrue(refresher.mutated.wait(1))
            return False

        code = main(
            ["status", "--format", "json", "--fresh"],
            stdout=stdout, stderr=stderr, store_factory=lambda: owned,
            refresher=refresher, clock=lambda: NOW, thread_joiner=joiner,
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["todayTokens"], 100)
        self.assertIn("refresh unavailable", stderr.getvalue())
        self.assertFalse(closed.is_set())
        blocker.set()
        self.assertTrue(refresher.post_block_read.wait(1))
        self.assertTrue(refresher.finished.wait(1))
        self.assertTrue(closed.wait(1))

    def test_fresh_timeout_never_closes_caller_owned_store(self):
        blocker = threading.Event()
        refresher = MutatingRefresher(self.store, blocker=blocker)

        def joiner(_thread, _timeout):
            self.assertTrue(refresher.mutated.wait(1))
            return False

        try:
            code, out, err = self.run_cli(
                ["status", "--format", "json", "--fresh"],
                refresher=refresher, thread_joiner=joiner,
            )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["todayTokens"], 100)
            self.assertIn("refresh unavailable", err)
        finally:
            blocker.set()
            self.assertTrue(refresher.post_block_read.wait(1))
            self.assertTrue(refresher.finished.wait(1))
        self.assertEqual(self.store.summary("2026-07-14", "2026-07-14").total_tokens, 999)

    def test_successful_fresh_requeries_and_emits_new_value(self):
        refresher = MutatingRefresher(self.store)
        code, out, err = self.run_cli(
            ["status", "--format", "json", "--fresh"], refresher=refresher
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["todayTokens"], 999)

    @unittest.skipIf(sys.platform == "win32", "POSIX path/process-group test")
    def test_default_fresh_uses_secret_safe_bounded_subprocess_and_requeries(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            self.store.replace_daily_usage(
                "codex", "2026-07-14",
                [DailyUsageRow(
                    day="2026-07-14", provider_id="codex", model_id="gpt",
                    input_tokens=999, output_tokens=0, cache_read_tokens=0,
                    cache_creation_tokens=0, reasoning_tokens=None, total_tokens=999,
                    cost_amount=None, cost_currency=None, cost_basis=None,
                    quality="direct", imported_at="2026-07-14T09:30:00Z",
                )],
            )
            return subprocess.CompletedProcess(command, 0)

        code, out, err = self.run_cli(
            ["status", "--format", "json", "--fresh"],
            subprocess_runner=runner,
            refresh_entrypoint=Path("/safe/openusage_collector.py"),
            child_environment={"PATH": "/usr/bin", "SECRET_TOKEN": "must-not-pass"},
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["todayTokens"], 999)
        command, kwargs = calls[0]
        self.assertEqual(command[1:], [
            "/safe/openusage_collector.py", "__refresh-once", "--ledger", self.store.path,
        ])
        self.assertFalse(kwargs["shell"])
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], DEFAULT_FRESH_TIMEOUT_SECONDS)
        self.assertNotIn("SECRET_TOKEN", kwargs["env"])
        self.assertNotIn("must-not-pass", repr(command))

    def test_default_fresh_refuses_unshareable_memory_ledger_without_child(self):
        with patch(
            "openusage_bar.collector_cli.subprocess.run",
            side_effect=AssertionError("child must not launch"),
        ) as runner:
            code, out, err = self.run_cli(
                ["status", "--format", "json", "--fresh"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["todayTokens"], 100)
        self.assertEqual(err, "refresh unavailable; using last-good ledger data\n")
        runner.assert_not_called()

    def test_invalid_frozen_runtime_path_keeps_last_good_without_launching_child(self):
        def runner(_command, **_kwargs):
            raise AssertionError("untrusted child must not launch")

        with (
            patch.object(sys, "frozen", True, create=True),
            patch.dict(
                "os.environ",
                {
                    "EXECUTABLEPATH": "/tmp/untrusted-helper",
                    "RESOURCEPATH": (
                        "/Applications/OpenUsage Bar.app/Contents/Resources"
                    ),
                },
            ),
        ):
            code, out, err = self.run_cli(
                ["status", "--format", "json", "--fresh"],
                subprocess_runner=runner,
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["todayTokens"], 100)
        self.assertEqual(
            err, "refresh unavailable; using last-good ledger data\n"
        )

    def test_default_fresh_memory_ledger_preserves_strict_exit(self):
        empty = ActivityStore(":memory:")
        try:
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("openusage_bar.collector_cli.subprocess.run") as runner:
                code = main(
                    ["status", "--format", "json", "--fresh", "--strict"],
                    stdout=stdout, stderr=stderr, store=empty,
                    query=QueryService(empty, clock=lambda: NOW), clock=lambda: NOW,
                )
            self.assertEqual(code, 3)
            self.assertIsNone(json.loads(stdout.getvalue())["todayTokens"])
            self.assertIn("refresh unavailable", stderr.getvalue())
            runner.assert_not_called()
        finally:
            empty.close()

    def test_default_fresh_timeout_is_reaped_and_owned_store_closes_normally(self):
        owned = seeded_store()
        closed = threading.Event()
        original_close = owned.close
        calls = []

        def close():
            closed.set()
            original_close()

        owned.close = close

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(
            ["status", "--format", "json", "--fresh"],
            stdout=stdout, stderr=stderr, store_factory=lambda: owned,
            clock=lambda: NOW, subprocess_runner=runner,
            refresh_entrypoint=Path("/safe/openusage_collector.py"),
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["todayTokens"], 100)
        self.assertEqual(stderr.getvalue(), "refresh unavailable; using last-good ledger data\n")
        self.assertTrue(closed.is_set())
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0][1]["timeout"], DEFAULT_FRESH_TIMEOUT_SECONDS
        )

    @unittest.skipIf(sys.platform == "win32", "POSIX process-group test")
    def test_default_fresh_timeout_kills_and_reaps_forked_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pidfile = root / "descendant.pid"
            helper = root / "refresh-helper"
            helper.write_text(
                f"#!{sys.executable}\n"
                "import os,time\n"
                "child=os.fork()\n"
                "if child==0: time.sleep(30); raise SystemExit\n"
                f"open({str(pidfile)!r},'w').write(str(child))\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            helper.chmod(0o700)
            code, out, err = self.run_cli(
                ["status", "--format", "json", "--fresh"],
                refresh_entrypoint=helper,
                fresh_timeout=1,
            )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["todayTokens"], 100)
            self.assertIn("refresh unavailable", err)
            descendant = int(pidfile.read_text(encoding="utf-8"))
            state = ""
            for _ in range(20):
                state = subprocess.run(
                    ["/bin/ps", "-o", "stat=", "-p", str(descendant)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ).stdout.strip()
                if not state:
                    break
                time.sleep(0.05)
            self.assertEqual(state, "")

    def test_internal_refresh_once_is_synchronous_hidden_and_closes_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.sqlite3"
            ActivityStore(path).close()
            refreshed = threading.Event()

            class Refresher:
                def refresh(self):
                    refreshed.set()

            stdout, stderr = io.StringIO(), io.StringIO()
            code = main(
                ["__refresh-once", "--ledger", str(path)],
                stdout=stdout, stderr=stderr,
                refresher_factory=lambda _store: Refresher(),
            )
            self.assertEqual((code, stdout.getvalue(), stderr.getvalue()), (0, "", ""))
            self.assertTrue(refreshed.is_set())
        help_stdout = io.StringIO()
        with patch("sys.stdout", help_stdout), self.assertRaises(SystemExit):
            main(["--help"])
        self.assertNotIn("__refresh-once", help_stdout.getvalue())

    def test_fresh_failure_is_sanitized_and_does_not_leak_secret_or_path(self):
        refresher = FakeRefresher(error=RuntimeError("SECRET /Users/private/file"))
        code, out, err = self.run_cli(["status", "--format", "json", "--fresh"], refresher=refresher)
        self.assertEqual(code, 0)
        self.assertTrue(out)
        self.assertNotIn("SECRET", err)
        self.assertNotIn("/Users", err)

    def test_fresh_success_invokes_refresh_once(self):
        refresher = FakeRefresher()
        code, out, err = self.run_cli(
            ["status", "--format", "json", "--fresh"], refresher=refresher
        )
        self.assertEqual((code, err, refresher.calls), (0, "", 1))
        self.assertTrue(out)

    def test_fresh_timeout_dependency_is_positive_bounded_and_not_bool(self):
        for value in (True, 0, 301):
            with self.subTest(value=value):
                code, out, err = self.run_cli(
                    ["status", "--format", "json", "--fresh"],
                    refresher=FakeRefresher(), fresh_timeout=value,
                )
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertTrue(err)

    def test_unavailable_default_process_keeps_last_good_payload(self):
        def unavailable(_command, **_kwargs):
            raise RuntimeError("SECRET /private/path")

        code, out, err = self.run_cli(
            ["status", "--format", "json", "--fresh"],
            subprocess_runner=unavailable,
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["todayTokens"], 100)
        self.assertIn("refresh unavailable", err)
        self.assertNotIn("SECRET", err)

    def test_strict_returns_nonzero_after_valid_stale_payload(self):
        self.store.record_source_failure("minimax", "current.quota", "temporary", NOW)
        code, out, err = self.run_cli(["sources", "--format", "json", "--strict"], offline=True)
        self.assertNotEqual(code, 0)
        self.assertTrue(json.loads(out)["sources"])
        self.assertEqual(err, "")
        code, _, _ = self.run_cli(["sources", "--format", "json"], offline=True)
        self.assertEqual(code, 0)

    def test_strict_treats_elapsed_source_freshness_as_stale(self):
        self.store.record_source_success(
            "codex", "openusage.daily", datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc),
            freshness_seconds=300,
        )
        code, out, err = self.run_cli(
            ["sources", "--format", "json", "--strict"], offline=True
        )
        self.assertEqual(code, 3)
        self.assertTrue(out)
        self.assertEqual(err, "")

    def test_empty_ledger_status_and_doctor_strict_emit_payload_then_nonzero(self):
        empty = ActivityStore(":memory:")
        try:
            query = QueryService(empty, clock=lambda: NOW)
            for command in ("status", "doctor"):
                with self.subTest(command=command):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    code = main(
                        [command, "--format", "json", "--strict", "--offline"],
                        stdout=stdout, stderr=stderr, store=empty, query=query,
                    )
                    self.assertEqual(code, 3)
                    self.assertEqual(json.loads(stdout.getvalue())["schemaVersion"], "1.0")
                    self.assertEqual(stderr.getvalue(), "")
        finally:
            empty.close()

    def test_usage_only_and_quota_only_ledgers_are_not_empty_partial(self):
        usage_only = ActivityStore(":memory:")
        try:
            usage_only.replace_daily_usage(
                "codex", "2026-07-14",
                [DailyUsageRow(
                    day="2026-07-14", provider_id="codex", model_id="gpt",
                    input_tokens=1, output_tokens=0, cache_read_tokens=0,
                    cache_creation_tokens=0, reasoning_tokens=None, total_tokens=1,
                    cost_amount=None, cost_currency=None, cost_basis=None,
                    quality="direct", imported_at="2026-07-14T09:00:00Z",
                )],
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            code = main(
                ["status", "--format", "json", "--strict", "--offline"],
                stdout=stdout, stderr=stderr, store=usage_only,
                query=QueryService(usage_only, clock=lambda: NOW), clock=lambda: NOW,
            )
            self.assertEqual((code, stderr.getvalue()), (0, ""))
        finally:
            usage_only.close()

        quota_only = ActivityStore(":memory:")
        try:
            quota_only.record_quota(QuotaObservation(
                record_id="minimax.window", observed_at="2026-07-14T09:00:00Z",
                provider_id="minimax", quota_name="Window", unit="percent",
                used="20", quota_limit="100", remaining="80", remaining_ratio=0.8,
                resets_at=None, period_start=None, period_end=None, state="ok",
                quality="direct", stale=False,
            ))
            stdout, stderr = io.StringIO(), io.StringIO()
            code = main(
                ["status", "--format", "json", "--strict", "--offline"],
                stdout=stdout, stderr=stderr, store=quota_only,
                query=QueryService(quota_only, clock=lambda: NOW), clock=lambda: NOW,
            )
            self.assertEqual((code, stderr.getvalue()), (0, ""))
        finally:
            quota_only.close()

        source_only = ActivityStore(":memory:")
        try:
            source_only.record_source_success(
                "generic", "official.api", NOW, freshness_seconds=300
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            code = main(
                ["status", "--format", "json", "--strict", "--offline"],
                stdout=stdout, stderr=stderr, store=source_only,
                query=QueryService(source_only, clock=lambda: NOW), clock=lambda: NOW,
            )
            self.assertEqual((code, stderr.getvalue()), (0, ""))
        finally:
            source_only.close()

    def test_doctor_uses_effective_source_freshness(self):
        self.store.record_source_success(
            "codex", "expired", datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc),
            freshness_seconds=300,
        )
        code, out, _ = self.run_cli(["doctor", "--format", "json"], offline=True)
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["health"]["sources"]["ok"])

        future = seeded_store()
        try:
            stdout, stderr = io.StringIO(), io.StringIO()
            code = main(
                ["doctor", "--format", "json", "--offline"],
                stdout=stdout, stderr=stderr, store=future,
                query=QueryService(future, clock=lambda: NOW), clock=lambda: NOW,
            )
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(stdout.getvalue())["health"]["sources"]["ok"])
        finally:
            future.close()

    def test_invalid_input_has_no_business_payload_and_sanitized_error(self):
        code, out, err = self.run_cli(["usage", "--from", "bad", "--to", "2026-07-14", "--format", "jsonl"], offline=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(out, "")
        self.assertIn("invalid", err.lower())

    def test_main_does_not_close_injected_store(self):
        code, _, _ = self.run_cli(["status", "--format", "json"], offline=True)
        self.assertEqual(code, 0)
        self.assertGreaterEqual(self.store.current_change_seq, 1)

    def test_main_closes_only_store_created_by_factory(self):
        owned = seeded_store()
        closed = threading.Event()
        original_close = owned.close

        def close():
            closed.set()
            original_close()

        owned.close = close
        out, err = io.StringIO(), io.StringIO()
        code = main(["status", "--format", "json", "--offline"], stdout=out, stderr=err, store_factory=lambda: owned, clock=lambda: NOW)
        self.assertEqual(code, 0)
        self.assertTrue(closed.is_set())

    def test_daemon_immediate_repeat_clean_stop_and_non_overlap(self):
        refresher = FakeRefresher()
        class CatalogMonitor:
            calls = 0

            def maybe_run(self):
                self.calls += 1

        catalog_monitor = CatalogMonitor()
        stop = threading.Event()
        waits = 0

        def wait(seconds):
            nonlocal waits
            self.assertEqual(seconds, 60)
            waits += 1
            if waits == 2:
                stop.set()
            return stop.is_set()

        with tempfile.TemporaryDirectory() as directory:
            code, out, err = self.run_cli(
                ["daemon", "--interval", "60", "--api-socket", str(Path(directory) / "api.sock")],
                refresher=refresher, stop_event=stop, waiter=wait,
                catalog_monitor=catalog_monitor,
            )
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(refresher.calls, 2)
        self.assertEqual(refresher.max_active, 1)
        self.assertEqual(catalog_monitor.calls, 2)

    def test_daemon_rejects_zero_bool_and_too_small_interval(self):
        for interval in ("0", "1", "true"):
            code, out, err = self.run_cli(["daemon", "--interval", interval], refresher=FakeRefresher())
            self.assertNotEqual(code, 0)
            self.assertEqual(out, "")
            self.assertTrue(err)

    def test_entry_point_imports_no_ui_framework_and_help_works(self):
        source = Path("openusage_collector.py").read_text(encoding="utf-8")
        self.assertNotIn("AppKit", source)
        self.assertNotIn("openusage_bar.ui", source)
        with patch.dict("sys.modules", {"AppKit": None, "Security": None}):
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.assertRaises(SystemExit) as raised:
                main(["--help"], stdout=stdout, stderr=stderr)
            self.assertEqual(raised.exception.code, 0)

    def test_production_factory_loads_no_ui_module(self):
        from openusage_bar.aggregator import build_headless_refresher

        ui_modules = {"AppKit", "SwiftUI", "openusage_bar.ui"}
        already_loaded = ui_modules & sys.modules.keys()
        with patch("openusage_bar.config.ProviderConfigStore.load", return_value=[]):
            refresher = build_headless_refresher(self.store)
        self.assertIsNotNone(refresher)
        self.assertEqual(ui_modules & sys.modules.keys(), already_loaded)

    def test_doctor_has_stable_health_facts_without_secrets(self):
        code, out, _ = self.run_cli(["doctor", "--format", "json"], offline=True)
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(set(payload["health"]), {
            "schema", "ledger", "query", "sources", "openusageCatalog",
        })
        self.assertEqual(payload["health"]["openusageCatalog"], {
            "status": "not_checked", "expectedCount": 35, "actualCount": 0,
            "missingCount": 0, "extraCount": 0,
        })
        self.assertNotIn("token", out.lower())
        self.assertNotIn("credential", out.lower())

    def test_catalog_drift_is_reported_but_does_not_make_readable_data_partial(self):
        self.store.record_source_status(
            "openusage_catalog", "openusage.detect", "temporarily_unavailable",
            NOW, "provider_catalog_drift_e35_a36_m1_x2",
        )
        code, out, err = self.run_cli(
            ["doctor", "--format", "json", "--strict"], offline=True
        )
        payload = json.loads(out)
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(payload["health"]["sources"]["ok"])
        self.assertEqual(payload["health"]["openusageCatalog"], {
            "status": "provider_catalog_drift", "expectedCount": 35,
            "actualCount": 36, "missingCount": 1, "extraCount": 2,
        })

    def test_catalog_drift_alone_is_not_readable_evidence_for_strict_doctor(self):
        empty = ActivityStore(":memory:")
        empty.record_source_status(
            "openusage_catalog", "openusage.detect", "temporarily_unavailable",
            NOW, "provider_catalog_drift_e35_a36_m1_x2",
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            code = main(
                ["doctor", "--format", "json", "--strict", "--offline"],
                stdout=stdout, stderr=stderr, store=empty,
                query=QueryService(empty, clock=lambda: NOW), clock=lambda: NOW,
            )
        finally:
            empty.close()
        payload = json.loads(stdout.getvalue())
        self.assertEqual((code, stderr.getvalue()), (3, ""))
        self.assertTrue(payload["health"]["sources"]["ok"])
        self.assertEqual(
            payload["health"]["openusageCatalog"]["status"],
            "provider_catalog_drift",
        )


if __name__ == "__main__":
    unittest.main()
