from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openusage_bar.gateway.ingress import parse_gateway_request
from openusage_bar.gateway.server import create_gateway_server

from scripts.measure_gateway_performance import (
    LatencyRound,
    MeasurementOutcome,
    PairedLatencyRound,
    ReferenceMachine,
    ThroughputRound,
    _filesystem,
    build_performance_report,
    load_performance_fixture,
    main as performance_main,
    measure_latency_round,
    measure_paired_latency_round,
    run_authenticated_loopback_smoke,
    summarize_paired_latency_rounds,
    summarize_latency_rounds,
    summarize_throughput_rounds,
    validate_performance_report,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/gateway/performance-v1.json"
SCHEMA = ROOT / "docs/schemas/gateway-performance-v1.schema.json"


class GatewayPerformanceStatisticsTests(unittest.TestCase):
    def test_latency_summary_uses_nearest_rank_and_selects_the_worst_round(self):
        rounds = (
            LatencyRound(tuple(range(1, 101)), warmup_count=100),
            LatencyRound(tuple(range(1, 99)) + (500, 501), warmup_count=100),
            LatencyRound((50,) * 100, warmup_count=100),
        )

        summary = summarize_latency_rounds(
            rounds,
            required_rounds=3,
            required_warmups=100,
            required_samples=100,
            threshold_ns=100,
        )

        self.assertEqual(
            [round_summary["p99Ns"] for round_summary in summary["rounds"]],
            [99, 500, 50],
        )
        self.assertEqual(summary["selectedRound"], 2)
        self.assertEqual(summary["worstP99Ns"], 500)
        self.assertEqual(summary["status"], "fail")

    def test_paired_overhead_keeps_negative_deltas_and_alternating_order(self):
        alternating = (True, False, True, False, True)
        rounds = (
            PairedLatencyRound(
                direct_ns=(10, 10, 10, 10, 10),
                gateway_ns=(5, 13, 14, 15, 16),
                attempt_direct_first=alternating,
                warmup_count=100,
            ),
            PairedLatencyRound(
                direct_ns=(10, 10, 10, 10, 10),
                gateway_ns=(11, 12, 13, 14, 30),
                attempt_direct_first=alternating,
                warmup_count=100,
            ),
            PairedLatencyRound(
                direct_ns=(10, 10, 10, 10, 10),
                gateway_ns=(11, 12, 13, 14, 17),
                attempt_direct_first=alternating,
                warmup_count=100,
            ),
        )

        summary = summarize_paired_latency_rounds(
            rounds,
            required_rounds=3,
            required_warmups=100,
            required_samples=5,
            threshold_ns=10,
        )

        self.assertEqual(summary["rounds"][0]["minDeltaNs"], -5)
        self.assertEqual(summary["rounds"][0]["negativePairCount"], 1)
        self.assertEqual(summary["rounds"][0]["p99DeltaNs"], 6)
        self.assertEqual(summary["selectedRound"], 2)
        self.assertEqual(summary["worstP99DeltaNs"], 20)
        self.assertEqual(summary["status"], "fail")

    def test_throughput_uses_completion_time_and_every_half_open_second_bucket(self):
        second = 1_000_000_000
        start = 10 * second
        rounds = (
            ThroughputRound(
                window_start_ns=start,
                duration_seconds=3,
                successful_completion_ns=(
                    start,
                    start + second - 1,
                    start + second,
                    start + 2 * second - 1,
                    start + 2 * second,
                    start + 3 * second - 1,
                ),
            ),
            ThroughputRound(
                window_start_ns=start,
                duration_seconds=3,
                successful_completion_ns=(
                    start,
                    start + 1,
                    start + 2,
                    start + second,
                    start + 2 * second,
                    start + 2 * second + 1,
                    start + 2 * second + 2,
                ),
            ),
            ThroughputRound(
                window_start_ns=start,
                duration_seconds=3,
                successful_completion_ns=(
                    start + 1,
                    start + 2,
                    start + second + 1,
                    start + second + 2,
                    start + 2 * second + 1,
                    start + 2 * second + 2,
                ),
            ),
        )

        summary = summarize_throughput_rounds(
            rounds,
            required_rounds=3,
            required_duration_seconds=3,
            minimum_successes_per_second=2,
        )

        self.assertEqual(summary["rounds"][0]["completeBucketSuccesses"], [2, 2, 2])
        self.assertEqual(summary["rounds"][0]["bucketDurationSeconds"], 1)
        self.assertEqual(summary["rounds"][0]["completeBucketCount"], 3)
        self.assertEqual(summary["rounds"][1]["completeBucketSuccesses"], [3, 1, 3])
        self.assertEqual(summary["selectedRound"], 2)
        self.assertEqual(summary["minimumCompleteBucketSuccesses"], 1)
        self.assertEqual(summary["status"], "fail")

    def test_throughput_tracks_boundary_late_completions_without_calling_them_errors(self):
        second = 1_000_000_000
        completions = tuple(range(100)) + (second, second + 1)

        summary = summarize_throughput_rounds(
            (
                ThroughputRound(
                    window_start_ns=0,
                    duration_seconds=1,
                    successful_completion_ns=completions,
                    warmup_count=100,
                ),
            ),
            required_rounds=1,
            required_duration_seconds=1,
            minimum_successes_per_second=100,
            required_warmups=100,
        )

        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["rounds"][0]["errorCount"], 0)
        self.assertEqual(summary["rounds"][0]["lateCompletionCount"], 2)
        self.assertEqual(summary["rounds"][0]["totalSuccessfulInWindow"], 100)

    def test_errors_timeouts_and_rate_limits_fail_closed_without_becoming_samples(self):
        latency = summarize_latency_rounds(
            (LatencyRound((), warmup_count=0, error_count=1, timeout_count=1),),
            required_rounds=1,
            required_warmups=0,
            required_samples=1,
            threshold_ns=50_000_000,
        )
        paired = summarize_paired_latency_rounds(
            (
                PairedLatencyRound(
                    direct_ns=(),
                    gateway_ns=(),
                    attempt_direct_first=(True,),
                    warmup_count=0,
                    error_count=1,
                    rate_limit_count=1,
                ),
            ),
            required_rounds=1,
            required_warmups=0,
            required_samples=1,
            threshold_ns=200_000_000,
        )
        throughput = summarize_throughput_rounds(
            (
                ThroughputRound(
                    window_start_ns=0,
                    duration_seconds=1,
                    successful_completion_ns=(),
                    error_count=1,
                    timeout_count=1,
                ),
            ),
            required_rounds=1,
            required_duration_seconds=1,
            minimum_successes_per_second=100,
        )

        self.assertEqual(latency["status"], "fail")
        self.assertEqual(latency["rounds"][0]["sampleCount"], 0)
        self.assertEqual(paired["status"], "fail")
        self.assertEqual(paired["rounds"][0]["rateLimitCount"], 1)
        self.assertEqual(throughput["status"], "fail")
        self.assertEqual(throughput["rounds"][0]["timeoutCount"], 1)

    def test_latency_measurement_uses_injected_monotonic_ns_and_invalidates_regression(self):
        timestamps = iter((10, 15, 20, 29, 30, 25))

        measured = measure_latency_round(
            lambda: MeasurementOutcome(ok=True),
            warmup_attempts=1,
            sample_attempts=2,
            monotonic_ns=lambda: next(timestamps),
        )
        summary = summarize_latency_rounds(
            (measured,),
            required_rounds=1,
            required_warmups=1,
            required_samples=2,
            threshold_ns=100,
        )

        self.assertEqual(measured.samples_ns, (9,))
        self.assertEqual(measured.clock_regression_count, 1)
        self.assertEqual(summary["status"], "invalid")

    def test_paired_measurement_alternates_order_with_the_injected_clock(self):
        timestamps = iter((0, 10, 20, 35, 40, 52, 60, 70))

        measured = measure_paired_latency_round(
            lambda: MeasurementOutcome(ok=True),
            lambda: MeasurementOutcome(ok=True),
            warmup_attempts=0,
            sample_attempts=2,
            monotonic_ns=lambda: next(timestamps),
        )

        self.assertEqual(measured.attempt_direct_first, (True, False))
        self.assertEqual(measured.direct_ns, (10, 10))
        self.assertEqual(measured.gateway_ns, (15, 12))


class GatewayPerformanceFixtureTests(unittest.TestCase):
    def test_gateway_server_queue_tracks_validated_max_threads(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server = create_gateway_server(
                object(),
                port=0,
                token_path=root / "gateway.token",
                max_threads=32,
            )
            try:
                self.assertEqual(server.request_queue_size, 32)
            finally:
                server.server_close()

            for index, invalid in enumerate((0, -1, False, True)):
                token_path = root / f"invalid-{index}.token"
                with self.subTest(max_threads=invalid), self.assertRaises(ValueError):
                    create_gateway_server(
                        object(),
                        port=0,
                        token_path=token_path,
                        max_threads=invalid,
                    )
                self.assertFalse(token_path.exists())

    def test_darwin_filesystem_uses_the_target_mounts_diskutil_metadata(self):
        df_output = (
            "Filesystem 512-blocks Used Available Capacity Mounted on\n"
            "/dev/disk3s5 100 50 50 50% /System/Volumes/Data"
        )
        disk_output = (
            "Device Identifier: disk3s5\n"
            "File System Personality: APFS\n"
            "Type (Bundle): apfs"
        )

        def command_value(command):
            if command[0] == "df":
                return df_output
            if command == ("diskutil", "info", "/System/Volumes/Data"):
                return disk_output
            return None

        with patch(
            "scripts.measure_gateway_performance.sys.platform", "darwin"
        ), patch(
            "scripts.measure_gateway_performance._command_value",
            side_effect=command_value,
        ):
            filesystem = _filesystem(Path("/private/var/folders/fixture"))

        self.assertEqual(filesystem, "apfs")

    def test_fixture_freezes_the_published_workload_and_exact_body_sizes(self):
        fixture = load_performance_fixture(FIXTURE)

        self.assertEqual(fixture.round_count, 3)
        self.assertEqual(fixture.warmup_count, 100)
        self.assertEqual(fixture.sample_count, 2_000)
        self.assertEqual(fixture.throughput_duration_seconds, 30)
        self.assertEqual(fixture.throughput_concurrency, 16)
        self.assertEqual(fixture.server_max_threads, 32)
        self.assertEqual(fixture.activity_quota_state_rows, 1_000)
        self.assertEqual(fixture.telemetry_request_aggregate_rows, 4_000)
        self.assertEqual(fixture.cache_entries, 1_000)
        self.assertEqual(fixture.cache_core_lookup_p99_ns, 5_000_000)

        should_send = json.dumps(
            fixture.should_send_request,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        response = fixture.response_request(fixture.hot_cache_entry_index)
        response_body = json.dumps(
            response,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        self.assertEqual(len(should_send), 82)
        self.assertEqual(len(response_body), 4_096)
        self.assertTrue(parse_gateway_request(response).stream)
        self.assertEqual(
            len(fixture.provider_output_text.encode("utf-8")),
            4_096,
        )

    def test_real_authenticated_loopback_smoke_uses_only_fixture_egress(self):
        fixture = load_performance_fixture(FIXTURE)

        result = run_authenticated_loopback_smoke(fixture)

        self.assertEqual(
            result,
            {
                "shouldSendStatus": 200,
                "primeResponsesStatus": 200,
                "exactHitResponsesStatus": 200,
                "exactHitCacheOutcome": "exact_hit",
                "fixtureEgressCalls": 1,
                "realCredentialReads": 0,
                "realProviderNetworkCalls": 0,
            },
        )
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn("openai", serialized)
        self.assertNotIn("gpt-4.1-mini", serialized)


class GatewayPerformanceReportTests(unittest.TestCase):
    def test_enforce_changes_only_failed_threshold_exit_not_evidence_class(self):
        source_commit = "a" * 40
        diagnostic_report = {
            "evidenceClass": "diagnostic",
            "overallStatus": "fail",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for enforce, expected_exit in ((False, 0), (True, 2)):
                with self.subTest(enforce=enforce):
                    output = root / f"enforce-{enforce}.json"
                    arguments = [
                        "run",
                        "--output",
                        str(output),
                        "--source-commit",
                        source_commit,
                        "--source-tree-state",
                        "clean",
                    ]
                    if enforce:
                        arguments.append("--enforce")
                    with (
                        patch(
                            "scripts.measure_gateway_performance.detect_source_provenance",
                            return_value={
                                "sourceCommit": source_commit,
                                "sourceTreeState": "clean",
                            },
                        ),
                        patch(
                            "scripts.measure_gateway_performance.detect_reference_machine",
                            return_value=object(),
                        ),
                        patch(
                            "scripts.measure_gateway_performance.run_performance_report",
                            return_value=diagnostic_report,
                        ),
                    ):
                        result = performance_main(arguments)

                    self.assertEqual(result, expected_exit)
                    self.assertEqual(
                        json.loads(output.read_text(encoding="utf-8"))[
                            "evidenceClass"
                        ],
                        "diagnostic",
                    )

    def test_report_and_schema_lock_evidence_class_to_diagnostic(self):
        fixture = load_performance_fixture(FIXTURE)
        latency_rounds = (
            LatencyRound((1_000_000,) * 2_000, warmup_count=100),
        ) * 3
        paired_rounds = (
            PairedLatencyRound(
                direct_ns=(100,) * 2_000,
                gateway_ns=(110,) * 2_000,
                attempt_direct_first=tuple(
                    index % 2 == 0 for index in range(2_000)
                ),
                warmup_count=100,
            ),
        ) * 3
        completions = tuple(
            second * 1_000_000_000 + offset
            for second in range(30)
            for offset in range(100)
        )
        throughput_rounds = (
            ThroughputRound(
                window_start_ns=0,
                duration_seconds=30,
                successful_completion_ns=completions,
                warmup_count=100,
            ),
        ) * 3
        report = build_performance_report(
            fixture=fixture,
            captured_at="2026-08-09T12:30:00Z",
            source_commit="a" * 40,
            source_tree_state="clean",
            reference_machine=ReferenceMachine(
                os_name="macos",
                os_version="15.6",
                os_build="24G84",
                architecture="arm64",
                cpu_model="Apple M4 Pro",
                logical_cpu_count=12,
                memory_bytes=25_769_803_776,
                storage_class="ssd",
                filesystem="apfs",
                power_state="ac",
                python_version="3.13.7",
            ),
            should_send_rounds=latency_rounds,
            proxy_overhead_rounds=paired_rounds,
            cache_e2e_rounds=latency_rounds,
            cache_core_rounds=latency_rounds,
            throughput_rounds=throughput_rounds,
        )
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

        self.assertEqual(report.get("evidenceClass"), "diagnostic")
        self.assertIn("evidenceClass", schema["required"])
        self.assertEqual(
            schema["properties"]["evidenceClass"],
            {"const": "diagnostic", "type": "string"},
        )

    def test_throughput_total_completed_must_exactly_account_for_every_completion(self):
        fixture = load_performance_fixture(FIXTURE)
        latency_rounds = (
            LatencyRound((1_000_000,) * 2_000, warmup_count=100),
        ) * 3
        paired_rounds = (
            PairedLatencyRound(
                direct_ns=(100,) * 2_000,
                gateway_ns=(110,) * 2_000,
                attempt_direct_first=tuple(
                    index % 2 == 0 for index in range(2_000)
                ),
                warmup_count=100,
            ),
        ) * 3
        completions = tuple(
            second * 1_000_000_000 + offset
            for second in range(30)
            for offset in range(100)
        )
        throughput_rounds = (
            ThroughputRound(
                window_start_ns=0,
                duration_seconds=30,
                successful_completion_ns=completions,
                warmup_count=100,
            ),
        ) * 3
        report = build_performance_report(
            fixture=fixture,
            captured_at="2026-08-09T12:30:00Z",
            source_commit="a" * 40,
            source_tree_state="clean",
            reference_machine=ReferenceMachine(
                os_name="macos",
                os_version="15.6",
                os_build="24G84",
                architecture="arm64",
                cpu_model="Apple M4 Pro",
                logical_cpu_count=12,
                memory_bytes=25_769_803_776,
                storage_class="ssd",
                filesystem="apfs",
                power_state="ac",
                python_version="3.13.7",
            ),
            should_send_rounds=latency_rounds,
            proxy_overhead_rounds=paired_rounds,
            cache_e2e_rounds=latency_rounds,
            cache_core_rounds=latency_rounds,
            throughput_rounds=throughput_rounds,
        )
        self.assertEqual(validate_performance_report(report), report)

        throughput_round = report["scenarios"]["responsesThroughput"]["rounds"][0]
        throughput_round["totalCompleted"] += 1

        with self.assertRaisesRegex(ValueError, "invalid performance report"):
            validate_performance_report(report)

    def test_report_is_allowlist_only_and_uses_only_aggregate_measurements(self):
        fixture = load_performance_fixture(FIXTURE)
        latency_rounds = tuple(
            LatencyRound((1_000_000,) * 2_000, warmup_count=100)
            for _ in range(3)
        )
        order = tuple(index % 2 == 0 for index in range(2_000))
        paired_rounds = tuple(
            PairedLatencyRound(
                direct_ns=(100,) * 2_000,
                gateway_ns=(110,) * 2_000,
                attempt_direct_first=order,
                warmup_count=100,
            )
            for _ in range(3)
        )
        completions = tuple(
            second * 1_000_000_000 + offset
            for second in range(30)
            for offset in range(100)
        )
        throughput_rounds = tuple(
            ThroughputRound(
                window_start_ns=0,
                duration_seconds=30,
                successful_completion_ns=completions,
                warmup_count=100,
            )
            for _ in range(3)
        )
        machine = ReferenceMachine(
            os_name="macos",
            os_version="15.6",
            os_build="24G84",
            architecture="arm64",
            cpu_model="Apple M4 Pro",
            logical_cpu_count=12,
            memory_bytes=25_769_803_776,
            storage_class="ssd",
            filesystem="apfs",
            power_state="ac",
            python_version="3.13.7",
        )

        report = build_performance_report(
            fixture=fixture,
            captured_at="2026-08-09T12:30:00Z",
            source_commit="a" * 40,
            source_tree_state="clean",
            reference_machine=machine,
            should_send_rounds=latency_rounds,
            proxy_overhead_rounds=paired_rounds,
            cache_e2e_rounds=latency_rounds,
            cache_core_rounds=latency_rounds,
            throughput_rounds=throughput_rounds,
        )

        self.assertEqual(report["overallStatus"], "pass")
        self.assertEqual(validate_performance_report(report), report)
        self.assertEqual(
            set(report),
            {
                "capturedAt",
                "evidenceClass",
                "measurementVersion",
                "overallStatus",
                "plan",
                "privacy",
                "referenceMachine",
                "reportKind",
                "scenarios",
                "schemaVersion",
                "sourceCommit",
                "sourceTreeState",
            },
        )
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn("openai", serialized)
        self.assertNotIn("gpt-4.1-mini", serialized)
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn("gateway.token", serialized)

        report["referenceMachine"]["hostname"] = "private-host"
        with self.assertRaisesRegex(ValueError, "invalid performance report"):
            validate_performance_report(report)

    def test_cache_budget_applies_to_core_lookup_and_keeps_e2e_informational(self):
        fixture = load_performance_fixture(FIXTURE)
        fast = tuple(
            LatencyRound((1_000_000,) * 2_000, warmup_count=100)
            for _ in range(3)
        )
        slow = tuple(
            LatencyRound((10_000_000,) * 2_000, warmup_count=100)
            for _ in range(3)
        )
        paired = tuple(
            PairedLatencyRound(
                direct_ns=(100,) * 2_000,
                gateway_ns=(110,) * 2_000,
                attempt_direct_first=tuple(index % 2 == 0 for index in range(2_000)),
                warmup_count=100,
            )
            for _ in range(3)
        )
        completions = tuple(
            second * 1_000_000_000 + offset
            for second in range(30)
            for offset in range(100)
        )
        throughput = tuple(
            ThroughputRound(
                window_start_ns=0,
                duration_seconds=30,
                successful_completion_ns=completions,
                warmup_count=100,
            )
            for _ in range(3)
        )
        machine = ReferenceMachine(
            os_name="macos",
            os_version="15.6",
            os_build="24G84",
            architecture="arm64",
            cpu_model="Apple M4 Pro",
            logical_cpu_count=12,
            memory_bytes=25_769_803_776,
            storage_class="ssd",
            filesystem="apfs",
            power_state="ac",
            python_version="3.13.7",
        )

        report = build_performance_report(
            fixture=fixture,
            captured_at="2026-08-09T12:30:00Z",
            source_commit="a" * 40,
            source_tree_state="clean",
            reference_machine=machine,
            should_send_rounds=fast,
            proxy_overhead_rounds=paired,
            cache_e2e_rounds=slow,
            cache_core_rounds=fast,
            throughput_rounds=throughput,
        )
        cache = report["scenarios"]["responsesExactCacheHit"]

        self.assertEqual(cache["status"], "pass")
        self.assertEqual(cache["coreLookup"]["status"], "pass")
        self.assertTrue(cache["e2eAuthenticatedLoopback"]["informationalOnly"])
        self.assertEqual(
            cache["e2eAuthenticatedLoopback"]["measurement"]["status"],
            "fail",
        )
        self.assertEqual(report["overallStatus"], "pass")

    def test_committed_report_schema_closes_every_object_shape(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

        def schema_nodes(node):
            if type(node) is not dict:
                return
            yield node
            for name in ("$defs", "properties"):
                values = node.get(name, {})
                if type(values) is dict:
                    for child in values.values():
                        yield from schema_nodes(child)
            if "items" in node:
                yield from schema_nodes(node["items"])

        object_nodes = [
            node for node in schema_nodes(schema) if node.get("type") == "object"
        ]
        self.assertGreaterEqual(len(object_nodes), 12)
        self.assertTrue(
            all(node.get("additionalProperties") is False for node in object_nodes)
        )
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["properties"]["measurementVersion"]["const"], "gateway-performance-v1")


if __name__ == "__main__":
    unittest.main()
