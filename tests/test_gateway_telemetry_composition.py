"""RED contracts for bounded telemetry queries and CLI composition.

The seams are the local ``GatewayTelemetryStore.recent_burn_rate`` query, the
read-only Gateway CLI commands, and the default enabled Gateway server factory.
No test opens a real listener, reads a credential, or calls a Provider.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import openusage_bar.collector_cli as collector_cli
from openusage_bar.collector_cli import main
from openusage_bar.gateway.config import GatewayConfig, GatewayMode
from openusage_bar.gateway.telemetry import GatewayTelemetryStore
from openusage_bar.query import CapacityProvider, CapacityResult, QuotaAppliesTo


_NOW = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


class _Query:
    def __init__(self) -> None:
        self.capacity_calls = 0

    def capacity(self) -> CapacityResult:
        self.capacity_calls += 1
        return CapacityResult(
            "1.0",
            7,
            "2026-08-09T12:00:00Z",
            (
                CapacityProvider(
                    record_id="openai.daily",
                    provider_id="openai",
                    account_ref=None,
                    quota_name="Daily",
                    unit="percent",
                    used="26",
                    quota_limit="100",
                    remaining="74",
                    remaining_ratio=0.74,
                    resets_at=None,
                    period_start=None,
                    period_end=None,
                    observed_at="2026-08-09T11:59:00Z",
                    freshness_seconds=60,
                    state="ok",
                    quality="direct",
                    stale=False,
                    revision=7,
                    source_id="openai.quota",
                    quota_window="daily",
                    applies_to=QuotaAppliesTo("account", ()),
                ),
            ),
        )


class _TelemetryProbe:
    def __init__(
        self,
        *,
        burn_rate: float | None = 12.0,
        failure: BaseException | None = None,
    ) -> None:
        self.burn_rate = burn_rate
        self.failure = failure
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def recent_burn_rate(
        self,
        provider_id: str,
        model_id: str,
        **bounds: object,
    ) -> float | None:
        self.calls.append((provider_id, model_id, bounds))
        if self.failure is not None:
            raise self.failure
        return self.burn_rate


def _record(
    store: GatewayTelemetryStore,
    *,
    request_id: str,
    finished_at: datetime,
    input_tokens: int,
    output_tokens: int,
    provider_id: str = "openai",
    model_id: str = "gpt-4.1-mini",
) -> None:
    store.record_request(
        request_id=request_id,
        provider_id=provider_id,
        model_id=model_id,
        started_at=finished_at - timedelta(milliseconds=50),
        finished_at=finished_at,
        status_class="2xx",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=50,
        estimated_cost=None,
        actual_cost=None,
        cache_outcome="disabled",
        fallback_count=0,
        error_code=None,
    )


def _should_send(router: object) -> tuple[int, dict[str, object]]:
    body = json.dumps(
        {
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "estimated_tokens": 8_000,
            "window": "5m",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    status, payload = router.dispatch(
        "POST",
        "/gateway/v1/should-send",
        body,
    )
    if type(payload) is not dict:
        raise AssertionError("Should-Send must return one JSON object")
    return status, payload


class GatewayTelemetryBurnRateTests(unittest.TestCase):
    def test_recent_burn_rate_is_scoped_windowed_and_row_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = GatewayTelemetryStore(
                Path(directory) / "gateway-telemetry.sqlite3",
                clock=_Clock(_NOW),
            )
            try:
                rows = (
                    # The two newest matching rows total 60 tokens.  The
                    # five-minute fixed denominator therefore yields 12/min.
                    ("matching-old", -4, 60, 40, "openai", "gpt-4.1-mini"),
                    ("matching-middle", -2, 25, 15, "openai", "gpt-4.1-mini"),
                    ("matching-new", -1, 10, 10, "openai", "gpt-4.1-mini"),
                    # Each excluded row would make the expected value wrong if
                    # the query omitted its lower/upper or identity boundary.
                    ("outside-window", -6, 50_000, 50_000, "openai", "gpt-4.1-mini"),
                    ("future", 1, 50_000, 50_000, "openai", "gpt-4.1-mini"),
                    ("other-model", -1, 50_000, 50_000, "openai", "gpt-5"),
                    ("other-provider", -1, 50_000, 50_000, "anthropic", "gpt-4.1-mini"),
                )
                for (
                    request_id,
                    minute_offset,
                    input_tokens,
                    output_tokens,
                    provider_id,
                    model_id,
                ) in rows:
                    _record(
                        store,
                        request_id=request_id,
                        finished_at=_NOW + timedelta(minutes=minute_offset),
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        provider_id=provider_id,
                        model_id=model_id,
                    )

                burn_rate = store.recent_burn_rate(
                    "openai",
                    "gpt-4.1-mini",
                    window_minutes=5,
                    max_rows=2,
                )

                self.assertEqual(burn_rate, 12.0)
            finally:
                store.close()

    def test_recent_burn_rate_distinguishes_no_sample_from_explicit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = GatewayTelemetryStore(
                Path(directory) / "gateway-telemetry.sqlite3",
                clock=_Clock(_NOW),
            )
            try:
                self.assertIsNone(
                    store.recent_burn_rate(
                        "openai",
                        "gpt-4.1-mini",
                        window_minutes=5,
                        max_rows=100,
                    )
                )

                _record(
                    store,
                    request_id="explicit-zero",
                    finished_at=_NOW - timedelta(minutes=1),
                    input_tokens=0,
                    output_tokens=0,
                )

                self.assertEqual(
                    store.recent_burn_rate(
                        "openai",
                        "gpt-4.1-mini",
                        window_minutes=5,
                        max_rows=100,
                    ),
                    0.0,
                )
            finally:
                store.close()

    def test_recent_burn_rate_rejects_unbounded_window_or_row_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = GatewayTelemetryStore(
                Path(directory) / "gateway-telemetry.sqlite3",
                clock=_Clock(_NOW),
            )
            try:
                for window_minutes in (0, 61, True, 1.5):
                    with self.subTest(window_minutes=window_minutes):
                        with self.assertRaises(ValueError):
                            store.recent_burn_rate(
                                "openai",
                                "gpt-4.1-mini",
                                window_minutes=window_minutes,
                                max_rows=100,
                            )
                for max_rows in (0, 1_001, True, 1.5):
                    with self.subTest(max_rows=max_rows):
                        with self.assertRaises(ValueError):
                            store.recent_burn_rate(
                                "openai",
                                "gpt-4.1-mini",
                                window_minutes=5,
                                max_rows=max_rows,
                            )
            finally:
                store.close()


class GatewayTelemetryCompositionTests(unittest.TestCase):
    def test_read_only_cli_paths_never_construct_or_read_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status_config = root / "status.json"
            status_config.write_text(
                json.dumps({"enabled": True, "mode": "advise"}),
                encoding="utf-8",
            )
            observe_config = root / "observe.json"
            observe_config.write_text(
                json.dumps({"enabled": True, "mode": "observe"}),
                encoding="utf-8",
            )

            def forbidden_store() -> object:
                raise AssertionError("read-only Gateway path opened the ledger")

            with patch(
                "openusage_bar.gateway.telemetry.GatewayTelemetryStore",
                side_effect=AssertionError(
                    "read-only Gateway path constructed telemetry"
                ),
            ) as telemetry_constructor:
                print_code = main(
                    ["gateway", "print-config"],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    store_factory=forbidden_store,
                )
                status_code = main(
                    [
                        "gateway",
                        "status",
                        "--config",
                        str(status_config),
                    ],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    store_factory=forbidden_store,
                )
                observe_code = main(
                    [
                        "gateway",
                        "start",
                        "--config",
                        str(observe_config),
                        "--token-path",
                        str(root / "gateway.token"),
                    ],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    store_factory=forbidden_store,
                )

            self.assertEqual(print_code, 0)
            self.assertEqual(status_code, 0)
            self.assertNotEqual(observe_code, 0)
            telemetry_constructor.assert_not_called()

    def test_enabled_advise_and_gateway_compose_one_shared_telemetry_store(self) -> None:
        advise_telemetry = _TelemetryProbe(burn_rate=12.0)
        gateway_telemetry = _TelemetryProbe(burn_rate=12.0)
        runtime_arguments: list[dict[str, object]] = []

        def runtime_factory(**kwargs: object):
            runtime_arguments.append(kwargs)
            return lambda _payload: {
                "apiVersion": "gateway.openusage/v1",
                "object": "gateway.response",
            }

        with tempfile.TemporaryDirectory() as directory, patch.object(
            collector_cli,
            "DEFAULT_GATEWAY_TELEMETRY_PATH",
            Path(directory) / "gateway-telemetry.sqlite3",
            create=True,
        ), patch(
            "openusage_bar.gateway.telemetry.GatewayTelemetryStore",
            side_effect=(advise_telemetry, gateway_telemetry),
        ) as telemetry_constructor, patch(
            "openusage_bar.gateway.runtime.GatewayRuntime",
            side_effect=runtime_factory,
        ), patch(
            "openusage_bar.gateway.server.create_gateway_server",
            side_effect=lambda router, **_kwargs: router,
        ):
            advise_router = collector_cli._build_default_gateway_server(
                config=GatewayConfig(
                    enabled=True,
                    mode=GatewayMode.ADVISE,
                ),
                token_path=Path(directory) / "advise.token",
                query=_Query(),
            )
            gateway_router = collector_cli._build_default_gateway_server(
                config=GatewayConfig(
                    enabled=True,
                    mode=GatewayMode.GATEWAY,
                    proxy_enabled=True,
                ),
                token_path=Path(directory) / "gateway.token",
                query=_Query(),
            )

            advise_status, advise_payload = _should_send(advise_router)
            gateway_status, gateway_payload = _should_send(gateway_router)

        self.assertEqual(telemetry_constructor.call_count, 2)
        self.assertEqual(len(runtime_arguments), 1)
        self.assertIs(runtime_arguments[0]["telemetry"], gateway_telemetry)
        self.assertEqual(advise_status, 200)
        self.assertEqual(gateway_status, 200)
        self.assertEqual(
            advise_payload["details"]["burn_rate_per_min"],
            12.0,
        )
        self.assertEqual(
            gateway_payload["details"]["burn_rate_per_min"],
            12.0,
        )
        self.assertIsNone(
            advise_payload["details"]["quota_remaining"]
        )
        self.assertIsNone(
            advise_payload["details"]["predicted_exhaustion_minutes"]
        )
        self.assertEqual(len(advise_telemetry.calls), 1)
        self.assertEqual(len(gateway_telemetry.calls), 1)
        self.assertEqual(
            advise_telemetry.calls[0][:2],
            ("openai", "gpt-4.1-mini"),
        )
        self.assertEqual(
            gateway_telemetry.calls[0][:2],
            ("openai", "gpt-4.1-mini"),
        )

    def test_telemetry_initialization_failure_is_fail_open_in_enabled_modes(self) -> None:
        runtime_arguments: list[dict[str, object]] = []

        def runtime_factory(**kwargs: object):
            runtime_arguments.append(kwargs)
            return lambda _payload: {
                "apiVersion": "gateway.openusage/v1",
                "object": "gateway.response",
            }

        with tempfile.TemporaryDirectory() as directory, patch.object(
            collector_cli,
            "DEFAULT_GATEWAY_TELEMETRY_PATH",
            Path(directory) / "gateway-telemetry.sqlite3",
            create=True,
        ), patch(
            "openusage_bar.gateway.telemetry.GatewayTelemetryStore",
            side_effect=RuntimeError("private telemetry init failure"),
        ) as telemetry_constructor, patch(
            "openusage_bar.gateway.runtime.GatewayRuntime",
            side_effect=runtime_factory,
        ), patch(
            "openusage_bar.gateway.server.create_gateway_server",
            side_effect=lambda router, **_kwargs: router,
        ):
            routers = [
                collector_cli._build_default_gateway_server(
                    config=GatewayConfig(
                        enabled=True,
                        mode=GatewayMode.ADVISE,
                    ),
                    token_path=Path(directory) / "advise.token",
                    query=_Query(),
                ),
                collector_cli._build_default_gateway_server(
                    config=GatewayConfig(
                        enabled=True,
                        mode=GatewayMode.GATEWAY,
                        proxy_enabled=True,
                    ),
                    token_path=Path(directory) / "gateway.token",
                    query=_Query(),
                ),
            ]

            results = [_should_send(router) for router in routers]

        self.assertEqual(telemetry_constructor.call_count, 2)
        self.assertEqual([status for status, _payload in results], [200, 200])
        self.assertEqual(len(runtime_arguments), 1)
        self.assertIsNone(runtime_arguments[0]["telemetry"])
        for _status, payload in results:
            self.assertIsNone(payload["details"]["burn_rate_per_min"])
            self.assertNotIn("telemetry", repr(payload).casefold())
            self.assertNotIn("failure", repr(payload).casefold())

    def test_telemetry_query_failure_is_fail_open_for_should_send(self) -> None:
        telemetry = _TelemetryProbe(
            failure=RuntimeError("private telemetry query failure")
        )
        query = _Query()

        with tempfile.TemporaryDirectory() as directory, patch.object(
            collector_cli,
            "DEFAULT_GATEWAY_TELEMETRY_PATH",
            Path(directory) / "gateway-telemetry.sqlite3",
            create=True,
        ), patch(
            "openusage_bar.gateway.telemetry.GatewayTelemetryStore",
            return_value=telemetry,
        ), patch(
            "openusage_bar.gateway.server.create_gateway_server",
            side_effect=lambda router, **_kwargs: router,
        ):
            router = collector_cli._build_default_gateway_server(
                config=GatewayConfig(
                    enabled=True,
                    mode=GatewayMode.ADVISE,
                ),
                token_path=Path(directory) / "gateway.token",
                query=query,
            )

            status, payload = _should_send(router)

        self.assertEqual(status, 200)
        self.assertEqual(query.capacity_calls, 1)
        self.assertEqual(len(telemetry.calls), 1)
        self.assertEqual(payload["decision"], "yes")
        self.assertIsNone(payload["details"]["burn_rate_per_min"])
        self.assertIsNone(
            payload["details"]["predicted_exhaustion_minutes"]
        )
        self.assertNotIn("telemetry", repr(payload).casefold())
        self.assertNotIn("failure", repr(payload).casefold())

    def test_cache_initialization_failure_keeps_gateway_and_telemetry_available(
        self,
    ) -> None:
        telemetry = _TelemetryProbe(burn_rate=9.0)
        runtime_arguments: list[dict[str, object]] = []

        def runtime_factory(**kwargs: object):
            runtime_arguments.append(kwargs)
            return lambda _payload: {
                "apiVersion": "gateway.openusage/v1",
                "object": "gateway.response",
            }

        with tempfile.TemporaryDirectory() as directory, patch.object(
            collector_cli,
            "DEFAULT_GATEWAY_TELEMETRY_PATH",
            Path(directory) / "gateway-telemetry.sqlite3",
        ), patch.object(
            collector_cli,
            "DEFAULT_GATEWAY_CACHE_PATH",
            Path(directory) / "gateway-cache.sqlite3",
        ), patch(
            "openusage_bar.gateway.telemetry.GatewayTelemetryStore",
            return_value=telemetry,
        ) as telemetry_constructor, patch(
            "openusage_bar.gateway.cache.SQLiteGatewayCache",
            side_effect=RuntimeError("private cache init failure"),
        ) as cache_constructor, patch(
            "openusage_bar.gateway.runtime.GatewayRuntime",
            side_effect=runtime_factory,
        ), patch(
            "openusage_bar.gateway.server.create_gateway_server",
            side_effect=lambda router, **_kwargs: router,
        ):
            router = collector_cli._build_default_gateway_server(
                config=GatewayConfig(
                    enabled=True,
                    mode=GatewayMode.GATEWAY,
                    proxy_enabled=True,
                    cache_enabled=True,
                ),
                token_path=Path(directory) / "gateway.token",
                query=_Query(),
            )

            health_status, health = router.dispatch(
                "GET",
                "/gateway/v1/health",
                b"",
            )
            should_send_status, should_send = _should_send(router)

        telemetry_constructor.assert_called_once()
        cache_constructor.assert_called_once()
        self.assertEqual(len(runtime_arguments), 1)
        self.assertIsNone(runtime_arguments[0]["cache"])
        self.assertIs(runtime_arguments[0]["telemetry"], telemetry)
        self.assertEqual(health_status, 200)
        self.assertTrue(health["capabilities"]["responses"])
        self.assertEqual(should_send_status, 200)
        self.assertEqual(
            should_send["details"]["burn_rate_per_min"],
            9.0,
        )
        self.assertIsNone(
            should_send["details"]["predicted_exhaustion_minutes"]
        )
        self.assertEqual(len(telemetry.calls), 1)
        self.assertNotIn("cache init failure", repr(health).casefold())
        self.assertNotIn("cache init failure", repr(should_send).casefold())


if __name__ == "__main__":
    unittest.main()
