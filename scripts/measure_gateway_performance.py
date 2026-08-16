#!/usr/bin/env python3
"""Measure and summarize privacy-safe OpenUsage Gateway performance evidence."""

from __future__ import annotations

import argparse
import ctypes
import http.client
import json
import os
import platform
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from typing import Callable, Iterator, Sequence


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


MAX_FIXTURE_BYTES = 64 * 1024
MAX_REPORT_BYTES = 512 * 1024
_SAFE_MACHINE_TEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()+@-]{0,127}")
_SOURCE_COMMIT = re.compile(r"[0-9a-f]{40}")
_PYTHON_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def _nonnegative_integer(value: object) -> bool:
    return type(value) is int and value >= 0


def _positive_integer(value: object) -> bool:
    return type(value) is int and value > 0


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid performance fixture")
        result[key] = value
    return result


def _exact_object(
    value: object,
    keys: frozenset[str],
) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise ValueError("invalid performance fixture")
    return value


@dataclass(frozen=True, slots=True)
class PerformanceFixture:
    """Strict, versioned workload definition; identities never enter reports."""

    clock_utc: str
    round_count: int
    warmup_count: int
    sample_count: int
    throughput_duration_seconds: int
    throughput_concurrency: int
    server_max_threads: int
    throughput_bucket_seconds: int
    request_timeout_seconds: int
    admission_rate_limit_capacity: int
    admission_rate_limit_refill_per_second: int
    activity_quota_state_rows: int
    telemetry_request_aggregate_rows: int
    telemetry_matching_in_window_rows: int
    telemetry_matching_outside_window_rows: int
    telemetry_other_model_rows: int
    telemetry_other_provider_rows: int
    cache_entries: int
    hot_cache_entry_index: int
    cache_prefixes_per_entry: int
    should_send_body_bytes: int
    responses_body_bytes: int
    provider_output_text_utf8_bytes: int
    should_send_p99_ns: int
    proxy_overhead_p99_ns: int
    cache_core_lookup_p99_ns: int
    throughput_minimum_successes_per_second: int
    _should_send_provider: str
    _should_send_model: str
    _should_send_estimated_tokens: int
    _should_send_window: str
    _response_provider: str
    _response_model: str
    _response_input_bytes: int
    _response_input_fill: str
    _response_stream: bool
    _provider_output_fill: str

    @property
    def should_send_request(self) -> dict[str, object]:
        return {
            "provider": self._should_send_provider,
            "model": self._should_send_model,
            "estimated_tokens": self._should_send_estimated_tokens,
            "window": self._should_send_window,
        }

    def response_request(self, index: int) -> dict[str, object]:
        if type(index) is not int or not 0 <= index < self.cache_entries:
            raise ValueError("invalid performance fixture entry")
        marker = f"{index:04d}"
        if len(marker) > self._response_input_bytes:
            raise ValueError("invalid performance fixture entry")
        input_text = marker + self._response_input_fill * (
            self._response_input_bytes - len(marker)
        )
        return {
            "provider": self._response_provider,
            "model": self._response_model,
            "request": {
                "model": self._response_model,
                "input": input_text,
                "stream": self._response_stream,
            },
        }

    @property
    def provider_output_text(self) -> str:
        return self._provider_output_fill * self.provider_output_text_utf8_bytes


def _machine_text(value: object) -> bool:
    return type(value) is str and _SAFE_MACHINE_TEXT.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class ReferenceMachine:
    """Path-free reference-machine facts needed to interpret one report."""

    os_name: str
    os_version: str
    os_build: str
    architecture: str
    cpu_model: str
    logical_cpu_count: int
    memory_bytes: int
    storage_class: str
    filesystem: str
    power_state: str
    python_version: str

    def __post_init__(self) -> None:
        if (
            self.os_name not in {"macos", "windows", "linux"}
            or not _machine_text(self.os_version)
            or not _machine_text(self.os_build)
            or not _machine_text(self.architecture)
            or not _machine_text(self.cpu_model)
            or not _positive_integer(self.logical_cpu_count)
            or not _positive_integer(self.memory_bytes)
            or self.storage_class not in {"ssd", "hdd", "unknown"}
            or not _machine_text(self.filesystem)
            or self.power_state not in {"ac", "battery", "unknown"}
            or type(self.python_version) is not str
            or _PYTHON_VERSION.fullmatch(self.python_version) is None
        ):
            raise ValueError("invalid reference machine")

    def payload(self) -> dict[str, object]:
        return {
            "osName": self.os_name,
            "osVersion": self.os_version,
            "osBuild": self.os_build,
            "architecture": self.architecture,
            "cpuModel": self.cpu_model,
            "logicalCpuCount": self.logical_cpu_count,
            "memoryBytes": self.memory_bytes,
            "storageClass": self.storage_class,
            "filesystem": self.filesystem,
            "powerState": self.power_state,
            "pythonVersion": self.python_version,
        }


def load_performance_fixture(path: str | Path) -> PerformanceFixture:
    """Read the one frozen v1 fixture without leaking its local path."""

    source = Path(path)
    try:
        metadata = source.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise OSError
        if not 0 < metadata.st_size <= MAX_FIXTURE_BYTES:
            raise OSError
        raw = source.read_bytes()
    except OSError:
        raise ValueError("performance fixture unavailable") from None
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (
        json.JSONDecodeError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
        UnicodeError,
    ):
        raise ValueError("invalid performance fixture") from None

    root = _exact_object(
        payload,
        frozenset(
            {
                "clockUtc",
                "facts",
                "measurementVersion",
                "plan",
                "schemaVersion",
                "thresholds",
                "workloads",
            }
        ),
    )
    plan = _exact_object(
        root["plan"],
        frozenset(
            {
                "admissionRateLimitCapacity",
                "admissionRateLimitRefillPerSecond",
                "clock",
                "httpConnectionPolicy",
                "p99Method",
                "requestTimeoutSeconds",
                "roundCount",
                "sampleCount",
                "serverMaxThreads",
                "throughputBucketSeconds",
                "throughputConcurrency",
                "throughputDurationSeconds",
                "warmupCount",
            }
        ),
    )
    facts = _exact_object(
        root["facts"],
        frozenset(
            {
                "activityQuotaStateRows",
                "cacheEntries",
                "cachePrefixesPerEntry",
                "hotCacheEntryIndex",
                "providerOutputTextUtf8Bytes",
                "responsesBodyBytes",
                "shouldSendBodyBytes",
                "telemetryMatchingInWindowRows",
                "telemetryMatchingOutsideWindowRows",
                "telemetryOtherModelRows",
                "telemetryOtherProviderRows",
                "telemetryRequestAggregateRows",
            }
        ),
    )
    thresholds = _exact_object(
        root["thresholds"],
        frozenset(
            {
                "cacheCoreLookupP99Ns",
                "proxyOverheadP99Ns",
                "shouldSendP99Ns",
                "throughputMinimumSuccessesPerSecond",
            }
        ),
    )
    workloads = _exact_object(
        root["workloads"],
        frozenset({"responses", "shouldSend"}),
    )
    should_send = _exact_object(
        workloads["shouldSend"],
        frozenset({"estimated_tokens", "model", "provider", "window"}),
    )
    responses = _exact_object(
        workloads["responses"],
        frozenset(
            {
                "inputBytes",
                "inputFill",
                "model",
                "provider",
                "providerOutputFill",
                "stream",
            }
        ),
    )

    expected_plan = {
        "admissionRateLimitCapacity": 10_000,
        "admissionRateLimitRefillPerSecond": 10_000,
        "clock": "monotonic_ns",
        "httpConnectionPolicy": "connection-close",
        "p99Method": "nearest-rank-ceil",
        "requestTimeoutSeconds": 5,
        "roundCount": 3,
        "sampleCount": 2_000,
        "serverMaxThreads": 32,
        "throughputBucketSeconds": 1,
        "throughputConcurrency": 16,
        "throughputDurationSeconds": 30,
        "warmupCount": 100,
    }
    expected_facts = {
        "activityQuotaStateRows": 1_000,
        "cacheEntries": 1_000,
        "cachePrefixesPerEntry": 0,
        "hotCacheEntryIndex": 500,
        "providerOutputTextUtf8Bytes": 4_096,
        "responsesBodyBytes": 4_096,
        "shouldSendBodyBytes": 82,
        "telemetryMatchingInWindowRows": 1_000,
        "telemetryMatchingOutsideWindowRows": 1_000,
        "telemetryOtherModelRows": 1_000,
        "telemetryOtherProviderRows": 1_000,
        "telemetryRequestAggregateRows": 4_000,
    }
    expected_thresholds = {
        "cacheCoreLookupP99Ns": 5_000_000,
        "proxyOverheadP99Ns": 200_000_000,
        "shouldSendP99Ns": 50_000_000,
        "throughputMinimumSuccessesPerSecond": 100,
    }
    expected_should_send = {
        "estimated_tokens": 8_000,
        "model": "gpt-4.1-mini",
        "provider": "openai",
        "window": "5m",
    }
    expected_responses = {
        "inputBytes": 3_992,
        "inputFill": "x",
        "model": "gpt-4.1-mini",
        "provider": "openai",
        "providerOutputFill": "o",
        "stream": True,
    }
    try:
        parsed_clock = datetime.fromisoformat(
            str(root["clockUtc"]).replace("Z", "+00:00")
        )
    except (TypeError, ValueError, OverflowError):
        parsed_clock = None
    if (
        root["schemaVersion"] != 1
        or type(root["schemaVersion"]) is not int
        or root["measurementVersion"] != "gateway-performance-v1"
        or root["clockUtc"] != "2026-08-09T12:00:00Z"
        or parsed_clock is None
        or parsed_clock.utcoffset() is None
        or plan != expected_plan
        or facts != expected_facts
        or thresholds != expected_thresholds
        or should_send != expected_should_send
        or responses != expected_responses
        or sum(
            facts[name]
            for name in (
                "telemetryMatchingInWindowRows",
                "telemetryMatchingOutsideWindowRows",
                "telemetryOtherModelRows",
                "telemetryOtherProviderRows",
            )
        )
        != facts["telemetryRequestAggregateRows"]
    ):
        raise ValueError("invalid performance fixture")

    fixture = PerformanceFixture(
        clock_utc=root["clockUtc"],
        round_count=plan["roundCount"],
        warmup_count=plan["warmupCount"],
        sample_count=plan["sampleCount"],
        throughput_duration_seconds=plan["throughputDurationSeconds"],
        throughput_concurrency=plan["throughputConcurrency"],
        server_max_threads=plan["serverMaxThreads"],
        throughput_bucket_seconds=plan["throughputBucketSeconds"],
        request_timeout_seconds=plan["requestTimeoutSeconds"],
        admission_rate_limit_capacity=plan["admissionRateLimitCapacity"],
        admission_rate_limit_refill_per_second=plan[
            "admissionRateLimitRefillPerSecond"
        ],
        activity_quota_state_rows=facts["activityQuotaStateRows"],
        telemetry_request_aggregate_rows=facts[
            "telemetryRequestAggregateRows"
        ],
        telemetry_matching_in_window_rows=facts[
            "telemetryMatchingInWindowRows"
        ],
        telemetry_matching_outside_window_rows=facts[
            "telemetryMatchingOutsideWindowRows"
        ],
        telemetry_other_model_rows=facts["telemetryOtherModelRows"],
        telemetry_other_provider_rows=facts["telemetryOtherProviderRows"],
        cache_entries=facts["cacheEntries"],
        hot_cache_entry_index=facts["hotCacheEntryIndex"],
        cache_prefixes_per_entry=facts["cachePrefixesPerEntry"],
        should_send_body_bytes=facts["shouldSendBodyBytes"],
        responses_body_bytes=facts["responsesBodyBytes"],
        provider_output_text_utf8_bytes=facts[
            "providerOutputTextUtf8Bytes"
        ],
        should_send_p99_ns=thresholds["shouldSendP99Ns"],
        proxy_overhead_p99_ns=thresholds["proxyOverheadP99Ns"],
        cache_core_lookup_p99_ns=thresholds["cacheCoreLookupP99Ns"],
        throughput_minimum_successes_per_second=thresholds[
            "throughputMinimumSuccessesPerSecond"
        ],
        _should_send_provider=should_send["provider"],
        _should_send_model=should_send["model"],
        _should_send_estimated_tokens=should_send["estimated_tokens"],
        _should_send_window=should_send["window"],
        _response_provider=responses["provider"],
        _response_model=responses["model"],
        _response_input_bytes=responses["inputBytes"],
        _response_input_fill=responses["inputFill"],
        _response_stream=responses["stream"],
        _provider_output_fill=responses["providerOutputFill"],
    )
    should_body = json.dumps(
        fixture.should_send_request,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    response_body = json.dumps(
        fixture.response_request(fixture.hot_cache_entry_index),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if (
        len(should_body) != fixture.should_send_body_bytes
        or len(response_body) != fixture.responses_body_bytes
        or len(fixture.provider_output_text.encode("utf-8"))
        != fixture.provider_output_text_utf8_bytes
    ):
        raise ValueError("invalid performance fixture")
    return fixture


def _fixture_sse(output_text: str) -> bytes:
    delta = json.dumps(
        {"delta": output_text, "type": "response.output_text.delta"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    completion = json.dumps(
        {
            "response": {
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
            "type": "response.completed",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return (
        b"event: response.output_text.delta\n"
        b"data: "
        + delta
        + b"\n\n"
        + b"event: response.completed\n"
        + b"data: "
        + completion
        + b"\n\n"
    )


def _authenticated_json_request(
    *,
    port: int,
    token: str,
    target: str,
    payload: dict[str, object],
    timeout_seconds: int,
) -> tuple[int, dict[str, object]]:
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        port,
        timeout=timeout_seconds,
    )
    body = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        connection.request(
            "POST",
            target,
            body=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Host": f"127.0.0.1:{port}",
            },
        )
        response = connection.getresponse()
        raw = response.read()
        parsed = json.loads(raw)
    finally:
        connection.close()
    if type(parsed) is not dict:
        raise RuntimeError("performance loopback response invalid")
    return response.status, parsed


def run_authenticated_loopback_smoke(
    fixture: PerformanceFixture,
) -> dict[str, object]:
    """Exercise auth, routing, runtime, and exact cache replay without timing."""

    if type(fixture) is not PerformanceFixture:
        raise ValueError("invalid performance fixture")

    from openusage_bar.gateway.api import GatewayRouter
    from openusage_bar.gateway.cache import SQLiteGatewayCache
    from openusage_bar.gateway.contracts import (
        Decision,
        GatewayMode,
        ShouldSendDecision,
    )
    from openusage_bar.gateway.providers import ProviderResult
    from openusage_bar.gateway.runtime import GatewayRuntime
    from openusage_bar.gateway.server import create_gateway_server

    egress_calls = 0
    sse = _fixture_sse(fixture.provider_output_text)
    if len(sse) != 4_307:
        raise RuntimeError("performance fixture transport invalid")

    def fixture_egress(
        _provider_id: str,
        _request_body: bytes,
        **_kwargs: object,
    ) -> ProviderResult:
        nonlocal egress_calls
        egress_calls += 1
        return ProviderResult(
            status_code=200,
            headers=(("Content-Type", "text/event-stream"),),
            body_chunks=(sse,),
        )

    def policy(_request: object) -> ShouldSendDecision:
        return ShouldSendDecision(
            decision=Decision.YES,
            confidence=0.92,
            reason="quota_healthy",
            defer_until=None,
            quota_remaining=74_000.0,
            burn_rate_per_minute=10.0,
            predicted_exhaustion_minutes=7_400.0,
        )

    token = "g" * 48
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        cache = SQLiteGatewayCache(root / "gateway-cache.sqlite3")
        server = None
        thread = None
        try:
            runtime = GatewayRuntime(egress=fixture_egress, cache=cache)
            router = GatewayRouter(
                mode=GatewayMode.GATEWAY,
                policy=policy,
                proxy=runtime,
            )
            server = create_gateway_server(
                router,
                host="127.0.0.1",
                port=0,
                bearer_token=token,
                token_path=root / "gateway.token",
                rate_limit_capacity=fixture.admission_rate_limit_capacity,
                rate_limit_refill_per_second=(
                    fixture.admission_rate_limit_refill_per_second
                ),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            should_status, _should_payload = _authenticated_json_request(
                port=port,
                token=token,
                target="/gateway/v1/should-send",
                payload=fixture.should_send_request,
                timeout_seconds=fixture.request_timeout_seconds,
            )
            response_request = fixture.response_request(
                fixture.hot_cache_entry_index
            )
            prime_status, prime = _authenticated_json_request(
                port=port,
                token=token,
                target="/gateway/v1/responses",
                payload=response_request,
                timeout_seconds=fixture.request_timeout_seconds,
            )
            hit_status, hit = _authenticated_json_request(
                port=port,
                token=token,
                target="/gateway/v1/responses",
                payload=response_request,
                timeout_seconds=fixture.request_timeout_seconds,
            )
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            if thread is not None:
                thread.join(2)
            cache.close()

    prime_cache = prime.get("cache")
    hit_cache = hit.get("cache")
    if (
        should_status != 200
        or prime_status != 200
        or hit_status != 200
        or type(prime_cache) is not dict
        or prime_cache.get("outcome") != "miss"
        or type(hit_cache) is not dict
        or hit_cache.get("outcome") != "exact_hit"
        or egress_calls != 1
    ):
        raise RuntimeError("performance loopback smoke failed")
    return {
        "shouldSendStatus": should_status,
        "primeResponsesStatus": prime_status,
        "exactHitResponsesStatus": hit_status,
        "exactHitCacheOutcome": hit_cache["outcome"],
        "fixtureEgressCalls": egress_calls,
        "realCredentialReads": 0,
        "realProviderNetworkCalls": 0,
    }


@dataclass(frozen=True, slots=True)
class LatencyRound:
    """One already-measured latency round in monotonic nanoseconds."""

    samples_ns: tuple[int, ...]
    warmup_count: int
    warmup_error_count: int = 0
    error_count: int = 0
    timeout_count: int = 0
    rate_limit_count: int = 0
    clock_regression_count: int = 0

    def __post_init__(self) -> None:
        if (
            type(self.samples_ns) is not tuple
            or any(not _nonnegative_integer(value) for value in self.samples_ns)
            or not _nonnegative_integer(self.warmup_count)
            or not _nonnegative_integer(self.warmup_error_count)
            or not _nonnegative_integer(self.error_count)
            or not _nonnegative_integer(self.timeout_count)
            or not _nonnegative_integer(self.rate_limit_count)
            or not _nonnegative_integer(self.clock_regression_count)
            or self.timeout_count + self.rate_limit_count > self.error_count
        ):
            raise ValueError("invalid latency round")


@dataclass(frozen=True, slots=True)
class MeasurementOutcome:
    """Sanitized result classification for one measured operation."""

    ok: bool
    timeout: bool = False
    rate_limited: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.ok) is not bool
            or type(self.timeout) is not bool
            or type(self.rate_limited) is not bool
            or (self.ok and (self.timeout or self.rate_limited))
            or (self.timeout and self.rate_limited)
        ):
            raise ValueError("invalid measurement outcome")


def _operation_outcome(operation: Callable[[], MeasurementOutcome]) -> MeasurementOutcome:
    try:
        outcome = operation()
    except TimeoutError:
        return MeasurementOutcome(ok=False, timeout=True)
    except OSError:
        return MeasurementOutcome(ok=False)
    except Exception:
        return MeasurementOutcome(ok=False)
    if type(outcome) is not MeasurementOutcome:
        return MeasurementOutcome(ok=False)
    return outcome


def measure_latency_round(
    operation: Callable[[], MeasurementOutcome],
    *,
    warmup_attempts: int,
    sample_attempts: int,
    monotonic_ns: Callable[[], int],
) -> LatencyRound:
    """Measure one operation with an injected monotonic nanosecond clock."""

    if (
        not callable(operation)
        or not callable(monotonic_ns)
        or not _nonnegative_integer(warmup_attempts)
        or not _positive_integer(sample_attempts)
    ):
        raise ValueError("invalid latency measurement")

    warmup_count = 0
    warmup_errors = 0
    for _ in range(warmup_attempts):
        started = monotonic_ns()
        outcome = _operation_outcome(operation)
        finished = monotonic_ns()
        if (
            not _nonnegative_integer(started)
            or not _nonnegative_integer(finished)
            or finished < started
            or not outcome.ok
        ):
            warmup_errors += 1
        else:
            warmup_count += 1

    samples: list[int] = []
    errors = 0
    timeouts = 0
    rate_limits = 0
    clock_regressions = 0
    for _ in range(sample_attempts):
        started = monotonic_ns()
        outcome = _operation_outcome(operation)
        finished = monotonic_ns()
        if (
            not _nonnegative_integer(started)
            or not _nonnegative_integer(finished)
            or finished < started
        ):
            clock_regressions += 1
        elif outcome.ok:
            samples.append(finished - started)
        else:
            errors += 1
            timeouts += int(outcome.timeout)
            rate_limits += int(outcome.rate_limited)
    return LatencyRound(
        samples_ns=tuple(samples),
        warmup_count=warmup_count,
        warmup_error_count=warmup_errors,
        error_count=errors,
        timeout_count=timeouts,
        rate_limit_count=rate_limits,
        clock_regression_count=clock_regressions,
    )


@dataclass(frozen=True, slots=True)
class PairedLatencyRound:
    """One alternating direct-versus-Gateway overhead round."""

    direct_ns: tuple[int, ...]
    gateway_ns: tuple[int, ...]
    attempt_direct_first: tuple[bool, ...]
    warmup_count: int
    warmup_error_count: int = 0
    error_count: int = 0
    timeout_count: int = 0
    rate_limit_count: int = 0
    clock_regression_count: int = 0

    def __post_init__(self) -> None:
        if (
            type(self.direct_ns) is not tuple
            or type(self.gateway_ns) is not tuple
            or type(self.attempt_direct_first) is not tuple
            or any(not _nonnegative_integer(value) for value in self.direct_ns)
            or any(not _nonnegative_integer(value) for value in self.gateway_ns)
            or any(type(value) is not bool for value in self.attempt_direct_first)
            or not _nonnegative_integer(self.warmup_count)
            or not _nonnegative_integer(self.warmup_error_count)
            or not _nonnegative_integer(self.error_count)
            or not _nonnegative_integer(self.timeout_count)
            or not _nonnegative_integer(self.rate_limit_count)
            or not _nonnegative_integer(self.clock_regression_count)
            or self.timeout_count + self.rate_limit_count > self.error_count
        ):
            raise ValueError("invalid paired latency round")


def _timed_operation(
    operation: Callable[[], MeasurementOutcome],
    monotonic_ns: Callable[[], int],
) -> tuple[int | None, MeasurementOutcome, bool]:
    started = monotonic_ns()
    outcome = _operation_outcome(operation)
    finished = monotonic_ns()
    if (
        not _nonnegative_integer(started)
        or not _nonnegative_integer(finished)
        or finished < started
    ):
        return None, outcome, True
    return finished - started, outcome, False


def measure_paired_latency_round(
    direct_operation: Callable[[], MeasurementOutcome],
    gateway_operation: Callable[[], MeasurementOutcome],
    *,
    warmup_attempts: int,
    sample_attempts: int,
    monotonic_ns: Callable[[], int],
) -> PairedLatencyRound:
    """Measure signed paired overhead while alternating execution order."""

    if (
        not callable(direct_operation)
        or not callable(gateway_operation)
        or not callable(monotonic_ns)
        or not _nonnegative_integer(warmup_attempts)
        or not _positive_integer(sample_attempts)
    ):
        raise ValueError("invalid paired latency measurement")

    def pair(index: int) -> tuple[int | None, MeasurementOutcome, bool, int | None, MeasurementOutcome, bool]:
        direct_first = index % 2 == 0
        if direct_first:
            direct = _timed_operation(direct_operation, monotonic_ns)
            gateway = _timed_operation(gateway_operation, monotonic_ns)
        else:
            gateway = _timed_operation(gateway_operation, monotonic_ns)
            direct = _timed_operation(direct_operation, monotonic_ns)
        return (*direct, *gateway)

    warmup_count = 0
    warmup_errors = 0
    for index in range(warmup_attempts):
        direct_ns, direct, direct_regressed, gateway_ns, gateway, gateway_regressed = pair(index)
        if (
            direct_ns is None
            or gateway_ns is None
            or direct_regressed
            or gateway_regressed
            or not direct.ok
            or not gateway.ok
        ):
            warmup_errors += 1
        else:
            warmup_count += 1

    direct_samples: list[int] = []
    gateway_samples: list[int] = []
    order: list[bool] = []
    errors = 0
    timeouts = 0
    rate_limits = 0
    regressions = 0
    for index in range(sample_attempts):
        direct_ns, direct, direct_regressed, gateway_ns, gateway, gateway_regressed = pair(index)
        order.append(index % 2 == 0)
        if direct_regressed or gateway_regressed:
            regressions += 1
        elif (
            direct_ns is not None
            and gateway_ns is not None
            and direct.ok
            and gateway.ok
        ):
            direct_samples.append(direct_ns)
            gateway_samples.append(gateway_ns)
        else:
            errors += 1
            timeouts += int(direct.timeout or gateway.timeout)
            rate_limits += int(direct.rate_limited or gateway.rate_limited)
    return PairedLatencyRound(
        direct_ns=tuple(direct_samples),
        gateway_ns=tuple(gateway_samples),
        attempt_direct_first=tuple(order),
        warmup_count=warmup_count,
        warmup_error_count=warmup_errors,
        error_count=errors,
        timeout_count=timeouts,
        rate_limit_count=rate_limits,
        clock_regression_count=regressions,
    )


@dataclass(frozen=True, slots=True)
class ThroughputRound:
    """Terminal successes and fail-closed errors for one fixed window."""

    window_start_ns: int
    duration_seconds: int
    successful_completion_ns: tuple[int, ...]
    warmup_count: int = 0
    warmup_error_count: int = 0
    error_count: int = 0
    timeout_count: int = 0
    rate_limit_count: int = 0

    def __post_init__(self) -> None:
        if (
            not _nonnegative_integer(self.window_start_ns)
            or not _positive_integer(self.duration_seconds)
            or type(self.successful_completion_ns) is not tuple
            or any(
                not _nonnegative_integer(value)
                for value in self.successful_completion_ns
            )
            or not _nonnegative_integer(self.warmup_count)
            or not _nonnegative_integer(self.warmup_error_count)
            or not _nonnegative_integer(self.error_count)
            or not _nonnegative_integer(self.timeout_count)
            or not _nonnegative_integer(self.rate_limit_count)
            or self.timeout_count + self.rate_limit_count > self.error_count
        ):
            raise ValueError("invalid throughput round")


def nearest_rank_percentile_ns(
    samples_ns: Sequence[int],
    percentile: int,
) -> int:
    """Return an integer nearest-rank percentile without interpolation."""

    if (
        not _positive_integer(percentile)
        or percentile > 100
        or not samples_ns
        or any(not _nonnegative_integer(value) for value in samples_ns)
    ):
        raise ValueError("invalid percentile samples")
    ordered = sorted(samples_ns)
    rank = ceil(percentile * len(ordered) / 100)
    return ordered[rank - 1]


def _nearest_rank_signed(samples_ns: Sequence[int], percentile: int) -> int:
    if (
        not _positive_integer(percentile)
        or percentile > 100
        or not samples_ns
        or any(type(value) is not int for value in samples_ns)
    ):
        raise ValueError("invalid signed percentile samples")
    ordered = sorted(samples_ns)
    rank = ceil(percentile * len(ordered) / 100)
    return ordered[rank - 1]


def summarize_latency_rounds(
    rounds: Sequence[LatencyRound],
    *,
    required_rounds: int,
    required_warmups: int,
    required_samples: int,
    threshold_ns: int,
) -> dict[str, object]:
    """Summarize fast-path rounds using the worst valid nearest-rank p99."""

    if (
        not _positive_integer(required_rounds)
        or not _nonnegative_integer(required_warmups)
        or not _positive_integer(required_samples)
        or not _positive_integer(threshold_ns)
        or any(type(value) is not LatencyRound for value in rounds)
    ):
        raise ValueError("invalid latency measurement")

    round_summaries: list[dict[str, object]] = []
    valid_p99: list[tuple[int, int]] = []
    for number, observation in enumerate(rounds, start=1):
        contract_valid = (
            observation.warmup_count == required_warmups
            and observation.warmup_error_count == 0
            and len(observation.samples_ns) + observation.error_count
            + observation.clock_regression_count
            == required_samples
            and observation.clock_regression_count == 0
        )
        p99_ns = (
            nearest_rank_percentile_ns(observation.samples_ns, 99)
            if observation.samples_ns
            else None
        )
        if contract_valid and p99_ns is not None:
            valid_p99.append((number, p99_ns))
        if not contract_valid:
            status = "invalid"
        elif observation.error_count or p99_ns is None or p99_ns >= threshold_ns:
            status = "fail"
        else:
            status = "pass"
        round_summaries.append(
            {
                "round": number,
                "status": status,
                "warmupCount": observation.warmup_count,
                "warmupErrorCount": observation.warmup_error_count,
                "attemptCount": (
                    len(observation.samples_ns)
                    + observation.error_count
                    + observation.clock_regression_count
                ),
                "sampleCount": len(observation.samples_ns),
                "p99Ns": p99_ns,
                "errorCount": observation.error_count,
                "timeoutCount": observation.timeout_count,
                "rateLimitCount": observation.rate_limit_count,
                "clockRegressionCount": observation.clock_regression_count,
            }
        )

    selected = max(valid_p99, key=lambda item: (item[1], -item[0]), default=None)
    invalid = len(rounds) != required_rounds or any(
        item["status"] == "invalid" for item in round_summaries
    )
    failed = any(item["status"] == "fail" for item in round_summaries)
    return {
        "status": "invalid" if invalid else "fail" if failed else "pass",
        "selectedRound": selected[0] if selected is not None else None,
        "worstP99Ns": selected[1] if selected is not None else None,
        "thresholdNs": threshold_ns,
        "rounds": round_summaries,
    }


def summarize_paired_latency_rounds(
    rounds: Sequence[PairedLatencyRound],
    *,
    required_rounds: int,
    required_warmups: int,
    required_samples: int,
    threshold_ns: int,
) -> dict[str, object]:
    """Summarize signed overhead without hiding negative paired deltas."""

    if (
        not _positive_integer(required_rounds)
        or not _nonnegative_integer(required_warmups)
        or not _positive_integer(required_samples)
        or not _positive_integer(threshold_ns)
        or any(type(value) is not PairedLatencyRound for value in rounds)
    ):
        raise ValueError("invalid paired latency measurement")

    expected_order = tuple(index % 2 == 0 for index in range(required_samples))
    round_summaries: list[dict[str, object]] = []
    valid_p99: list[tuple[int, int]] = []
    for number, observation in enumerate(rounds, start=1):
        success_count = len(observation.direct_ns)
        contract_valid = (
            observation.warmup_count == required_warmups
            and observation.warmup_error_count == 0
            and len(observation.gateway_ns) == success_count
            and success_count
            + observation.error_count
            + observation.clock_regression_count
            == required_samples
            and observation.attempt_direct_first == expected_order
            and observation.clock_regression_count == 0
        )
        deltas = tuple(
            gateway - direct
            for direct, gateway in zip(
                observation.direct_ns,
                observation.gateway_ns,
            )
        )
        p99_ns = _nearest_rank_signed(deltas, 99) if deltas else None
        p50_ns = _nearest_rank_signed(deltas, 50) if deltas else None
        if contract_valid and p99_ns is not None:
            valid_p99.append((number, p99_ns))
        if not contract_valid:
            status = "invalid"
        elif observation.error_count or p99_ns is None or p99_ns >= threshold_ns:
            status = "fail"
        else:
            status = "pass"
        round_summaries.append(
            {
                "round": number,
                "status": status,
                "warmupCount": observation.warmup_count,
                "warmupErrorCount": observation.warmup_error_count,
                "attemptCount": len(observation.attempt_direct_first),
                "successfulPairCount": len(deltas),
                "negativePairCount": sum(delta < 0 for delta in deltas),
                "minDeltaNs": min(deltas) if deltas else None,
                "medianDeltaNs": p50_ns,
                "p99DeltaNs": p99_ns,
                "maxDeltaNs": max(deltas) if deltas else None,
                "errorCount": observation.error_count,
                "timeoutCount": observation.timeout_count,
                "rateLimitCount": observation.rate_limit_count,
                "clockRegressionCount": observation.clock_regression_count,
            }
        )

    selected = max(valid_p99, key=lambda item: (item[1], -item[0]), default=None)
    invalid = len(rounds) != required_rounds or any(
        item["status"] == "invalid" for item in round_summaries
    )
    failed = any(item["status"] == "fail" for item in round_summaries)
    return {
        "status": "invalid" if invalid else "fail" if failed else "pass",
        "selectedRound": selected[0] if selected is not None else None,
        "worstP99DeltaNs": selected[1] if selected is not None else None,
        "thresholdNs": threshold_ns,
        "rounds": round_summaries,
    }


def summarize_throughput_rounds(
    rounds: Sequence[ThroughputRound],
    *,
    required_rounds: int,
    required_duration_seconds: int,
    minimum_successes_per_second: int,
    required_warmups: int = 0,
) -> dict[str, object]:
    """Bucket terminal completions into every half-open one-second window."""

    if (
        not _positive_integer(required_rounds)
        or not _positive_integer(required_duration_seconds)
        or not _positive_integer(minimum_successes_per_second)
        or not _nonnegative_integer(required_warmups)
        or any(type(value) is not ThroughputRound for value in rounds)
    ):
        raise ValueError("invalid throughput measurement")

    second_ns = 1_000_000_000
    round_summaries: list[dict[str, object]] = []
    valid_minimums: list[tuple[int, int]] = []
    for number, observation in enumerate(rounds, start=1):
        window_end = (
            observation.window_start_ns
            + observation.duration_seconds * second_ns
        )
        clock_regression_count = sum(
            completed < observation.window_start_ns
            for completed in observation.successful_completion_ns
        )
        late_completion_count = sum(
            completed >= window_end
            for completed in observation.successful_completion_ns
        )
        buckets = [0] * observation.duration_seconds
        for completed in observation.successful_completion_ns:
            if observation.window_start_ns <= completed < window_end:
                bucket = (
                    completed - observation.window_start_ns
                ) // second_ns
                buckets[bucket] += 1
        contract_valid = (
            observation.duration_seconds == required_duration_seconds
            and observation.warmup_count == required_warmups
            and observation.warmup_error_count == 0
            and clock_regression_count == 0
        )
        minimum = min(buckets) if buckets else 0
        if contract_valid:
            valid_minimums.append((number, minimum))
        if not contract_valid:
            status = "invalid"
        elif observation.error_count or minimum < minimum_successes_per_second:
            status = "fail"
        else:
            status = "pass"
        round_summaries.append(
            {
                "round": number,
                "status": status,
                "warmupCount": observation.warmup_count,
                "warmupErrorCount": observation.warmup_error_count,
                "bucketDurationSeconds": 1,
                "completeBucketCount": observation.duration_seconds,
                "completeBucketSuccesses": buckets,
                "minCompleteBucketSuccesses": minimum,
                "totalSuccessfulInWindow": sum(buckets),
                "totalCompleted": (
                    len(observation.successful_completion_ns)
                    + observation.error_count
                ),
                "errorCount": observation.error_count,
                "timeoutCount": observation.timeout_count,
                "rateLimitCount": observation.rate_limit_count,
                "lateCompletionCount": late_completion_count,
                "clockRegressionCount": clock_regression_count,
            }
        )

    selected = min(valid_minimums, key=lambda item: (item[1], item[0]), default=None)
    invalid = len(rounds) != required_rounds or any(
        item["status"] == "invalid" for item in round_summaries
    )
    failed = any(item["status"] == "fail" for item in round_summaries)
    return {
        "status": "invalid" if invalid else "fail" if failed else "pass",
        "selectedRound": selected[0] if selected is not None else None,
        "minimumCompleteBucketSuccesses": (
            selected[1] if selected is not None else None
        ),
        "minimumSuccessesPerSecond": minimum_successes_per_second,
        "rounds": round_summaries,
    }


def _report_plan() -> dict[str, object]:
    return {
        "roundCount": 3,
        "warmupCount": 100,
        "sampleCount": 2_000,
        "throughputDurationSeconds": 30,
        "throughputConcurrency": 16,
        "serverMaxThreads": 32,
        "throughputBucketSeconds": 1,
        "requestTimeoutSeconds": 5,
        "p99Method": "nearest-rank-ceil",
        "clock": "monotonic_ns",
        "httpConnectionPolicy": "connection-close",
        "admissionRateLimitPolicy": (
            "benchmark-only-capacity-10000-refill-10000-per-second"
        ),
        "fixtures": {
            "activityQuotaStateRows": 1_000,
            "telemetryRequestAggregateRows": 4_000,
            "telemetryMatchingInWindowRows": 1_000,
            "telemetryMatchingOutsideWindowRows": 1_000,
            "telemetryOtherModelRows": 1_000,
            "telemetryOtherProviderRows": 1_000,
            "cacheEntries": 1_000,
            "hotCacheEntryIndex": 500,
            "cachePrefixesPerEntry": 0,
            "shouldSendBodyBytes": 82,
            "responsesBodyBytes": 4_096,
            "providerOutputTextUtf8Bytes": 4_096,
        },
    }


_PRIVACY_REPORT = {
    "allowlistOnly": True,
    "containsLocalPaths": False,
    "containsProviderIdentity": False,
    "containsRequestOrResponse": False,
    "containsCredentials": False,
    "containsProcessIdentifiers": False,
    "realCredentialReads": 0,
    "realProviderNetworkCalls": 0,
}


def _overall_status(statuses: Sequence[object]) -> str:
    if any(status == "invalid" for status in statuses):
        return "invalid"
    if any(status == "fail" for status in statuses):
        return "fail"
    return "pass"


def build_performance_report(
    *,
    fixture: PerformanceFixture,
    captured_at: str,
    source_commit: str,
    source_tree_state: str,
    reference_machine: ReferenceMachine,
    should_send_rounds: Sequence[LatencyRound],
    proxy_overhead_rounds: Sequence[PairedLatencyRound],
    cache_e2e_rounds: Sequence[LatencyRound],
    cache_core_rounds: Sequence[LatencyRound],
    throughput_rounds: Sequence[ThroughputRound],
) -> dict[str, object]:
    """Build one aggregate-only report and validate it before publication."""

    if type(fixture) is not PerformanceFixture or type(reference_machine) is not ReferenceMachine:
        raise ValueError("invalid performance report")
    should_send = summarize_latency_rounds(
        should_send_rounds,
        required_rounds=fixture.round_count,
        required_warmups=fixture.warmup_count,
        required_samples=fixture.sample_count,
        threshold_ns=fixture.should_send_p99_ns,
    )
    proxy_overhead = summarize_paired_latency_rounds(
        proxy_overhead_rounds,
        required_rounds=fixture.round_count,
        required_warmups=fixture.warmup_count,
        required_samples=fixture.sample_count,
        threshold_ns=fixture.proxy_overhead_p99_ns,
    )
    cache_e2e = summarize_latency_rounds(
        cache_e2e_rounds,
        required_rounds=fixture.round_count,
        required_warmups=fixture.warmup_count,
        required_samples=fixture.sample_count,
        threshold_ns=fixture.cache_core_lookup_p99_ns,
    )
    cache_core = summarize_latency_rounds(
        cache_core_rounds,
        required_rounds=fixture.round_count,
        required_warmups=fixture.warmup_count,
        required_samples=fixture.sample_count,
        threshold_ns=fixture.cache_core_lookup_p99_ns,
    )
    throughput = summarize_throughput_rounds(
        throughput_rounds,
        required_rounds=fixture.round_count,
        required_duration_seconds=fixture.throughput_duration_seconds,
        minimum_successes_per_second=(
            fixture.throughput_minimum_successes_per_second
        ),
        required_warmups=fixture.warmup_count,
    )
    cache = {
        "status": cache_core["status"],
        "e2eAuthenticatedLoopback": {
            "informationalOnly": True,
            "measurement": cache_e2e,
        },
        "coreLookup": cache_core,
    }
    statuses = (
        should_send["status"],
        proxy_overhead["status"],
        cache["status"],
        throughput["status"],
    )
    report: dict[str, object] = {
        "schemaVersion": 1,
        "reportKind": "openusage.gatewayPerformance",
        "measurementVersion": "gateway-performance-v1",
        "evidenceClass": "diagnostic",
        "capturedAt": captured_at,
        "sourceCommit": source_commit,
        "sourceTreeState": source_tree_state,
        "referenceMachine": reference_machine.payload(),
        "plan": _report_plan(),
        "scenarios": {
            "shouldSendAuthenticatedLoopback": should_send,
            "responsesProxyOverheadPaired": proxy_overhead,
            "responsesExactCacheHit": cache,
            "responsesThroughput": throughput,
        },
        "privacy": dict(_PRIVACY_REPORT),
        "overallStatus": _overall_status(statuses),
    }
    validate_performance_report(report)
    return report


def _report_object(value: object, keys: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise ValueError("invalid performance report")
    return value


def _report_status(value: object) -> bool:
    return value in {"pass", "fail", "invalid"}


def _latency_report_valid(
    value: object,
    *,
    expected_threshold_ns: int,
) -> bool:
    try:
        summary = _report_object(
            value,
            frozenset(
                {
                    "rounds",
                    "selectedRound",
                    "status",
                    "thresholdNs",
                    "worstP99Ns",
                }
            ),
        )
        if (
            summary["thresholdNs"] != expected_threshold_ns
            or not _report_status(summary["status"])
            or type(summary["rounds"]) is not list
            or len(summary["rounds"]) != 3
        ):
            return False
        valid_p99: list[tuple[int, int]] = []
        statuses: list[str] = []
        for number, raw_round in enumerate(summary["rounds"], start=1):
            item = _report_object(
                raw_round,
                frozenset(
                    {
                        "attemptCount",
                        "clockRegressionCount",
                        "errorCount",
                        "p99Ns",
                        "rateLimitCount",
                        "round",
                        "sampleCount",
                        "status",
                        "timeoutCount",
                        "warmupCount",
                        "warmupErrorCount",
                    }
                ),
            )
            integers = (
                item["attemptCount"],
                item["clockRegressionCount"],
                item["errorCount"],
                item["rateLimitCount"],
                item["sampleCount"],
                item["timeoutCount"],
                item["warmupCount"],
                item["warmupErrorCount"],
            )
            if (
                item["round"] != number
                or any(not _nonnegative_integer(field) for field in integers)
                or item["attemptCount"]
                != item["sampleCount"]
                + item["errorCount"]
                + item["clockRegressionCount"]
                or item["timeoutCount"] + item["rateLimitCount"]
                > item["errorCount"]
                or (
                    item["p99Ns"] is not None
                    and not _nonnegative_integer(item["p99Ns"])
                )
                or (item["sampleCount"] == 0) != (item["p99Ns"] is None)
            ):
                return False
            contract_valid = (
                item["warmupCount"] == 100
                and item["warmupErrorCount"] == 0
                and item["attemptCount"] == 2_000
                and item["clockRegressionCount"] == 0
            )
            expected_status = (
                "invalid"
                if not contract_valid
                else "fail"
                if item["errorCount"]
                or item["p99Ns"] is None
                or item["p99Ns"] >= expected_threshold_ns
                else "pass"
            )
            if item["status"] != expected_status:
                return False
            statuses.append(expected_status)
            if contract_valid and item["p99Ns"] is not None:
                valid_p99.append((number, item["p99Ns"]))
        selected = max(
            valid_p99,
            key=lambda candidate: (candidate[1], -candidate[0]),
            default=None,
        )
        expected_summary_status = _overall_status(statuses)
        return (
            summary["selectedRound"]
            == (selected[0] if selected is not None else None)
            and summary["worstP99Ns"]
            == (selected[1] if selected is not None else None)
            and summary["status"] == expected_summary_status
        )
    except (KeyError, TypeError, ValueError):
        return False


def _paired_report_valid(value: object) -> bool:
    try:
        summary = _report_object(
            value,
            frozenset(
                {
                    "rounds",
                    "selectedRound",
                    "status",
                    "thresholdNs",
                    "worstP99DeltaNs",
                }
            ),
        )
        threshold = 200_000_000
        if (
            summary["thresholdNs"] != threshold
            or type(summary["rounds"]) is not list
            or len(summary["rounds"]) != 3
        ):
            return False
        valid_p99: list[tuple[int, int]] = []
        statuses: list[str] = []
        for number, raw_round in enumerate(summary["rounds"], start=1):
            item = _report_object(
                raw_round,
                frozenset(
                    {
                        "attemptCount",
                        "clockRegressionCount",
                        "errorCount",
                        "maxDeltaNs",
                        "medianDeltaNs",
                        "minDeltaNs",
                        "negativePairCount",
                        "p99DeltaNs",
                        "rateLimitCount",
                        "round",
                        "status",
                        "successfulPairCount",
                        "timeoutCount",
                        "warmupCount",
                        "warmupErrorCount",
                    }
                ),
            )
            counts = (
                item["attemptCount"],
                item["clockRegressionCount"],
                item["errorCount"],
                item["negativePairCount"],
                item["rateLimitCount"],
                item["successfulPairCount"],
                item["timeoutCount"],
                item["warmupCount"],
                item["warmupErrorCount"],
            )
            stats = (
                item["minDeltaNs"],
                item["medianDeltaNs"],
                item["p99DeltaNs"],
                item["maxDeltaNs"],
            )
            if (
                item["round"] != number
                or any(not _nonnegative_integer(field) for field in counts)
                or item["successfulPairCount"] + item["errorCount"]
                + item["clockRegressionCount"]
                != item["attemptCount"]
                or item["negativePairCount"] > item["successfulPairCount"]
                or item["timeoutCount"] + item["rateLimitCount"]
                > item["errorCount"]
                or any(field is not None and type(field) is not int for field in stats)
                or (item["successfulPairCount"] == 0)
                != all(field is None for field in stats)
                or (
                    item["successfulPairCount"] > 0
                    and not (
                        item["minDeltaNs"]
                        <= item["medianDeltaNs"]
                        <= item["p99DeltaNs"]
                        <= item["maxDeltaNs"]
                    )
                )
            ):
                return False
            contract_valid = (
                item["warmupCount"] == 100
                and item["warmupErrorCount"] == 0
                and item["attemptCount"] == 2_000
                and item["clockRegressionCount"] == 0
            )
            expected_status = (
                "invalid"
                if not contract_valid
                else "fail"
                if item["errorCount"]
                or item["p99DeltaNs"] is None
                or item["p99DeltaNs"] >= threshold
                else "pass"
            )
            if item["status"] != expected_status:
                return False
            statuses.append(expected_status)
            if contract_valid and item["p99DeltaNs"] is not None:
                valid_p99.append((number, item["p99DeltaNs"]))
        selected = max(
            valid_p99,
            key=lambda candidate: (candidate[1], -candidate[0]),
            default=None,
        )
        return (
            summary["selectedRound"]
            == (selected[0] if selected is not None else None)
            and summary["worstP99DeltaNs"]
            == (selected[1] if selected is not None else None)
            and summary["status"] == _overall_status(statuses)
        )
    except (KeyError, TypeError, ValueError):
        return False


def _throughput_report_valid(value: object) -> bool:
    try:
        summary = _report_object(
            value,
            frozenset(
                {
                    "minimumCompleteBucketSuccesses",
                    "minimumSuccessesPerSecond",
                    "rounds",
                    "selectedRound",
                    "status",
                }
            ),
        )
        if (
            summary["minimumSuccessesPerSecond"] != 100
            or type(summary["rounds"]) is not list
            or len(summary["rounds"]) != 3
        ):
            return False
        valid_minimums: list[tuple[int, int]] = []
        statuses: list[str] = []
        for number, raw_round in enumerate(summary["rounds"], start=1):
            item = _report_object(
                raw_round,
                frozenset(
                    {
                        "bucketDurationSeconds",
                        "clockRegressionCount",
                        "completeBucketCount",
                        "completeBucketSuccesses",
                        "errorCount",
                        "lateCompletionCount",
                        "minCompleteBucketSuccesses",
                        "rateLimitCount",
                        "round",
                        "status",
                        "timeoutCount",
                        "totalCompleted",
                        "totalSuccessfulInWindow",
                        "warmupCount",
                        "warmupErrorCount",
                    }
                ),
            )
            buckets = item["completeBucketSuccesses"]
            count_fields = (
                item["clockRegressionCount"],
                item["completeBucketCount"],
                item["errorCount"],
                item["lateCompletionCount"],
                item["minCompleteBucketSuccesses"],
                item["rateLimitCount"],
                item["timeoutCount"],
                item["totalCompleted"],
                item["totalSuccessfulInWindow"],
                item["warmupCount"],
                item["warmupErrorCount"],
            )
            if (
                item["round"] != number
                or item["bucketDurationSeconds"] != 1
                or type(buckets) is not list
                or len(buckets) != item["completeBucketCount"]
                or any(not _nonnegative_integer(field) for field in count_fields)
                or any(not _nonnegative_integer(bucket) for bucket in buckets)
                or not buckets
                or item["minCompleteBucketSuccesses"] != min(buckets)
                or item["totalSuccessfulInWindow"] != sum(buckets)
                or item["timeoutCount"] + item["rateLimitCount"]
                > item["errorCount"]
                or item["totalCompleted"]
                != (
                    item["totalSuccessfulInWindow"]
                    + item["lateCompletionCount"]
                    + item["clockRegressionCount"]
                    + item["errorCount"]
                )
            ):
                return False
            contract_valid = (
                item["warmupCount"] == 100
                and item["warmupErrorCount"] == 0
                and item["completeBucketCount"] == 30
                and item["clockRegressionCount"] == 0
            )
            expected_status = (
                "invalid"
                if not contract_valid
                else "fail"
                if item["errorCount"]
                or item["minCompleteBucketSuccesses"] < 100
                else "pass"
            )
            if item["status"] != expected_status:
                return False
            statuses.append(expected_status)
            if contract_valid:
                valid_minimums.append(
                    (number, item["minCompleteBucketSuccesses"])
                )
        selected = min(
            valid_minimums,
            key=lambda candidate: (candidate[1], candidate[0]),
            default=None,
        )
        return (
            summary["selectedRound"]
            == (selected[0] if selected is not None else None)
            and summary["minimumCompleteBucketSuccesses"]
            == (selected[1] if selected is not None else None)
            and summary["status"] == _overall_status(statuses)
        )
    except (KeyError, TypeError, ValueError):
        return False


def validate_performance_report(payload: object) -> dict[str, object]:
    """Validate the exact aggregate allowlist used by published evidence."""

    try:
        report = _report_object(
            payload,
            frozenset(
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
                }
            ),
        )
        encoded = json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        captured_at = report["capturedAt"]
        captured = datetime.fromisoformat(
            captured_at.replace("Z", "+00:00")
        )
        if (
            len(encoded) > MAX_REPORT_BYTES
            or report["schemaVersion"] != 1
            or type(report["schemaVersion"]) is not int
            or report["reportKind"] != "openusage.gatewayPerformance"
            or report["measurementVersion"] != "gateway-performance-v1"
            or report["evidenceClass"] != "diagnostic"
            or type(captured_at) is not str
            or not captured_at.endswith("Z")
            or captured.utcoffset() != timezone.utc.utcoffset(captured)
            or type(report["sourceCommit"]) is not str
            or _SOURCE_COMMIT.fullmatch(report["sourceCommit"]) is None
            or report["sourceTreeState"] not in {"clean", "dirty", "unknown"}
            or report["plan"] != _report_plan()
            or report["privacy"] != _PRIVACY_REPORT
            or not _report_status(report["overallStatus"])
        ):
            raise ValueError

        machine = _report_object(
            report["referenceMachine"],
            frozenset(
                {
                    "architecture",
                    "cpuModel",
                    "filesystem",
                    "logicalCpuCount",
                    "memoryBytes",
                    "osBuild",
                    "osName",
                    "osVersion",
                    "powerState",
                    "pythonVersion",
                    "storageClass",
                }
            ),
        )
        ReferenceMachine(
            os_name=machine["osName"],
            os_version=machine["osVersion"],
            os_build=machine["osBuild"],
            architecture=machine["architecture"],
            cpu_model=machine["cpuModel"],
            logical_cpu_count=machine["logicalCpuCount"],
            memory_bytes=machine["memoryBytes"],
            storage_class=machine["storageClass"],
            filesystem=machine["filesystem"],
            power_state=machine["powerState"],
            python_version=machine["pythonVersion"],
        )
        scenarios = _report_object(
            report["scenarios"],
            frozenset(
                {
                    "responsesExactCacheHit",
                    "responsesProxyOverheadPaired",
                    "responsesThroughput",
                    "shouldSendAuthenticatedLoopback",
                }
            ),
        )
        cache = _report_object(
            scenarios["responsesExactCacheHit"],
            frozenset({"coreLookup", "e2eAuthenticatedLoopback", "status"}),
        )
        e2e = _report_object(
            cache["e2eAuthenticatedLoopback"],
            frozenset({"informationalOnly", "measurement"}),
        )
        valid_should = _latency_report_valid(
            scenarios["shouldSendAuthenticatedLoopback"],
            expected_threshold_ns=50_000_000,
        )
        valid_proxy = _paired_report_valid(
            scenarios["responsesProxyOverheadPaired"]
        )
        valid_cache = _latency_report_valid(
            cache["coreLookup"],
            expected_threshold_ns=5_000_000,
        )
        valid_e2e = _latency_report_valid(
            e2e["measurement"],
            expected_threshold_ns=5_000_000,
        )
        valid_throughput = _throughput_report_valid(
            scenarios["responsesThroughput"]
        )
        expected_overall = _overall_status(
            (
                scenarios["shouldSendAuthenticatedLoopback"]["status"],
                scenarios["responsesProxyOverheadPaired"]["status"],
                cache["status"],
                scenarios["responsesThroughput"]["status"],
            )
        )
        if (
            not all(
                (
                    valid_should,
                    valid_proxy,
                    valid_cache,
                    valid_e2e,
                    valid_throughput,
                )
            )
            or e2e["informationalOnly"] is not True
            or cache["status"] != cache["coreLookup"]["status"]
            or report["overallStatus"] != expected_overall
        ):
            raise ValueError
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
        UnicodeError,
    ):
        raise ValueError("invalid performance report") from None
    return report


def _fixture_datetime(fixture: PerformanceFixture) -> datetime:
    return datetime.fromisoformat(fixture.clock_utc.replace("Z", "+00:00"))


def _private_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)


def _clone_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        with sqlite3.connect(
            f"file:{source}?mode=ro",
            uri=True,
        ) as source_db, sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db)
    except sqlite3.Error:
        raise RuntimeError("performance fixture clone failed") from None
    _private_file(destination)


class _FixtureTransport:
    def __init__(self, fixture: PerformanceFixture) -> None:
        from openusage_bar.gateway.providers import ProviderResult

        self._result_type = ProviderResult
        self._sse = _fixture_sse(fixture.provider_output_text)
        if len(self._sse) != 4_307:
            raise RuntimeError("performance fixture transport invalid")
        self._calls = 0
        self._lock = threading.Lock()

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    def __call__(
        self,
        _provider_id: str,
        _request_body: bytes,
        **_kwargs: object,
    ) -> object:
        with self._lock:
            self._calls += 1
        return self._result_type(
            status_code=200,
            headers=(("Content-Type", "text/event-stream"),),
            body_chunks=(self._sse,),
        )


class _FixtureTemplates:
    """Build synthetic SQLite facts once, then clone them outside timing."""

    def __init__(self, fixture: PerformanceFixture) -> None:
        self.fixture = fixture
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.activity_path = self.root / "activity.sqlite3"
        self.telemetry_path = self.root / "gateway-telemetry.sqlite3"
        self.cache_path = self.root / "gateway-cache.sqlite3"
        try:
            self._seed_activity()
            self._seed_telemetry()
            self._seed_cache()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self._temporary.cleanup()

    def clone_activity(self, root: Path) -> Path:
        destination = root / "activity" / "activity.sqlite3"
        _clone_sqlite(self.activity_path, destination)
        return destination

    def clone_telemetry(self, root: Path) -> Path:
        destination = root / "telemetry" / "gateway-telemetry.sqlite3"
        _clone_sqlite(self.telemetry_path, destination)
        return destination

    def clone_cache(self, root: Path) -> Path:
        destination = root / "cache" / "gateway-cache.sqlite3"
        _clone_sqlite(self.cache_path, destination)
        return destination

    def _seed_activity(self) -> None:
        from openusage_bar.activity_records import QuotaObservation
        from openusage_bar.activity_store import ActivityStore

        now = _fixture_datetime(self.fixture)
        observed_at = now.isoformat().replace("+00:00", "Z")
        resets_at = (now + timedelta(hours=1)).isoformat().replace(
            "+00:00",
            "Z",
        )
        provider = self.fixture.should_send_request["provider"]
        store = ActivityStore(self.activity_path)
        try:
            for index in range(self.fixture.activity_quota_state_rows):
                store.record_quota(
                    QuotaObservation(
                        record_id=f"performance.quota.{index:04d}",
                        observed_at=observed_at,
                        provider_id=provider,
                        account_ref=f"fixture-account-{index:04d}",
                        quota_name="Performance quota",
                        unit="tokens",
                        used="26000",
                        quota_limit="100000",
                        remaining="74000",
                        remaining_ratio=0.74,
                        resets_at=resets_at,
                        period_start=None,
                        period_end=None,
                        state="ok",
                        quality="direct",
                        stale=False,
                        quota_window="daily",
                        applies_to_kind="account",
                    )
                )
            if len(store.quota_states()) != self.fixture.activity_quota_state_rows:
                raise RuntimeError("performance activity fixture invalid")
        finally:
            store.close()

    def _seed_telemetry(self) -> None:
        from openusage_bar.gateway.telemetry import GatewayTelemetryStore

        now = _fixture_datetime(self.fixture)
        provider = self.fixture.should_send_request["provider"]
        model = self.fixture.should_send_request["model"]
        store = GatewayTelemetryStore(
            self.telemetry_path,
            clock=lambda: now,
        )

        def record_group(
            label: str,
            count: int,
            *,
            provider_id: str,
            model_id: str,
            finished_at: datetime,
        ) -> None:
            for index in range(count):
                store.record_request(
                    request_id=f"performance-{label}-{index:04d}",
                    provider_id=provider_id,
                    model_id=model_id,
                    started_at=finished_at - timedelta(milliseconds=1),
                    finished_at=finished_at,
                    status_class="2xx",
                    input_tokens=1,
                    output_tokens=1,
                    latency_ms=1,
                    estimated_cost=None,
                    actual_cost=None,
                    cache_outcome="miss",
                    fallback_count=0,
                    error_code=None,
                )

        try:
            record_group(
                "matching-window",
                self.fixture.telemetry_matching_in_window_rows,
                provider_id=provider,
                model_id=model,
                finished_at=now - timedelta(seconds=1),
            )
            record_group(
                "matching-outside",
                self.fixture.telemetry_matching_outside_window_rows,
                provider_id=provider,
                model_id=model,
                finished_at=now - timedelta(minutes=10),
            )
            record_group(
                "other-model",
                self.fixture.telemetry_other_model_rows,
                provider_id=provider,
                model_id="fixture-model-other",
                finished_at=now - timedelta(seconds=1),
            )
            record_group(
                "other-provider",
                self.fixture.telemetry_other_provider_rows,
                provider_id="anthropic",
                model_id=model,
                finished_at=now - timedelta(seconds=1),
            )
            burn_rate = store.recent_burn_rate(
                provider,
                model,
                window_minutes=5,
                max_rows=1_000,
            )
            if burn_rate is None:
                raise RuntimeError("performance telemetry fixture invalid")
        finally:
            store.close()

    def _seed_cache(self) -> None:
        from openusage_bar.gateway.cache import SQLiteGatewayCache
        from openusage_bar.gateway.runtime import GatewayRuntime

        cache = SQLiteGatewayCache(self.cache_path)
        transport = _FixtureTransport(self.fixture)
        runtime = GatewayRuntime(egress=transport, cache=cache)
        try:
            for index in range(self.fixture.cache_entries):
                response = runtime(self.fixture.response_request(index))
                cache_payload = response.get("cache")
                if (
                    response.get("object") != "gateway.response"
                    or response.get("status") != "complete"
                    or type(cache_payload) is not dict
                    or cache_payload.get("outcome") != "miss"
                ):
                    raise RuntimeError("performance cache fixture invalid")
            if (
                cache.stats().entries != self.fixture.cache_entries
                or transport.calls != self.fixture.cache_entries
            ):
                raise RuntimeError("performance cache fixture invalid")
        finally:
            cache.close()


@contextmanager
def _running_gateway_server(
    router: object,
    fixture: PerformanceFixture,
    root: Path,
) -> Iterator[tuple[int, str]]:
    from openusage_bar.gateway.server import create_gateway_server

    token = "g" * 48
    server = create_gateway_server(
        router,
        host="127.0.0.1",
        port=0,
        bearer_token=token,
        token_path=root / "gateway.token",
        rate_limit_capacity=fixture.admission_rate_limit_capacity,
        rate_limit_refill_per_second=(
            fixture.admission_rate_limit_refill_per_second
        ),
        max_threads=fixture.server_max_threads,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(fixture.request_timeout_seconds + 1)
        if thread.is_alive():
            raise RuntimeError("performance Gateway shutdown failed")


def _should_send_operation(
    fixture: PerformanceFixture,
    port: int,
    token: str,
) -> MeasurementOutcome:
    status, payload = _authenticated_json_request(
        port=port,
        token=token,
        target="/gateway/v1/should-send",
        payload=fixture.should_send_request,
        timeout_seconds=fixture.request_timeout_seconds,
    )
    if status == 429:
        return MeasurementOutcome(ok=False, rate_limited=True)
    return MeasurementOutcome(
        ok=(
            status == 200
            and payload.get("decision") in {"yes", "defer", "no"}
            and type(payload.get("details")) is dict
        )
    )


def _responses_operation(
    fixture: PerformanceFixture,
    port: int,
    token: str,
    payload: dict[str, object],
    *,
    expected_cache_outcome: str | None = None,
) -> MeasurementOutcome:
    status, response = _authenticated_json_request(
        port=port,
        token=token,
        target="/gateway/v1/responses",
        payload=payload,
        timeout_seconds=fixture.request_timeout_seconds,
    )
    if status == 429:
        return MeasurementOutcome(ok=False, rate_limited=True)
    cache = response.get("cache")
    cache_matches = expected_cache_outcome is None or (
        type(cache) is dict and cache.get("outcome") == expected_cache_outcome
    )
    return MeasurementOutcome(
        ok=(
            status == 200
            and response.get("object") == "gateway.response"
            and response.get("status") == "complete"
            and cache_matches
        )
    )


def _direct_fixture_operation(
    transport: _FixtureTransport,
    provider_id: str,
    request_body: bytes,
) -> MeasurementOutcome:
    result = transport(provider_id, request_body)
    return MeasurementOutcome(
        ok=(
            getattr(result, "status_code", None) == 200
            and getattr(result, "headers", None)
            == (("Content-Type", "text/event-stream"),)
            and type(getattr(result, "body_chunks", None)) is tuple
            and len(result.body_chunks) == 1
            and len(result.body_chunks[0]) == 4_307
        )
    )


def _hot_cache_keys(
    fixture: PerformanceFixture,
) -> tuple[dict[str, object], object]:
    from openusage_bar.gateway.cache import CacheKeySet
    from openusage_bar.gateway.ingress import parse_gateway_request
    from openusage_bar.gateway.pii import Redactor

    payload = fixture.response_request(fixture.hot_cache_entry_index)
    request = parse_gateway_request(payload)
    redacted = Redactor().redact(request.request_body.decode("utf-8"))
    keys = CacheKeySet.from_request(
        provider_id=request.provider_id,
        model=request.model,
        parameters={"stream": request.stream},
        request=redacted,
        prefixes=(),
        stream=request.stream,
    )
    return payload, keys


def _measure_should_send_round(
    templates: _FixtureTemplates,
    monotonic_ns: Callable[[], int],
) -> LatencyRound:
    from openusage_bar.activity_store import ActivityStore
    from openusage_bar.gateway.api import GatewayRouter
    from openusage_bar.gateway.contracts import GatewayMode
    from openusage_bar.gateway.policy import SnapshottingShouldSendEvaluator
    from openusage_bar.gateway.telemetry import GatewayTelemetryStore
    from openusage_bar.query import QueryService

    fixture = templates.fixture
    now = _fixture_datetime(fixture)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        activity = ActivityStore(templates.clone_activity(root))
        telemetry = GatewayTelemetryStore(
            templates.clone_telemetry(root),
            clock=lambda: now,
        )
        query = QueryService(activity, clock=lambda: now)
        def burn_rate(provider_id: str, model_id: str) -> float | None:
            return telemetry.recent_burn_rate(
                provider_id,
                model_id,
                window_minutes=5,
                max_rows=1_000,
            )

        should_send = SnapshottingShouldSendEvaluator(
            capacity=query.capacity,
            burn_rate=burn_rate,
            ttl_seconds=10.0,
        )

        router = GatewayRouter(
            mode=GatewayMode.ADVISE,
            policy=should_send,
            proxy=None,
        )
        try:
            with _running_gateway_server(router, fixture, root) as (port, token):
                return measure_latency_round(
                    lambda: _should_send_operation(fixture, port, token),
                    warmup_attempts=fixture.warmup_count,
                    sample_attempts=fixture.sample_count,
                    monotonic_ns=monotonic_ns,
                )
        finally:
            telemetry.close()
            activity.close()


def _measure_proxy_overhead_round(
    templates: _FixtureTemplates,
    monotonic_ns: Callable[[], int],
) -> PairedLatencyRound:
    from openusage_bar.gateway.api import GatewayRouter
    from openusage_bar.gateway.contracts import GatewayMode
    from openusage_bar.gateway.ingress import parse_gateway_request
    from openusage_bar.gateway.runtime import GatewayRuntime
    from openusage_bar.gateway.telemetry import GatewayTelemetryStore

    fixture = templates.fixture
    now = _fixture_datetime(fixture)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        telemetry = GatewayTelemetryStore(
            templates.clone_telemetry(root),
            clock=lambda: now,
        )
        transport = _FixtureTransport(fixture)
        payload = fixture.response_request(fixture.hot_cache_entry_index)
        request = parse_gateway_request(payload)
        runtime = GatewayRuntime(egress=transport, telemetry=telemetry)
        router = GatewayRouter(
            mode=GatewayMode.GATEWAY,
            policy=lambda _request: None,
            proxy=runtime,
        )
        try:
            with _running_gateway_server(router, fixture, root) as (port, token):
                return measure_paired_latency_round(
                    lambda: _direct_fixture_operation(
                        transport,
                        request.provider_id,
                        request.request_body,
                    ),
                    lambda: _responses_operation(
                        fixture,
                        port,
                        token,
                        payload,
                        expected_cache_outcome="miss",
                    ),
                    warmup_attempts=fixture.warmup_count,
                    sample_attempts=fixture.sample_count,
                    monotonic_ns=monotonic_ns,
                )
        finally:
            telemetry.close()


def _measure_cache_rounds(
    templates: _FixtureTemplates,
    monotonic_ns: Callable[[], int],
) -> tuple[LatencyRound, LatencyRound]:
    from openusage_bar.gateway.api import GatewayRouter
    from openusage_bar.gateway.cache import ExactHit, SQLiteGatewayCache
    from openusage_bar.gateway.contracts import GatewayMode
    from openusage_bar.gateway.runtime import GatewayRuntime
    from openusage_bar.gateway.telemetry import GatewayTelemetryStore

    fixture = templates.fixture
    now = _fixture_datetime(fixture)
    payload, keys = _hot_cache_keys(fixture)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        cache = SQLiteGatewayCache(templates.clone_cache(root))
        telemetry = GatewayTelemetryStore(
            templates.clone_telemetry(root),
            clock=lambda: now,
        )
        transport = _FixtureTransport(fixture)
        runtime = GatewayRuntime(
            egress=transport,
            cache=cache,
            telemetry=telemetry,
        )
        router = GatewayRouter(
            mode=GatewayMode.GATEWAY,
            policy=lambda _request: None,
            proxy=runtime,
        )
        try:
            with _running_gateway_server(router, fixture, root) as (port, token):
                preflight = _responses_operation(
                    fixture,
                    port,
                    token,
                    payload,
                    expected_cache_outcome="exact_hit",
                )
                if not preflight.ok or transport.calls != 0:
                    raise RuntimeError("performance exact-hit fixture invalid")
                e2e = measure_latency_round(
                    lambda: _responses_operation(
                        fixture,
                        port,
                        token,
                        payload,
                        expected_cache_outcome="exact_hit",
                    ),
                    warmup_attempts=fixture.warmup_count,
                    sample_attempts=fixture.sample_count,
                    monotonic_ns=monotonic_ns,
                )
                if transport.calls != 0:
                    raise RuntimeError("performance exact-hit fixture invalid")
        finally:
            telemetry.close()
            cache.close()

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        cache = SQLiteGatewayCache(templates.clone_cache(root))

        def lookup() -> MeasurementOutcome:
            return MeasurementOutcome(ok=type(cache.lookup(keys)) is ExactHit)

        try:
            if not lookup().ok:
                raise RuntimeError("performance exact-hit fixture invalid")
            core = measure_latency_round(
                lookup,
                warmup_attempts=fixture.warmup_count,
                sample_attempts=fixture.sample_count,
                monotonic_ns=monotonic_ns,
            )
        finally:
            cache.close()
    return e2e, core


def _measure_throughput_window(
    operation: Callable[[], MeasurementOutcome],
    fixture: PerformanceFixture,
    monotonic_ns: Callable[[], int],
) -> ThroughputRound:
    warmup_count = 0
    warmup_errors = 0
    for _ in range(fixture.warmup_count):
        outcome = _operation_outcome(operation)
        if outcome.ok:
            warmup_count += 1
        else:
            warmup_errors += 1

    ready = 0
    ready_condition = threading.Condition()
    start_event = threading.Event()
    result_lock = threading.Lock()
    successful_completion_ns: list[int] = []
    errors = 0
    timeouts = 0
    rate_limits = 0
    window_start = 0
    window_end = 0

    def worker() -> None:
        nonlocal ready, errors, timeouts, rate_limits
        with ready_condition:
            ready += 1
            ready_condition.notify_all()
        start_event.wait()
        while True:
            current = monotonic_ns()
            if current >= window_end:
                return
            outcome = _operation_outcome(operation)
            completed = monotonic_ns()
            with result_lock:
                if outcome.ok:
                    successful_completion_ns.append(completed)
                else:
                    errors += 1
                    timeouts += int(outcome.timeout)
                    rate_limits += int(outcome.rate_limited)

    with ThreadPoolExecutor(
        max_workers=fixture.throughput_concurrency,
        thread_name_prefix="gateway-performance",
    ) as executor:
        futures = [
            executor.submit(worker)
            for _ in range(fixture.throughput_concurrency)
        ]
        with ready_condition:
            ready_condition.wait_for(
                lambda: ready == fixture.throughput_concurrency,
                timeout=fixture.request_timeout_seconds,
            )
        if ready != fixture.throughput_concurrency:
            start_event.set()
            raise RuntimeError("performance throughput workers unavailable")
        window_start = monotonic_ns()
        if not _nonnegative_integer(window_start):
            start_event.set()
            raise RuntimeError("performance clock invalid")
        window_end = (
            window_start
            + fixture.throughput_duration_seconds * 1_000_000_000
        )
        start_event.set()
        for future in futures:
            try:
                future.result(
                    timeout=(
                        fixture.throughput_duration_seconds
                        + fixture.request_timeout_seconds
                        + 5
                    )
                )
            except TimeoutError:
                raise RuntimeError("performance throughput worker timed out") from None

    return ThroughputRound(
        window_start_ns=window_start,
        duration_seconds=fixture.throughput_duration_seconds,
        successful_completion_ns=tuple(successful_completion_ns),
        warmup_count=warmup_count,
        warmup_error_count=warmup_errors,
        error_count=errors,
        timeout_count=timeouts,
        rate_limit_count=rate_limits,
    )


def _measure_throughput_round(
    templates: _FixtureTemplates,
    monotonic_ns: Callable[[], int],
) -> ThroughputRound:
    from openusage_bar.gateway.api import GatewayRouter
    from openusage_bar.gateway.contracts import GatewayMode
    from openusage_bar.gateway.runtime import GatewayRuntime
    from openusage_bar.gateway.telemetry import GatewayTelemetryStore

    fixture = templates.fixture
    now = _fixture_datetime(fixture)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        telemetry = GatewayTelemetryStore(
            templates.clone_telemetry(root),
            clock=lambda: now,
        )
        transport = _FixtureTransport(fixture)
        payload = fixture.response_request(fixture.hot_cache_entry_index)
        runtime = GatewayRuntime(egress=transport, telemetry=telemetry)
        router = GatewayRouter(
            mode=GatewayMode.GATEWAY,
            policy=lambda _request: None,
            proxy=runtime,
        )
        try:
            with _running_gateway_server(router, fixture, root) as (port, token):
                return _measure_throughput_window(
                    lambda: _responses_operation(
                        fixture,
                        port,
                        token,
                        payload,
                        expected_cache_outcome="miss",
                    ),
                    fixture,
                    monotonic_ns,
                )
        finally:
            telemetry.close()


@dataclass(frozen=True, slots=True)
class PerformanceObservations:
    should_send_rounds: tuple[LatencyRound, ...]
    proxy_overhead_rounds: tuple[PairedLatencyRound, ...]
    cache_e2e_rounds: tuple[LatencyRound, ...]
    cache_core_rounds: tuple[LatencyRound, ...]
    throughput_rounds: tuple[ThroughputRound, ...]


def run_performance_measurements(
    fixture: PerformanceFixture,
    *,
    monotonic_ns: Callable[[], int] = time.monotonic_ns,
    progress: Callable[[str], None] | None = None,
) -> PerformanceObservations:
    """Run the frozen full workload using only synthetic local fixtures."""

    if (
        type(fixture) is not PerformanceFixture
        or not callable(monotonic_ns)
        or (progress is not None and not callable(progress))
    ):
        raise ValueError("invalid performance measurement")
    emit = progress or (lambda _stage: None)
    emit("fixture-start")
    templates = _FixtureTemplates(fixture)
    emit("fixture-complete")
    try:
        should_send_list: list[LatencyRound] = []
        proxy_overhead_list: list[PairedLatencyRound] = []
        cache_pairs_list: list[tuple[LatencyRound, LatencyRound]] = []
        throughput_list: list[ThroughputRound] = []
        for number in range(1, fixture.round_count + 1):
            emit(f"should-send-round-{number}-start")
            should_send_list.append(
                _measure_should_send_round(templates, monotonic_ns)
            )
            emit(f"should-send-round-{number}-complete")
        for number in range(1, fixture.round_count + 1):
            emit(f"proxy-overhead-round-{number}-start")
            proxy_overhead_list.append(
                _measure_proxy_overhead_round(templates, monotonic_ns)
            )
            emit(f"proxy-overhead-round-{number}-complete")
        for number in range(1, fixture.round_count + 1):
            emit(f"cache-round-{number}-start")
            cache_pairs_list.append(
                _measure_cache_rounds(templates, monotonic_ns)
            )
            emit(f"cache-round-{number}-complete")
        for number in range(1, fixture.round_count + 1):
            emit(f"throughput-round-{number}-start")
            throughput_list.append(
                _measure_throughput_round(templates, monotonic_ns)
            )
            emit(f"throughput-round-{number}-complete")
    finally:
        templates.close()
    should_send = tuple(should_send_list)
    proxy_overhead = tuple(proxy_overhead_list)
    cache_pairs = tuple(cache_pairs_list)
    throughput = tuple(throughput_list)
    return PerformanceObservations(
        should_send_rounds=should_send,
        proxy_overhead_rounds=proxy_overhead,
        cache_e2e_rounds=tuple(pair[0] for pair in cache_pairs),
        cache_core_rounds=tuple(pair[1] for pair in cache_pairs),
        throughput_rounds=throughput,
    )


def _command_value(command: Sequence[str]) -> str | None:
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _safe_machine_value(value: object) -> str:
    text = value if type(value) is str else ""
    text = re.sub(r"[^A-Za-z0-9 ._()+@-]", " ", text)
    text = " ".join(text.split())[:128].strip()
    return text if _machine_text(text) else "unknown"


def _memory_bytes() -> int:
    if sys.platform == "win32":
        class MemoryStatus(ctypes.Structure):
            _fields_ = (
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            )

        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.total_physical)
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        if page_size > 0 and pages > 0:
            return page_size * pages
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    raise RuntimeError("reference machine memory unavailable")


def _cpu_model() -> str:
    value: str | None = None
    if sys.platform == "darwin":
        value = _command_value(("sysctl", "-n", "machdep.cpu.brand_string"))
    elif sys.platform == "win32":
        value = platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER")
    else:
        try:
            for line in Path("/proc/cpuinfo").read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines():
                if line.casefold().startswith("model name") and ":" in line:
                    value = line.split(":", 1)[1].strip()
                    break
        except OSError:
            value = None
    return _safe_machine_value(value or platform.processor() or platform.machine())


def _filesystem(target: Path) -> str:
    if sys.platform == "win32":
        root = str(target.resolve().anchor)
        buffer = ctypes.create_unicode_buffer(64)
        try:
            ok = ctypes.windll.kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(root),
                None,
                0,
                None,
                None,
                None,
                buffer,
                len(buffer),
            )
        except (AttributeError, OSError):
            ok = False
        return _safe_machine_value(buffer.value.casefold() if ok else "unknown")
    if sys.platform == "darwin":
        df_output = _command_value(("df", "-P", str(target.resolve())))
        lines = tuple(
            line for line in (df_output or "").splitlines() if line.strip()
        )
        fields = lines[-1].split(maxsplit=5) if len(lines) >= 2 else []
        if len(fields) == 6:
            disk_info = _command_value(("diskutil", "info", fields[-1]))
            for line in (disk_info or "").splitlines():
                name, separator, value = line.partition(":")
                if separator and name.strip().casefold() in {
                    "file system personality",
                    "type (bundle)",
                }:
                    return _safe_machine_value(value.strip().casefold())
        return "unknown"
    return _safe_machine_value(
        (
            _command_value(("stat", "-f", "-c", "%T", str(target)))
            or "unknown"
        ).casefold()
    )


def _storage_class() -> str:
    if sys.platform == "darwin":
        value = _command_value(("diskutil", "info", "/"))
        if value is not None:
            for line in value.splitlines():
                normalized = line.strip().casefold()
                if normalized.startswith("solid state:"):
                    return "ssd" if normalized.endswith("yes") else "hdd"
    return "unknown"


def _power_state() -> str:
    if sys.platform == "darwin":
        value = _command_value(("pmset", "-g", "batt"))
        if value is not None:
            lowered = value.casefold()
            if "ac power" in lowered:
                return "ac"
            if "battery power" in lowered:
                return "battery"
    elif sys.platform == "win32":
        class PowerStatus(ctypes.Structure):
            _fields_ = (
                ("ac_line_status", ctypes.c_ubyte),
                ("battery_flag", ctypes.c_ubyte),
                ("battery_life_percent", ctypes.c_ubyte),
                ("system_status_flag", ctypes.c_ubyte),
                ("battery_life_time", ctypes.c_ulong),
                ("battery_full_life_time", ctypes.c_ulong),
            )

        status = PowerStatus()
        try:
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                return {0: "battery", 1: "ac"}.get(
                    int(status.ac_line_status),
                    "unknown",
                )
        except (AttributeError, OSError):
            pass
    else:
        try:
            supplies = Path("/sys/class/power_supply")
            for online in supplies.glob("*/online"):
                if online.read_text(encoding="ascii").strip() == "1":
                    return "ac"
            for status in supplies.glob("*/status"):
                if status.read_text(encoding="ascii").strip().casefold() in {
                    "charging",
                    "discharging",
                    "full",
                }:
                    return "battery"
        except OSError:
            pass
    return "unknown"


def detect_reference_machine(
    *,
    storage_class: str = "auto",
    filesystem: str = "auto",
    power_state: str = "auto",
) -> ReferenceMachine:
    """Detect only bounded, path-free machine metadata for interpretation."""

    system = platform.system()
    if system == "Darwin":
        os_name = "macos"
        os_version = platform.mac_ver()[0] or "unknown"
        os_build = _command_value(("sw_vers", "-buildVersion")) or "unknown"
    elif system == "Windows":
        os_name = "windows"
        os_version = platform.release() or "unknown"
        os_build = platform.version() or "unknown"
    elif system == "Linux":
        os_name = "linux"
        try:
            release = platform.freedesktop_os_release()
        except OSError:
            release = {}
        os_version = release.get("VERSION_ID") or platform.release() or "unknown"
        os_build = platform.release() or "unknown"
    else:
        raise RuntimeError("reference machine operating system unsupported")
    resolved_storage = _storage_class() if storage_class == "auto" else storage_class
    resolved_filesystem = (
        _filesystem(Path(tempfile.gettempdir()))
        if filesystem == "auto"
        else _safe_machine_value(filesystem.casefold())
    )
    resolved_power = _power_state() if power_state == "auto" else power_state
    return ReferenceMachine(
        os_name=os_name,
        os_version=_safe_machine_value(os_version),
        os_build=_safe_machine_value(os_build),
        architecture=_safe_machine_value(platform.machine() or "unknown"),
        cpu_model=_cpu_model(),
        logical_cpu_count=os.cpu_count() or 1,
        memory_bytes=_memory_bytes(),
        storage_class=resolved_storage,
        filesystem=resolved_filesystem,
        power_state=resolved_power,
        python_version=platform.python_version(),
    )


def detect_source_provenance(repo_root: Path) -> dict[str, str]:
    """Bind performance evidence to the repository's real HEAD and worktree."""

    if not isinstance(repo_root, Path):
        raise ValueError("source repository unavailable")
    try:
        resolved_root = repo_root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise OSError
        head = subprocess.run(
            ("git", "-C", str(resolved_root), "rev-parse", "--verify", "HEAD"),
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
        status = subprocess.run(
            (
                "git",
                "-C",
                str(resolved_root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ),
            capture_output=True,
            check=False,
            text=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise ValueError("source repository unavailable") from None
    source_commit = head.stdout.strip()
    if head.returncode != 0 or _SOURCE_COMMIT.fullmatch(source_commit) is None:
        raise ValueError("source repository unavailable")
    if status.returncode != 0:
        raise ValueError("source repository unavailable")
    return {
        "sourceCommit": source_commit,
        "sourceTreeState": "dirty" if status.stdout else "clean",
    }


def run_performance_report(
    *,
    fixture: PerformanceFixture,
    source_commit: str,
    source_tree_state: str,
    reference_machine: ReferenceMachine,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    observations = run_performance_measurements(
        fixture,
        progress=progress,
    )
    captured_at = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    return build_performance_report(
        fixture=fixture,
        captured_at=captured_at,
        source_commit=source_commit,
        source_tree_state=source_tree_state,
        reference_machine=reference_machine,
        should_send_rounds=observations.should_send_rounds,
        proxy_overhead_rounds=observations.proxy_overhead_rounds,
        cache_e2e_rounds=observations.cache_e2e_rounds,
        cache_core_rounds=observations.cache_core_rounds,
        throughput_rounds=observations.throughput_rounds,
    )


def _write_report(path: Path, report: dict[str, object]) -> None:
    encoded = (
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_REPORT_BYTES:
        raise ValueError("invalid performance report")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise OSError
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".gateway-performance-",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            _private_file(temporary)
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    except OSError:
        raise ValueError("performance report unavailable") from None


def _read_report(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or not 0 < metadata.st_size <= MAX_REPORT_BYTES
        ):
            raise OSError
        raw = path.read_bytes()
    except OSError:
        raise ValueError("performance report unavailable") from None
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (
        json.JSONDecodeError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
        UnicodeError,
    ):
        raise ValueError("invalid performance report") from None
    return validate_performance_report(payload)


def _parser() -> argparse.ArgumentParser:
    default_fixture = (
        Path(__file__).resolve().parents[1]
        / "tests/fixtures/gateway/performance-v1.json"
    )
    parser = argparse.ArgumentParser(
        description="Measure privacy-safe OpenUsage Gateway performance evidence.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    smoke = commands.add_parser("smoke")
    smoke.add_argument("--fixture", type=Path, default=default_fixture)
    run = commands.add_parser("run")
    run.add_argument("--fixture", type=Path, default=default_fixture)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--source-commit", required=True)
    run.add_argument(
        "--source-tree-state",
        choices=("auto", "clean", "dirty", "unknown"),
        default="auto",
    )
    run.add_argument(
        "--storage-class",
        choices=("auto", "ssd", "hdd", "unknown"),
        default="auto",
    )
    run.add_argument("--filesystem", default="auto")
    run.add_argument(
        "--power-state",
        choices=("auto", "ac", "battery", "unknown"),
        default="auto",
    )
    run.add_argument("--enforce", action="store_true")
    verify = commands.add_parser("verify")
    verify.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "smoke":
            result = run_authenticated_loopback_smoke(
                load_performance_fixture(arguments.fixture)
            )
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
            return 0
        if arguments.command == "verify":
            report = _read_report(arguments.report)
            print(
                "gateway_performance_report_ok "
                f"status={report['overallStatus']} "
                f"evidence_class={report['evidenceClass']}"
            )
            return 0

        fixture = load_performance_fixture(arguments.fixture)
        provenance = detect_source_provenance(Path(__file__).resolve().parents[1])
        if arguments.source_commit != provenance["sourceCommit"]:
            raise ValueError("source provenance mismatch")
        if (
            arguments.source_tree_state != "auto"
            and arguments.source_tree_state != provenance["sourceTreeState"]
        ):
            raise ValueError("source provenance mismatch")
        machine = detect_reference_machine(
            storage_class=arguments.storage_class,
            filesystem=arguments.filesystem,
            power_state=arguments.power_state,
        )
        report = run_performance_report(
            fixture=fixture,
            source_commit=arguments.source_commit,
            source_tree_state=provenance["sourceTreeState"],
            reference_machine=machine,
            progress=lambda stage: print(
                f"gateway_performance_progress stage={stage}",
                file=sys.stderr,
                flush=True,
            ),
        )
        _write_report(arguments.output, report)
        print(
            "gateway_performance_report_ok "
            f"status={report['overallStatus']} "
            f"evidence_class={report['evidenceClass']}"
        )
        return 2 if arguments.enforce and report["overallStatus"] != "pass" else 0
    except (RuntimeError, ValueError):
        print(
            "gateway_performance_failed reason=measurement_invalid",
            file=sys.stderr,
        )
        return 1


__all__ = [
    "LatencyRound",
    "MeasurementOutcome",
    "PairedLatencyRound",
    "PerformanceFixture",
    "PerformanceObservations",
    "ReferenceMachine",
    "ThroughputRound",
    "build_performance_report",
    "detect_reference_machine",
    "detect_source_provenance",
    "load_performance_fixture",
    "measure_latency_round",
    "measure_paired_latency_round",
    "nearest_rank_percentile_ns",
    "run_authenticated_loopback_smoke",
    "run_performance_measurements",
    "run_performance_report",
    "summarize_latency_rounds",
    "summarize_paired_latency_rounds",
    "summarize_throughput_rounds",
    "validate_performance_report",
]


if __name__ == "__main__":
    raise SystemExit(main())
