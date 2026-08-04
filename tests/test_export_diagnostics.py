from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timezone
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
        "balances": [
            {
                "accountRef": "personal-account",
                "providerId": "private-instance",
                "sourceId": "private-balance-source",
                "state": "ok",
                "quality": "direct",
                "stale": False,
                "available": "secret-balance-is-never-copied",
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
                        "factFamilies": [
                            "detection",
                            "subscription_capacity",
                        ],
                        "authority": "provider_official",
                        "accountScope": "configured_account",
                        "modelScope": "mixed",
                        "verification": "live_account",
                    }
                ],
            }
        ],
    }


def activity() -> dict[str, object]:
    return {
        "schemaVersion": "1.0",
        "dataRevision": 19,
        "generatedAt": "2026-07-18T00:00:00Z",
        "rows": [
            {
                "day": "2026-07-17",
                "providerId": "codex-local",
                "accountRef": "work-account",
                "modelId": "gpt-5.6-sol",
                "inputTokens": 100,
                "outputTokens": 20,
                "cacheReadTokens": 80,
                "cacheCreationTokens": 0,
                "reasoningTokens": 5,
                "totalTokens": 120,
                "tokenCountingConvention": "input_includes_cache",
                "costAmount": None,
                "costCurrency": None,
                "costBasis": None,
                "quality": "fallback",
                "importedAt": "2026-07-17T23:55:00Z",
                "revision": 2,
                "recordId": "daily.codex-local.work-account.2026-07-17.gpt-5.6-sol",
                "sourceId": "openusage.daily",
            }
        ],
        "coverage": [
            {
                "day": "2026-07-17",
                "providerId": "codex-local",
                "accountRef": "work-account",
                "covered": True,
                "sourceId": "openusage.daily",
            },
            {
                "day": "2026-07-18",
                "providerId": "codex-local",
                "accountRef": "work-account",
                "covered": False,
                "sourceId": None,
            },
        ],
    }


def source_status() -> dict[str, object]:
    return {
        "schemaVersion": "1.0",
        "dataRevision": 19,
        "generatedAt": "2026-07-18T00:00:00Z",
        "sources": [
            {
                "providerId": "codex-local",
                "sourceId": "openusage.daily",
                "state": "stale",
                "lastAttemptAt": "2026-07-18T00:00:00Z",
                "lastSuccessAt": "2026-07-17T23:55:00Z",
                "staleAt": "2026-07-18T00:00:00Z",
                "errorCode": "empty_result",
            },
            {
                "providerId": "minimax-primary",
                "sourceId": "minimax.billing",
                "state": "temporarily_unavailable",
                "lastAttemptAt": "2026-07-18T00:00:00Z",
                "lastSuccessAt": None,
                "staleAt": None,
                "errorCode": "auth_required",
            },
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


class ReconciliationServer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.ready = threading.Event()
        self.requests: list[str] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        responses = {
            "/v1/snapshot": snapshot(),
            "/v1/capabilities": capabilities(),
            "/v1/activity/daily?from=2026-07-17&to=2026-07-18": activity(),
            "/v1/sources/status": source_status(),
        }
        try:
            server.bind(str(self.path))
            server.listen(4)
            self.ready.set()
            for _ in range(4):
                client, _ = server.accept()
                with client:
                    request = client.recv(4096).decode("ascii", "replace")
                    route = request.split(" ", 2)[1]
                    self.requests.append(route)
                    payload = responses[route]
                    body = json.dumps(payload).encode()
                    client.sendall(
                        f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n"
                        "Connection: close\r\n\r\n".encode()
                        + body
                    )
        finally:
            server.close()

    def __enter__(self) -> "ReconciliationServer":
        self.thread.start()
        self.ready.wait(2)
        return self

    def __exit__(self, *_: object) -> None:
        self.thread.join(2)


class DriftingReconciliationServer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.ready = threading.Event()
        self.requests: list[str] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        route_payloads = {
            "/v1/snapshot": snapshot,
            "/v1/capabilities": capabilities,
            "/v1/activity/daily?from=2026-07-17&to=2026-07-18": activity,
            "/v1/sources/status": source_status,
        }
        try:
            server.bind(str(self.path))
            server.listen(8)
            self.ready.set()
            for request_index in range(8):
                client, _ = server.accept()
                with client:
                    request = client.recv(4096).decode("ascii", "replace")
                    route = request.split(" ", 2)[1]
                    self.requests.append(route)
                    payload = route_payloads[route]()
                    if request_index == 3:
                        payload["dataRevision"] = 20
                    body = json.dumps(payload).encode()
                    client.sendall(
                        f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n"
                        "Connection: close\r\n\r\n".encode()
                        + body
                    )
        finally:
            server.close()

    def __enter__(self) -> "DriftingReconciliationServer":
        self.thread.start()
        self.ready.wait(2)
        return self

    def __exit__(self, *_: object) -> None:
        self.thread.join(2)


@unittest.skipUnless(sys.platform == "darwin", "macOS build-venv diagnostics test")
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
        self.assertEqual(payload["aggregates"]["balanceCount"], 1)
        self.assertEqual(
            payload["aggregates"]["balanceStates"],
            {"ok": 1},
        )
        self.assertEqual(
            payload["aggregates"]["balanceQuality"],
            {"direct": 1},
        )
        self.assertEqual(payload["aggregates"]["staleBalanceCount"], 0)
        self.assertEqual(payload["aggregates"]["sourceStates"], {"error": 1, "live": 1})
        self.assertEqual(payload["aggregates"]["sourceErrorCodes"], {"AUTH_FAILED": 1})
        self.assertEqual(payload["capabilityDeclarations"][0]["familyId"], "minimax")
        self.assertEqual(
            payload["capabilityDeclarations"][0]["sources"],
            [{
                "accountScope": "configured_account",
                "authority": "provider_official",
                "factFamilies": ["detection", "subscription_capacity"],
                "kind": "provider_api",
                "modelScope": "mixed",
                "provenance": "provider_official",
                "stability": "stable",
                "verification": "live_account",
            }],
        )
        encoded = json.dumps(payload, sort_keys=True)
        for forbidden in (
            "Alice private account", "personal-account", "private-instance",
            "private-source", "private-balance-source", "keychain:private-label",
            "secret-value", "secret-balance",
            "accountRef", "displayName", "credentialSource", "sourceId",
            "payloadJson", "cookie", "prompt", "response",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_v1_preserves_unknown_today_tokens_instead_of_inventing_zero(self) -> None:
        missing = snapshot()
        missing["summary"] = {
            "todayTokens": None,
            "modelCount": 0,
            "coveredDayCount": 0,
        }
        payload = self.module.build_diagnostics(
            missing,
            capabilities(),
            product={"version": "0.4.0", "build": "4"},
            runtime={"macOS": "26.0", "architecture": "arm64"},
        )

        self.assertIsNone(payload["aggregates"]["todayTokens"])

    def test_v1_accepts_an_n_minus_one_snapshot_without_balances(self) -> None:
        previous = snapshot()
        previous.pop("balances")

        payload = self.module.build_diagnostics(
            previous,
            capabilities(),
            product={"version": "0.4.0", "build": "4"},
            runtime={"macOS": "26.0", "architecture": "arm64"},
        )

        self.assertEqual(payload["aggregates"]["balanceCount"], 0)
        self.assertEqual(payload["aggregates"]["balanceQuality"], {})
        self.assertEqual(payload["aggregates"]["balanceStates"], {})
        self.assertEqual(payload["aggregates"]["staleBalanceCount"], 0)

    def test_v1_accepts_n_minus_one_capability_sources_without_evidence(self) -> None:
        previous = capabilities()
        source = previous["providers"][0]["sources"][0]
        for field in (
            "accountScope",
            "authority",
            "factFamilies",
            "modelScope",
            "verification",
        ):
            source.pop(field)

        payload = self.module.build_diagnostics(
            snapshot(),
            previous,
            product={"version": "0.4.4", "build": "8"},
            runtime={"macOS": "26.0", "architecture": "arm64"},
        )

        self.assertEqual(
            payload["capabilityDeclarations"][0]["sources"],
            [{
                "accountScope": "unknown",
                "authority": "unknown",
                "factFamilies": [],
                "kind": "provider_api",
                "modelScope": "unknown",
                "provenance": "provider_official",
                "stability": "stable",
                "verification": "unverified",
            }],
        )

    def test_v1_rejects_malformed_balance_and_capability_evidence(self) -> None:
        invalid_balance = snapshot()
        invalid_balance["balances"][0]["stale"] = "false"
        with self.assertRaisesRegex(ValueError, "balance stale state"):
            self.module.build_diagnostics(
                invalid_balance,
                capabilities(),
                product={"version": "0.4.0", "build": "4"},
                runtime={"macOS": "26.0", "architecture": "arm64"},
            )

        invalid_capability = capabilities()
        del invalid_capability["providers"][0]["sources"][0]["verification"]
        with self.assertRaisesRegex(ValueError, "source verification"):
            self.module.build_diagnostics(
                snapshot(),
                invalid_capability,
                product={"version": "0.4.0", "build": "4"},
                runtime={"macOS": "26.0", "architecture": "arm64"},
            )

    def test_v1_rejects_numeric_zero_without_usage_or_coverage(self) -> None:
        invalid = snapshot()
        invalid["summary"] = {
            "todayTokens": 0,
            "modelCount": 0,
            "coveredDayCount": 0,
        }

        with self.assertRaisesRegex(ValueError, "numeric today tokens"):
            self.module.build_diagnostics(
                invalid,
                capabilities(),
                product={"version": "0.4.0", "build": "4"},
                runtime={"macOS": "26.0", "architecture": "arm64"},
            )

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

    def test_builds_v2_daily_reconciliation_from_allowlisted_local_api_facts(self) -> None:
        payload = self.module.build_reconciliation_diagnostics(
            snapshot(),
            capabilities(),
            activity(),
            source_status(),
            from_day=date(2026, 7, 17),
            to_day=date(2026, 7, 18),
            local_timezone="Asia/Singapore",
            product={"version": "0.4.0", "build": "4"},
            runtime={"macOS": "26.0", "architecture": "arm64"},
            clock=lambda: datetime(2026, 7, 18, tzinfo=timezone.utc),
        )

        self.assertEqual(payload["schemaVersion"], "openusage-diagnostics-2")
        self.assertEqual(payload["range"], {
            "from": "2026-07-17",
            "to": "2026-07-18",
            "timezone": "Asia/Singapore",
        })
        self.assertEqual(payload["localAPI"]["dataRevision"], 19)
        self.assertEqual(payload["dailyUsage"], [{
            "day": "2026-07-17",
            "providerId": "codex-local",
            "accountRef": "account-1",
            "modelId": "gpt-5.6-sol",
            "totalTokens": 120,
            "inputTokens": 100,
            "outputTokens": 20,
            "cacheReadTokens": 80,
            "cacheCreationTokens": 0,
            "reasoningTokens": 5,
            "tokenCountingConvention": "input_includes_cache",
            "sourceId": "openusage.daily",
            "quality": "fallback",
            "coverage": "covered",
            "importedAt": "2026-07-17T23:55:00Z",
            "completeness": "complete",
            "reconciliation": {
                "status": "matched",
                "expectedTotalTokens": 120,
                "deltaTokens": 0,
            },
            "accountTotalComparison": {
                "status": "not_comparable",
                "coverageScope": "local_collector",
                "reason": "local_collector_is_not_account_total",
                "limitations": [
                    "deleted_or_unavailable_sessions",
                    "other_devices",
                    "web_or_mobile",
                ],
            },
        }])
        self.assertEqual(payload["observability"], {
            "accountReferences": "per_export_pseudonyms",
            "sourceSelectionHistory": "not_observable",
        })
        self.assertEqual(
            sorted(issue["code"] for issue in payload["issues"]),
            ["coverage_gap", "last_good_retained", "source_error", "source_stale"],
        )
        self.assertEqual(payload["aggregates"]["tokenTotals"], {
            "totalTokens": None,
            "inputTokens": None,
            "outputTokens": None,
            "cacheReadTokens": None,
            "cacheCreationTokens": None,
            "reasoningTokens": None,
        })
        self.assertEqual(payload["aggregates"]["observedTokenTotals"], {
            "totalTokens": 120,
            "inputTokens": 100,
            "outputTokens": 20,
            "cacheReadTokens": 80,
            "cacheCreationTokens": 0,
            "reasoningTokens": 5,
        })
        self.assertEqual(payload["aggregates"]["aggregateCompleteness"], "partial")
        self.assertEqual(payload["aggregates"]["issueCounts"], {
            "coverage_gap": 1,
            "last_good_retained": 1,
            "source_error": 1,
            "source_stale": 1,
        })
        self.assertEqual(payload["aggregates"]["countingConventionCounts"], {
            "input_includes_cache": 1,
        })
        self.assertEqual(payload["aggregates"]["qualityCounts"], {
            "fallback": 1,
        })
        self.assertEqual(payload["aggregates"]["completenessCounts"], {
            "complete": 1,
        })
        self.assertEqual(payload["aggregates"]["reconciliationStatusCounts"], {
            "matched": 1,
        })
        self.assertEqual(
            payload["aggregates"]["accountTotalComparisonStatusCounts"],
            {"not_comparable": 1},
        )
        encoded = json.dumps(payload, sort_keys=True)
        for forbidden in (
            "Alice private account", "keychain:private-label", "secret-value",
            "displayName", "credentialSource", "payloadJson", "prompt", "response",
            "work-account",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_v2_marks_codex_local_sessions_as_not_account_total_comparable(self) -> None:
        rows = activity()
        rows["rows"][0]["sourceId"] = "codex.local_sessions"
        rows["coverage"][0]["sourceId"] = "codex.local_sessions"

        payload = self.module.build_reconciliation_diagnostics(
            snapshot(), capabilities(), rows, source_status(),
            from_day=date(2026, 7, 17),
            to_day=date(2026, 7, 18),
            local_timezone="Asia/Singapore",
            product={"version": "0.4.0", "build": "4"},
            runtime={"macOS": "26.0", "architecture": "arm64"},
        )

        self.assertEqual(
            payload["dailyUsage"][0]["accountTotalComparison"],
            {
                "status": "not_comparable",
                "coverageScope": "local_device_sessions",
                "reason": "local_sessions_are_partial_account_coverage",
                "limitations": [
                    "deleted_or_unavailable_sessions",
                    "other_devices",
                    "web_or_mobile",
                ],
            },
        )

    def test_v2_preserves_source_total_and_reports_known_convention_delta(self) -> None:
        rows = activity()
        rows["rows"][0]["totalTokens"] = 125

        payload = self.module.build_reconciliation_diagnostics(
            snapshot(), capabilities(), rows, source_status(),
            from_day=date(2026, 7, 17),
            to_day=date(2026, 7, 18),
            local_timezone="Asia/Singapore",
            product={"version": "0.4.0", "build": "4"},
            runtime={"macOS": "26.0", "architecture": "arm64"},
        )

        row = payload["dailyUsage"][0]
        self.assertEqual(row["totalTokens"], 125)
        self.assertEqual(row["reconciliation"], {
            "status": "mismatch",
            "expectedTotalTokens": 120,
            "deltaTokens": 5,
        })
        self.assertIsNone(payload["aggregates"]["tokenTotals"]["totalTokens"])
        self.assertEqual(
            payload["aggregates"]["observedTokenTotals"]["totalTokens"], 125
        )
        self.assertEqual(payload["aggregates"]["reconciliationStatusCounts"], {
            "mismatch": 1,
        })

    def test_v2_compares_disjoint_components_only_when_reasoning_is_known(self) -> None:
        rows = activity()
        rows["rows"][0]["tokenCountingConvention"] = "components_disjoint"
        rows["rows"][0]["totalTokens"] = 205

        payload = self.module.build_reconciliation_diagnostics(
            snapshot(), capabilities(), rows, source_status(),
            from_day=date(2026, 7, 17),
            to_day=date(2026, 7, 18),
            local_timezone="Asia/Singapore",
            product={"version": "0.4.0", "build": "4"},
            runtime={"macOS": "26.0", "architecture": "arm64"},
        )

        self.assertEqual(payload["dailyUsage"][0]["reconciliation"], {
            "status": "matched",
            "expectedTotalTokens": 205,
            "deltaTokens": 0,
        })

    def test_v2_exposes_complete_totals_only_for_complete_coverage(self) -> None:
        rows = activity()
        rows["coverage"] = [rows["coverage"][0]]

        payload = self.module.build_reconciliation_diagnostics(
            snapshot(), capabilities(), rows, source_status(),
            from_day=date(2026, 7, 17),
            to_day=date(2026, 7, 18),
            local_timezone="Asia/Singapore",
            product={"version": "0.4.0", "build": "4"},
            runtime={"macOS": "26.0", "architecture": "arm64"},
        )

        expected = {
            "totalTokens": 120,
            "inputTokens": 100,
            "outputTokens": 20,
            "cacheReadTokens": 80,
            "cacheCreationTokens": 0,
            "reasoningTokens": 5,
        }
        self.assertEqual(payload["aggregates"]["aggregateCompleteness"], "complete")
        self.assertEqual(payload["aggregates"]["tokenTotals"], expected)
        self.assertEqual(payload["aggregates"]["observedTokenTotals"], expected)

    def test_v2_distinguishes_missing_from_covered_zero_aggregates(self) -> None:
        missing = activity()
        missing["rows"] = []
        missing["coverage"] = [{
            **missing["coverage"][0],
            "covered": False,
            "sourceId": None,
        }]
        missing_payload = self.module.build_reconciliation_diagnostics(
            snapshot(), capabilities(), missing, source_status(),
            from_day=date(2026, 7, 17),
            to_day=date(2026, 7, 18),
            local_timezone="Asia/Singapore",
            product={"version": "0.4.0", "build": "4"},
            runtime={"macOS": "26.0", "architecture": "arm64"},
        )
        null_totals = {
            "totalTokens": None,
            "inputTokens": None,
            "outputTokens": None,
            "cacheReadTokens": None,
            "cacheCreationTokens": None,
            "reasoningTokens": None,
        }
        self.assertEqual(
            missing_payload["aggregates"]["aggregateCompleteness"], "missing"
        )
        self.assertEqual(missing_payload["aggregates"]["tokenTotals"], null_totals)
        self.assertEqual(
            missing_payload["aggregates"]["observedTokenTotals"], null_totals
        )

        covered_zero = activity()
        covered_zero["rows"] = []
        covered_zero["coverage"] = [covered_zero["coverage"][0]]
        covered_zero_payload = self.module.build_reconciliation_diagnostics(
            snapshot(), capabilities(), covered_zero, source_status(),
            from_day=date(2026, 7, 17),
            to_day=date(2026, 7, 18),
            local_timezone="Asia/Singapore",
            product={"version": "0.4.0", "build": "4"},
            runtime={"macOS": "26.0", "architecture": "arm64"},
        )
        zero_totals = {key: 0 for key in null_totals}
        self.assertEqual(
            covered_zero_payload["aggregates"]["aggregateCompleteness"],
            "covered_zero",
        )
        self.assertEqual(
            covered_zero_payload["aggregates"]["tokenTotals"], zero_totals
        )
        self.assertEqual(
            covered_zero_payload["aggregates"]["observedTokenTotals"], zero_totals
        )

    def test_v2_pseudonymizes_accounts_per_export_before_emitting_rows(self) -> None:
        rows = activity()
        rows["coverage"] = [rows["coverage"][0]]
        rows["coverage"].append({
            **rows["coverage"][0],
            "accountRef": "personal-account",
        })
        rows["rows"].append({
            **rows["rows"][0],
            "accountRef": "personal-account",
            "recordId": "personal-account-row",
        })
        rows["coverage"].append({
            **rows["coverage"][0],
            "accountRef": None,
        })
        rows["rows"].append({
            **rows["rows"][0],
            "accountRef": None,
            "recordId": "accountless-row",
        })

        payload = self.module.build_reconciliation_diagnostics(
            snapshot(), capabilities(), rows, source_status(),
            from_day=date(2026, 7, 17),
            to_day=date(2026, 7, 18),
            local_timezone="Asia/Singapore",
            product={"version": "0.4.0", "build": "4"},
            runtime={"macOS": "26.0", "architecture": "arm64"},
        )

        self.assertEqual(
            {row["accountRef"] for row in payload["dailyUsage"]},
            {"account-1", "account-2", None},
        )
        self.assertNotIn("duplicate_effective_row", payload["aggregates"]["issueCounts"])
        encoded = json.dumps(payload, sort_keys=True)
        self.assertNotIn("personal-account", encoded)
        self.assertNotIn("work-account", encoded)
        self.assertEqual(payload["observability"]["accountReferences"], (
            "per_export_pseudonyms"
        ))

    def test_v2_marks_non_comparable_and_incomplete_rows_without_assuming_zero(self) -> None:
        rows = activity()
        base = rows["rows"][0]
        base["tokenCountingConvention"] = "unknown"
        rows["rows"].append({
            **base,
            "modelId": "provider-total",
            "recordId": "daily.codex-local.work-account.2026-07-17.provider-total",
            "tokenCountingConvention": "provider_reported",
            "quality": "live",
        })
        rows["rows"].append({
            **base,
            "modelId": "disjoint-without-reasoning",
            "recordId": (
                "daily.codex-local.work-account.2026-07-17."
                "disjoint-without-reasoning"
            ),
            "tokenCountingConvention": "components_disjoint",
            "reasoningTokens": None,
            "quality": "partial",
        })

        payload = self.module.build_reconciliation_diagnostics(
            snapshot(), capabilities(), rows, source_status(),
            from_day=date(2026, 7, 17),
            to_day=date(2026, 7, 18),
            local_timezone="Asia/Singapore",
            product={"version": "0.4.0", "build": "4"},
            runtime={"macOS": "26.0", "architecture": "arm64"},
        )

        reconciliations = {
            row["modelId"]: row["reconciliation"] for row in payload["dailyUsage"]
        }
        self.assertEqual(reconciliations["gpt-5.6-sol"], {
            "status": "not_comparable",
            "reason": "unknown_counting_convention",
        })
        self.assertEqual(reconciliations["provider-total"], {
            "status": "not_comparable",
            "reason": "provider_reported_total",
        })
        self.assertEqual(reconciliations["disjoint-without-reasoning"], {
            "status": "incomplete",
            "reason": "reasoning_tokens_missing",
        })
        self.assertEqual(payload["aggregates"]["countingConventionCounts"], {
            "components_disjoint": 1,
            "provider_reported": 1,
            "unknown": 1,
        })
        self.assertEqual(payload["aggregates"]["qualityCounts"], {
            "fallback": 1,
            "live": 1,
            "partial": 1,
        })
        self.assertEqual(payload["aggregates"]["completenessCounts"], {
            "complete": 2,
            "partial": 1,
        })
        self.assertEqual(payload["aggregates"]["reconciliationStatusCounts"], {
            "incomplete": 1,
            "not_comparable": 2,
        })

    def test_v2_reports_only_actual_duplicate_effective_rows(self) -> None:
        rows = activity()
        rows["rows"].append({
            **rows["rows"][0],
            "recordId": "duplicate-effective-record",
            "importedAt": "2026-07-18T00:00:00Z",
        })

        payload = self.module.build_reconciliation_diagnostics(
            snapshot(), capabilities(), rows, source_status(),
            from_day=date(2026, 7, 17),
            to_day=date(2026, 7, 18),
            local_timezone="Asia/Singapore",
            product={"version": "0.4.0", "build": "4"},
            runtime={"macOS": "26.0", "architecture": "arm64"},
        )

        duplicate = next(
            issue for issue in payload["issues"]
            if issue["code"] == "duplicate_effective_row"
        )
        self.assertEqual(duplicate["rowCount"], 2)
        self.assertEqual(duplicate["sourceIds"], ["openusage.daily"])
        self.assertNotIn(
            "duplicate_source_candidate",
            payload["aggregates"]["issueCounts"],
        )

    def test_v2_reports_different_source_duplicate_candidate_from_actual_rows(self) -> None:
        rows = activity()
        rows["rows"].append({
            **rows["rows"][0],
            "recordId": "different-source-effective-record",
            "sourceId": "provider.official",
            "importedAt": "2026-07-18T00:00:00Z",
        })

        payload = self.module.build_reconciliation_diagnostics(
            snapshot(), capabilities(), rows, source_status(),
            from_day=date(2026, 7, 17),
            to_day=date(2026, 7, 18),
            local_timezone="Asia/Singapore",
            product={"version": "0.4.0", "build": "4"},
            runtime={"macOS": "26.0", "architecture": "arm64"},
        )

        candidate = next(
            issue for issue in payload["issues"]
            if issue["code"] == "duplicate_source_candidate"
        )
        self.assertEqual(candidate["rowCount"], 2)
        self.assertEqual(
            candidate["sourceIds"],
            ["openusage.daily", "provider.official"],
        )
        self.assertEqual(payload["aggregates"]["issueCounts"], {
            "coverage_gap": 1,
            "duplicate_effective_row": 1,
            "duplicate_source_candidate": 1,
            "last_good_retained": 1,
            "source_error": 1,
            "source_stale": 1,
        })

    def test_v2_does_not_infer_duplicate_or_coverage_gap_from_source_status(self) -> None:
        rows = activity()
        rows["coverage"] = [rows["coverage"][0]]
        statuses = source_status()
        statuses["sources"].append({
            "providerId": "codex-local",
            "sourceId": "provider.official",
            "state": "ok",
            "lastAttemptAt": "2026-07-18T00:00:00Z",
            "lastSuccessAt": "2026-07-18T00:00:00Z",
            "staleAt": None,
            "errorCode": None,
        })

        payload = self.module.build_reconciliation_diagnostics(
            snapshot(), capabilities(), rows, statuses,
            from_day=date(2026, 7, 17),
            to_day=date(2026, 7, 18),
            local_timezone="Asia/Singapore",
            product={"version": "0.4.0", "build": "4"},
            runtime={"macOS": "26.0", "architecture": "arm64"},
        )

        self.assertEqual(payload["observability"], {
            "accountReferences": "per_export_pseudonyms",
            "sourceSelectionHistory": "not_observable",
        })
        self.assertEqual(payload["aggregates"]["coverageGapCount"], 0)
        self.assertNotIn("coverage_gap", payload["aggregates"]["issueCounts"])
        self.assertNotIn(
            "duplicate_effective_row", payload["aggregates"]["issueCounts"]
        )
        self.assertNotIn(
            "duplicate_source_candidate", payload["aggregates"]["issueCounts"]
        )

    def test_v2_rejects_unbounded_ranges_revision_drift_and_unstable_identity(self) -> None:
        with self.assertRaises(ValueError):
            self.module.build_reconciliation_diagnostics(
                snapshot(), capabilities(), activity(), source_status(),
                from_day=date(2024, 7, 17),
                to_day=date(2026, 7, 18),
                local_timezone="Asia/Singapore",
                product={"version": "0.4.0", "build": "4"},
                runtime={"macOS": "26.0", "architecture": "arm64"},
            )

    def test_v2_normalizes_known_errors_without_echoing_unknown_identifiers(self) -> None:
        statuses = source_status()
        statuses["sources"][1]["errorCode"] = "private_account_marker"
        payload = self.module.build_reconciliation_diagnostics(
            snapshot(), capabilities(), activity(), statuses,
            from_day=date(2026, 7, 17),
            to_day=date(2026, 7, 18),
            local_timezone="Asia/Singapore",
            product={"version": "0.4.0", "build": "4"},
            runtime={"macOS": "26.0", "architecture": "arm64"},
        )

        source_error = next(
            issue for issue in payload["issues"] if issue["code"] == "source_error"
        )
        self.assertEqual(source_error["errorCode"], "UNCLASSIFIED")
        self.assertNotIn("private_account_marker", json.dumps(payload))

        drifted = source_status()
        drifted["dataRevision"] = 20
        with self.assertRaises(ValueError):
            self.module.build_reconciliation_diagnostics(
                snapshot(), capabilities(), activity(), drifted,
                from_day=date(2026, 7, 17),
                to_day=date(2026, 7, 18),
                local_timezone="Asia/Singapore",
                product={"version": "0.4.0", "build": "4"},
                runtime={"macOS": "26.0", "architecture": "arm64"},
            )

        unsafe = activity()
        unsafe["rows"][0]["accountRef"] = "alice@example.com"
        with self.assertRaises(ValueError):
            self.module.build_reconciliation_diagnostics(
                snapshot(), capabilities(), unsafe, source_status(),
                from_day=date(2026, 7, 17),
                to_day=date(2026, 7, 18),
                local_timezone="Asia/Singapore",
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

    def test_cli_defaults_to_v1_and_writes_from_two_read_only_routes(self) -> None:
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
            self.assertEqual(exported["schemaVersion"], "openusage-diagnostics-1")
            self.assertEqual(exported["product"], {"build": "4", "version": "0.4.0"})
            self.assertNotIn(str(Path.home()), output.read_text(encoding="utf-8"))
            privacy = subprocess.run(
                [str(ROOT / ".build-venv/bin/python"), str(ROOT / "scripts/privacy_scan.py"), str(output)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(privacy.returncode, 0, privacy.stderr)

    def test_cli_explicit_v2_reads_the_four_declared_read_only_routes(self) -> None:
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
            output = root / "diagnostics-v2.json"
            with ReconciliationServer(socket_path) as server:
                result = subprocess.run(
                    [
                        str(SCRIPT), "--socket", str(socket_path),
                        "--app", str(app), "--output", str(output),
                        "--schema-version", "2",
                        "--from", "2026-07-17", "--to", "2026-07-18",
                        "--timezone", "Asia/Singapore",
                    ],
                    capture_output=True,
                    text=True,
                )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(server.requests, [
                "/v1/snapshot",
                "/v1/capabilities",
                "/v1/activity/daily?from=2026-07-17&to=2026-07-18",
                "/v1/sources/status",
            ])
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            exported = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(exported["schemaVersion"], "openusage-diagnostics-2")
            self.assertEqual(exported["range"]["timezone"], "Asia/Singapore")
            privacy = subprocess.run(
                [
                    str(ROOT / ".build-venv/bin/python"),
                    str(ROOT / "scripts/privacy_scan.py"),
                    str(output),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(privacy.returncode, 0, privacy.stderr)

    def test_cli_v2_retries_all_four_routes_after_first_revision_drift(self) -> None:
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
            output = root / "diagnostics-v2.json"
            with DriftingReconciliationServer(socket_path) as server:
                result = subprocess.run(
                    [
                        str(SCRIPT), "--socket", str(socket_path),
                        "--app", str(app), "--output", str(output),
                        "--schema-version", "2",
                        "--from", "2026-07-17", "--to", "2026-07-18",
                        "--timezone", "Asia/Singapore",
                    ],
                    capture_output=True,
                    text=True,
                )

            route_cycle = [
                "/v1/snapshot",
                "/v1/capabilities",
                "/v1/activity/daily?from=2026-07-17&to=2026-07-18",
                "/v1/sources/status",
            ]
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(server.requests, route_cycle + route_cycle)
            exported = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(exported["localAPI"]["dataRevision"], 19)
            self.assertEqual(exported["schemaVersion"], "openusage-diagnostics-2")


if __name__ == "__main__":
    unittest.main()
