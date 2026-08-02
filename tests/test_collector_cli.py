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
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

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
from openusage_bar.openusage_adapter import AUTO_TIMEOUT_SECONDS, DIRECT_TIMEOUT_SECONDS
from openusage_bar.query import QueryService, to_wire
from openusage_bar.routing_contract import RouteTarget
from openusage_bar.routing_execution import (
    ExecutionConnection,
    ExecutionConnectionStore,
)
from openusage_bar.routing_targets import RouteTargetStore


NOW = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)


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

    def test_daemon_serves_private_unix_api_and_cleans_socket_on_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "openusage.sock"
            router_path = Path(directory) / "router.sock"
            stop = threading.Event()
            result = []

            thread = threading.Thread(
                target=lambda: result.append(main(
                    [
                        "daemon", "--interval", "60",
                        "--api-socket", str(socket_path),
                        "--router-socket", str(router_path),
                    ],
                    stderr=io.StringIO(), store=self.store, query=self.query,
                    refresher=FakeRefresher(), stop_event=stop,
                ))
            )
            thread.start()
            deadline = time.monotonic() + 3
            while (
                (not socket_path.exists() or not router_path.exists())
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertTrue(socket_path.exists())
            self.assertEqual(socket_path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(router_path.exists())
            self.assertEqual(router_path.stat().st_mode & 0o777, 0o600)

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

            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(2)
            client.connect(str(router_path))
            client.sendall(b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            routing_response = b""
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                routing_response += chunk
            client.close()
            self.assertIn(b"HTTP/1.1 200", routing_response)
            self.assertIn(b'"targetCount":0', routing_response)

            stop.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [0])
            self.assertFalse(socket_path.exists())
            self.assertFalse(router_path.exists())

    def test_daemon_routes_only_through_installed_adapter_and_connection(self):
        self.store.record_quota(QuotaObservation(
            record_id="minimax.current", observed_at="2026-07-14T09:59:00Z",
            provider_id="minimax", quota_name="Five hour", unit="percent",
            used="20", quota_limit="100", remaining="80", remaining_ratio=0.8,
            resets_at="2026-07-14T12:00:00Z", period_start=None, period_end=None,
            state="ok", quality="direct", stale=False,
        ))
        self.store.record_source_success("minimax", "current.quota", NOW)
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "openusage.sock"
            router_path = Path(directory) / "router.sock"
            RouteTargetStore(router_path.with_name("route-targets.json")).save((
                RouteTarget(
                    target_id="minimax.work.minimax-m2",
                    provider_id="minimax",
                    account_ref="account-1",
                    model_id="minimax-m2",
                    connection_ref="conn_0123456789abcdef",
                    execution_class="openai_compatible",
                    execution_adapter_id="openai_compatible.direct",
                    resource_mode="quota",
                    fact_account_ref=None,
                    runtime_scope_ref=None,
                    balance_currency=None,
                    cost_currency=None,
                    input_cost_micros_per_million=None,
                    output_cost_micros_per_million=None,
                    enabled=True,
                    adapter_available=False,
                    regions=("global",),
                    privacy_class="direct_provider",
                    capabilities=("chat",),
                    context_window_tokens=128_000,
                    quality_tier=3,
                ),
            ), revision=1)
            ExecutionConnectionStore(
                router_path.with_name("execution-connections.json")
            ).save((ExecutionConnection(
                connection_ref="conn_0123456789abcdef",
                provider_id="minimax",
                account_ref="account-1",
                execution_class="openai_compatible",
                execution_adapter_id="openai_compatible.direct",
                base_url="https://api.minimax.chat/v1",
                enabled=True,
                models=("minimax-m2",),
            ),), revision=1)
            stop = threading.Event()
            result: list[int] = []
            thread = threading.Thread(target=lambda: result.append(main(
                [
                    "daemon", "--interval", "60",
                    "--api-socket", str(socket_path),
                    "--router-socket", str(router_path),
                ],
                stderr=io.StringIO(), store=self.store, query=self.query,
                refresher=FakeRefresher(), stop_event=stop, clock=lambda: NOW,
            )))
            thread.start()
            deadline = time.monotonic() + 3
            while not router_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(router_path.exists())

            payload = {
                "schemaVersion": "1.0",
                "clientRequestRef": "req_0123456789abcdef",
                "policyId": "reliable",
                "task": {
                    "kind": "chat", "requiredCapabilities": ["chat"],
                    "estimatedInputTokens": 1000, "maxOutputTokens": 1000,
                    "minimumContextWindowTokens": 2000,
                    "privacy": "direct_provider", "regions": ["global"],
                },
                "constraints": {
                    "allowProviders": [], "denyProviders": [],
                    "allowTargets": [], "denyTargets": [],
                    "maximumEstimatedCostMicrounits": None, "costCurrency": None,
                },
                "session": None,
            }
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(2)
            client.connect(str(router_path))
            client.sendall(
                b"POST /v1/simulations HTTP/1.1\r\nHost: localhost\r\n"
                + f"Content-Length: {len(encoded)}\r\n".encode("ascii")
                + b"Content-Type: application/json\r\nConnection: close\r\n\r\n"
                + encoded
            )
            response = b""
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                response += chunk
            client.close()
            self.assertIn(b"HTTP/1.1 200", response)
            self.assertIn(b'"targetId":"minimax.work.minimax-m2"', response)

            stop.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [0])

    def test_routing_start_failure_does_not_stop_collection_or_resource_api(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "openusage.sock"
            router_path = Path(directory) / "router.sock"
            router_path.write_text("occupied", encoding="utf-8")
            stop = threading.Event()
            result = []
            stderr = io.StringIO()
            thread = threading.Thread(
                target=lambda: result.append(main(
                    [
                        "daemon", "--interval", "60",
                        "--api-socket", str(socket_path),
                        "--router-socket", str(router_path),
                    ],
                    stderr=stderr,
                    store=self.store,
                    query=self.query,
                    refresher=FakeRefresher(),
                    stop_event=stop,
                ))
            )
            thread.start()
            deadline = time.monotonic() + 3
            while not socket_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(socket_path.exists())
            self.assertEqual(router_path.read_text(encoding="utf-8"), "occupied")

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

            stop.set()
            thread.join(3)
            self.assertEqual(result, [0])
            self.assertIn(
                "routing API unavailable; continuing without routing\n",
                stderr.getvalue(),
            )
            self.assertFalse(socket_path.exists())
            self.assertEqual(router_path.read_text(encoding="utf-8"), "occupied")

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
                # Line tracing can delay interpreter startup substantially on
                # slower macOS runners. Keep the timeout bounded while giving
                # the helper enough time to fork and publish its child PID.
                fresh_timeout=3,
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

    def test_daemon_local_api_exposes_default_runtime_summary_read_only(self):
        from openusage_bar.runtime_store import RuntimeStore
        from tests.test_local_api import unix_request
        from tests.test_runtime_store import observation

        stop = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_path = root / "runtime.sqlite3"
            socket_path = root / "api.sock"
            runtime = RuntimeStore(runtime_path, clock=lambda: NOW)
            try:
                runtime.ingest((observation(1, completed_at=NOW),))
            finally:
                runtime.close()
            responses = []

            def wait(_seconds):
                responses.append(
                    unix_request(
                        socket_path,
                        "/v1/runtime/summary?windowSeconds=3600",
                    )
                )
                stop.set()
                return True

            with patch(
                "openusage_bar.collector_cli.DEFAULT_RUNTIME_PATH", runtime_path
            ):
                code, out, err = self.run_cli(
                    [
                        "daemon", "--interval", "60",
                        "--api-socket", str(socket_path),
                    ],
                    refresher=FakeRefresher(),
                    stop_event=stop,
                    waiter=wait,
                )

        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(responses[0][0], 200)
        payload = json.loads(responses[0][2])
        self.assertEqual(payload["runtime"]["runtimeRevision"], 1)
        self.assertEqual(payload["runtime"]["tokens"]["total"], 20)

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


class RuntimeObservationCLITests(unittest.TestCase):
    def test_runtime_ingest_reads_stdin_without_opening_activity_store(self):
        from tests.test_runtime_observation import document

        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime.sqlite3"
            stdout, stderr = io.StringIO(), io.StringIO()
            activity_factory = Mock(side_effect=AssertionError("must not open"))

            code = main(
                ["runtime-ingest", "--database", str(runtime)],
                stdin=io.StringIO(document()),
                stdout=stdout,
                stderr=stderr,
                store_factory=activity_factory,
                clock=lambda: datetime(2026, 8, 1, 0, 1, tzinfo=timezone.utc),
            )

            self.assertEqual((code, stderr.getvalue()), (0, ""))
            self.assertEqual(json.loads(stdout.getvalue()), {
                "acceptedCount": 1,
                "duplicateCount": 0,
                "expiredCount": 0,
                "prunedCount": 0,
                "runtimeRevision": 1,
                "schemaVersion": 1,
            })
            self.assertNotIn("openai", stdout.getvalue())
            self.assertNotIn("gpt-5", stdout.getvalue())
            self.assertTrue(runtime.is_file())
            activity_factory.assert_not_called()
            self.assertFalse((Path(directory) / "activity.sqlite3").exists())

    def test_runtime_ingest_retry_is_idempotent(self):
        from tests.test_runtime_observation import document

        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime.sqlite3"
            for expected in (
                {"acceptedCount": 1, "duplicateCount": 0, "runtimeRevision": 1},
                {"acceptedCount": 0, "duplicateCount": 1, "runtimeRevision": 1},
            ):
                stdout = io.StringIO()
                code = main(
                    ["runtime-ingest", "--database", str(runtime)],
                    stdin=io.StringIO(document()),
                    stdout=stdout,
                    stderr=io.StringIO(),
                    clock=lambda: datetime(2026, 8, 1, 0, 1, tzinfo=timezone.utc),
                )
                payload = json.loads(stdout.getvalue())
                self.assertEqual(code, 0)
                for key, value in expected.items():
                    self.assertEqual(payload[key], value)

    def test_runtime_summary_is_stable_bounded_json(self):
        from tests.test_runtime_observation import document

        current = datetime(2026, 8, 1, 0, 1, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime.sqlite3"
            self.assertEqual(main(
                ["runtime-ingest", "--database", str(runtime)],
                stdin=io.StringIO(document()),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                clock=lambda: current,
            ), 0)
            stdout, stderr = io.StringIO(), io.StringIO()
            activity_factory = Mock(side_effect=AssertionError("must not open"))

            code = main(
                [
                    "runtime-summary", "--database", str(runtime),
                    "--window-seconds", "3600",
                ],
                stdin=io.StringIO("must-not-read"),
                stdout=stdout,
                stderr=stderr,
                store_factory=activity_factory,
                clock=lambda: current,
            )

            payload = json.loads(stdout.getvalue())
            self.assertEqual((code, stderr.getvalue()), (0, ""))
            self.assertEqual(payload["schemaVersion"], 1)
            self.assertEqual(payload["runtimeRevision"], 1)
            self.assertEqual(payload["coverage"], {
                "state": "complete", "omittedGroupCount": 0,
            })
            self.assertEqual(payload["observationCount"], 1)
            self.assertEqual(payload["tokens"]["total"], 20)
            self.assertEqual(payload["statusCounts"], {"completed": 1})
            self.assertEqual(payload["costs"], [{"currency": "usd", "micros": 25}])
            self.assertEqual(payload["latency"]["durationP95Ms"], 3000)
            self.assertEqual(payload["groups"][0]["providerId"], "openai")
            self.assertEqual(payload["groups"][0]["modelId"], "gpt-5")
            self.assertEqual(
                payload["groups"][0]["scopeRef"], "anon_0123456789abcdef"
            )
            self.assertNotIn("sourceId", stdout.getvalue())
            self.assertNotIn("requestId", stdout.getvalue())
            activity_factory.assert_not_called()

    def test_invalid_or_private_runtime_input_is_sanitized_and_not_persisted(self):
        from openusage_bar.runtime_observation import MAX_DOCUMENT_BYTES
        from tests.test_runtime_observation import document, valid_observation

        private = valid_observation()
        private["prompt"] = "do not echo this private value"
        payloads = (
            document([private]),
            "{" + " " * MAX_DOCUMENT_BYTES + "}",
        )
        for payload in payloads:
            with self.subTest(size=len(payload)), tempfile.TemporaryDirectory() as directory:
                runtime = Path(directory) / "runtime.sqlite3"
                stdout, stderr = io.StringIO(), io.StringIO()
                code = main(
                    ["runtime-ingest", "--database", str(runtime)],
                    stdin=io.StringIO(payload),
                    stdout=stdout,
                    stderr=stderr,
                    clock=lambda: datetime(2026, 8, 1, 0, 1, tzinfo=timezone.utc),
                )
                self.assertEqual(code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "invalid runtime input\n")
                self.assertNotIn("private", stderr.getvalue())
                self.assertFalse(runtime.exists())

    def test_runtime_paths_and_windows_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.sqlite3"
            target.touch()
            symlink = root / "runtime.sqlite3"
            symlink.symlink_to(target)
            for arguments in (
                ["runtime-summary", "--database", "relative.sqlite3", "--window-seconds", "60"],
                ["runtime-summary", "--database", str(symlink), "--window-seconds", "60"],
                ["runtime-summary", "--database", str(root / "valid.sqlite3"), "--window-seconds", "59"],
                ["runtime-summary", "--database", str(root / "valid.sqlite3"), "--window-seconds", "86401"],
            ):
                with self.subTest(arguments=arguments):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    code = main(arguments, stdout=stdout, stderr=stderr)
                    self.assertEqual(code, 2)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertEqual(stderr.getvalue(), "invalid command input\n")


if __name__ == "__main__":
    unittest.main()
