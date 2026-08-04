#!/usr/bin/env python3
"""Measure content-free smart-routing latency against frozen release budgets."""

from __future__ import annotations

import argparse
import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from openusage_bar.routing_api import RoutingController, create_routing_unix_server
from openusage_bar.routing_engine import decide_route
from openusage_bar.routing_policy import built_in_policy
from openusage_bar.routing_proxy import RoutingProxyController
from openusage_bar.routing_store import RoutingStore
from openusage_bar.routing_targets import RouteTargetConfiguration
from tests.test_routing_api import FakeQuery, NOW, request_payload
from tests.test_routing_engine import context, facts, request, target
from tests.test_routing_proxy import (
    FakeDecisionController,
    FakeEvidenceStore,
    FakeRegistry,
    connection as execution_connection,
    target as execution_target,
)


TARGET_COUNT = 128
ENGINE_P95_BUDGET_MS = 5.0
API_P95_BUDGET_MS = 50.0
PROXY_P95_BUDGET_MS = 20.0


def _validate_samples(value: int) -> int:
    if isinstance(value, bool) or not 100 <= value <= 2_000:
        raise ValueError("samples must be between 100 and 2000")
    return value


def _p95_ms(values_ns: list[int]) -> float:
    if not values_ns or any(value < 0 for value in values_ns):
        raise ValueError("invalid performance samples")
    ordered = sorted(values_ns)
    rank = max(1, (95 * len(ordered) + 99) // 100)
    return round(ordered[min(rank, len(ordered)) - 1] / 1_000_000, 3)


def _fixtures():
    targets = tuple(
        target(f"target-{index:03d}") for index in range(TARGET_COUNT)
    )
    target_facts = tuple(
        facts(f"target-{index:03d}") for index in range(TARGET_COUNT)
    )
    route_request = request()
    policy = built_in_policy("reliable")
    decision_context = context()
    return targets, target_facts, route_request, policy, decision_context


def _engine_samples(samples: int) -> list[int]:
    targets, target_facts, route_request, policy, decision_context = _fixtures()
    for _ in range(20):
        decide_route(
            route_request, targets, target_facts, policy, decision_context
        )
    values: list[int] = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        decision = decide_route(
            route_request, targets, target_facts, policy, decision_context
        )
        values.append(time.perf_counter_ns() - started)
        if decision.selected is None:
            raise RuntimeError("performance fixture produced no route")
    return values


def _request(path: Path, payload: bytes) -> None:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    try:
        client.connect(str(path))
        client.sendall(payload)
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        response = bytearray()
        while True:
            chunk = client.recv(64 * 1024)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > 256 * 1024 + 16 * 1024:
                raise RuntimeError("routing response exceeded bound")
    finally:
        client.close()
    if not response.startswith(b"HTTP/1.1 200 "):
        raise RuntimeError("routing performance request failed")


def _api_samples(samples: int) -> list[int]:
    targets, _target_facts, _request_value, _policy, _context = _fixtures()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        evidence = RoutingStore(root / "routing.sqlite3", clock=lambda: NOW)
        controller = RoutingController(
            query=FakeQuery(),
            target_loader=lambda: RouteTargetConfiguration(1, 1, targets),
            evidence_store=evidence,
            runtime_reader=lambda _start, _end: None,
            available_connections=lambda: ("connection-1",),
            clock=lambda: NOW,
        )
        socket_path = root / "router.sock"
        server = create_routing_unix_server(socket_path, controller)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        body = json.dumps(
            request_payload(), separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        wire = (
            b"POST /v1/simulations HTTP/1.1\r\n"
            b"Host: localhost\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            + body
        )
        try:
            for _ in range(20):
                _request(socket_path, wire)
            values: list[int] = []
            for _ in range(samples):
                started = time.perf_counter_ns()
                _request(socket_path, wire)
                values.append(time.perf_counter_ns() - started)
            return values
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
            evidence.close()


def _proxy_samples(samples: int) -> list[int]:
    selected = execution_target(
        "openai.performance.gpt-5",
        model="gpt-5",
        connection="performance",
    )

    class Adapter:
        @staticmethod
        def execute_chat(connection, model_id, body):
            return {
                "id": "chatcmpl-performance",
                "object": "chat.completion",
                "model": model_id,
                "choices": [],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 3,
                    "total_tokens": 15,
                },
            }

    controller = RoutingProxyController(
        decision_controller=FakeDecisionController(selected, ()),
        target_loader=lambda: RouteTargetConfiguration(1, 1, (selected,)),
        registry_loader=lambda: FakeRegistry({
            selected.target_id: (Adapter(), execution_connection(selected))
        }),
        evidence_store=FakeEvidenceStore(),
        clock=lambda: NOW,
    )
    payload = {
        "model": "openusage/reliable",
        "messages": [{"role": "user", "content": "performance-fixture"}],
        "stream": False,
    }
    for _ in range(20):
        controller.complete(payload)
    values: list[int] = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        controller.complete(payload)
        values.append(time.perf_counter_ns() - started)
    return values


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=200)
    parsed = parser.parse_args(arguments)
    try:
        samples = _validate_samples(parsed.samples)
        engine_p95 = _p95_ms(_engine_samples(samples))
        api_p95 = _p95_ms(_api_samples(samples))
        proxy_p95 = _p95_ms(_proxy_samples(samples))
    except (OSError, RuntimeError, ValueError):
        print("routing_performance_unavailable")
        return 2
    result = "ok" if (
        engine_p95 <= ENGINE_P95_BUDGET_MS
        and api_p95 <= API_P95_BUDGET_MS
        and proxy_p95 <= PROXY_P95_BUDGET_MS
    ) else "budget_failed"
    print(
        f"routing_performance_{result} targets={TARGET_COUNT} samples={samples} "
        f"engineP95Ms={engine_p95:.3f} apiP95Ms={api_p95:.3f} "
        f"proxyP95Ms={proxy_p95:.3f}"
    )
    return 0 if result == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
