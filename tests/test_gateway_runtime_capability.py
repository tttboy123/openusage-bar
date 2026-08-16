from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from openusage_bar.gateway.api import GatewayRouter
from openusage_bar.gateway.contracts import GatewayMode
from openusage_bar.gateway.server import create_gateway_server
from openusage_bar.runtime_capabilities import (
    build_runtime_capability,
    validate_runtime_capability,
)


UNKNOWN_FEATURE = {
    "support": "unknown",
    "enabled": "unknown",
    "configured": "unknown",
    "operational": "unknown",
}
RUNTIME_CAPABILITY_MEDIA_TYPE = (
    "application/vnd.openusage.runtime-capability+json"
)
GATEWAY_TOKEN = "g" * 48
ROOT = Path(__file__).resolve().parents[1]


def _start(server: object) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _request(
    port: int,
    token: str,
    *,
    accept: str | None = None,
    authorize: bool = True,
    method: str = "GET",
    payload: bytes | None = None,
    target: str = "/gateway/v1/health",
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    headers = {
        "Host": f"127.0.0.1:{port}",
    }
    if authorize:
        headers["Authorization"] = f"Bearer {token}"
    if accept is not None:
        headers["Accept"] = accept
    connection.request(method, target, body=payload, headers=headers)
    response = connection.getresponse()
    body = response.read()
    result = (
        response.status,
        {name.lower(): value for name, value in response.getheaders()},
        body,
    )
    connection.close()
    return result


class GatewayRuntimeCapabilityTests(unittest.TestCase):
    def test_observe_server_snapshot_stays_contract_valid_and_disabled(self) -> None:
        router = GatewayRouter(
            mode=GatewayMode.OBSERVE,
            policy=lambda _request: None,
            proxy=lambda _payload: {},
        )
        with tempfile.TemporaryDirectory() as directory:
            server = create_gateway_server(
                router,
                port=0,
                bearer_token=GATEWAY_TOKEN,
                token_path=Path(directory) / "gateway.token",
            )
            thread = _start(server)
            try:
                status, _headers, raw = _request(
                    server.server_address[1],
                    GATEWAY_TOKEN,
                    accept=RUNTIME_CAPABILITY_MEDIA_TYPE,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

        snapshot = json.loads(raw)
        listener = snapshot["gateway"]["features"]["listener"]
        self.assertEqual(status, 200)
        self.assertEqual(validate_runtime_capability(snapshot), snapshot)
        self.assertEqual(snapshot["gateway"]["mode"], "observe")
        self.assertEqual(snapshot["gateway"]["operational"], "disabled")
        self.assertEqual(
            listener,
            {
                "support": "supported",
                "enabled": False,
                "configured": False,
                "operational": "disabled",
            },
        )

    def test_content_negotiated_health_and_responses_vary_on_accept(self) -> None:
        def proxy(payload: dict[str, object]) -> dict[str, object]:
            request = payload["request"]
            assert isinstance(request, dict)
            return {
                "apiVersion": "gateway.openusage/v1",
                "object": "gateway.response",
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "stream": request["stream"],
                "status": "complete",
                "outputText": "ok",
                "usage": None,
                "tool": {"observed": False},
                "cache": {"outcome": "disabled"},
                "fallback": {
                    "attempted": False,
                    "attemptCount": 1,
                    "finalAction": "none",
                },
            }

        router = GatewayRouter(
            mode=GatewayMode.GATEWAY,
            policy=lambda _request: None,
            proxy=proxy,
        )
        with tempfile.TemporaryDirectory() as directory:
            server = create_gateway_server(
                router,
                port=0,
                bearer_token=GATEWAY_TOKEN,
                token_path=Path(directory) / "gateway.token",
            )
            thread = _start(server)
            try:
                legacy = _request(server.server_address[1], GATEWAY_TOKEN)
                capability = _request(
                    server.server_address[1],
                    GATEWAY_TOKEN,
                    accept=RUNTIME_CAPABILITY_MEDIA_TYPE,
                )
                response_request = {
                    "provider": "openai",
                    "model": "gpt-4.1-mini",
                    "request": {
                        "model": "gpt-4.1-mini",
                        "input": "hello",
                        "stream": False,
                    },
                }
                json_response = _request(
                    server.server_address[1],
                    GATEWAY_TOKEN,
                    accept="application/json",
                    method="POST",
                    payload=json.dumps(response_request).encode("utf-8"),
                    target="/gateway/v1/responses",
                )
                response_request["request"]["stream"] = True
                sse_response = _request(
                    server.server_address[1],
                    GATEWAY_TOKEN,
                    accept="text/event-stream",
                    method="POST",
                    payload=json.dumps(response_request).encode("utf-8"),
                    target="/gateway/v1/responses",
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

        for representation, (status, headers, _raw) in {
            "health-legacy": legacy,
            "health-capability": capability,
            "responses-json": json_response,
            "responses-sse": sse_response,
        }.items():
            with self.subTest(representation=representation):
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("vary"), "Accept")

    def test_runtime_accept_falls_back_for_a_legacy_router_double(self) -> None:
        class LegacyRouter:
            def dispatch(
                self, method: str, path: str, body: bytes
            ) -> tuple[int, dict[str, object]]:
                self.last_request = (method, path, body)
                return 200, {
                    "apiVersion": "gateway.openusage/v1",
                    "status": "ok",
                    "mode": "advise",
                    "capabilities": {"shouldSend": True, "responses": False},
                }

        router = LegacyRouter()
        with tempfile.TemporaryDirectory() as directory:
            server = create_gateway_server(
                router,
                port=0,
                bearer_token=GATEWAY_TOKEN,
                token_path=Path(directory) / "gateway.token",
            )
            thread = _start(server)
            try:
                status, headers, raw = _request(
                    server.server_address[1],
                    GATEWAY_TOKEN,
                    accept=RUNTIME_CAPABILITY_MEDIA_TYPE,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("vary"), "Accept")
        self.assertEqual(router.last_request, ("GET", "/gateway/v1/health", b""))
        self.assertEqual(
            json.loads(raw),
            {
                "apiVersion": "gateway.openusage/v1",
                "status": "ok",
                "mode": "advise",
                "capabilities": {"shouldSend": True, "responses": False},
            },
        )

    def test_only_http_listener_liveness_can_promote_gateway_ready(self) -> None:
        router = GatewayRouter(
            mode=GatewayMode.GATEWAY,
            policy=lambda _request: None,
            proxy=lambda _payload: {},
        )

        direct_status, direct = router.dispatch_runtime_capability(
            "GET", "/gateway/v1/health", b""
        )

        self.assertEqual(direct_status, 200)
        self.assertEqual(direct["gateway"]["operational"], "unknown")
        for feature_id in ("listener", "should_send", "responses"):
            with self.subTest(boundary="direct", feature=feature_id):
                feature = direct["gateway"]["features"][feature_id]
                self.assertIs(feature["configured"], True)
                self.assertEqual(feature["operational"], "unknown")

        with tempfile.TemporaryDirectory() as directory:
            server = create_gateway_server(
                router,
                port=0,
                bearer_token=GATEWAY_TOKEN,
                token_path=Path(directory) / "gateway.token",
            )
            thread = _start(server)
            try:
                status, _headers, raw = _request(
                    server.server_address[1],
                    GATEWAY_TOKEN,
                    accept=RUNTIME_CAPABILITY_MEDIA_TYPE,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

        snapshot = json.loads(raw)
        gateway = snapshot["gateway"]
        self.assertEqual(status, 200)
        self.assertEqual(gateway["operational"], "ready")
        self.assertEqual(
            gateway["features"]["listener"]["operational"], "ready"
        )
        for feature_id in ("should_send", "responses"):
            with self.subTest(boundary="http", feature=feature_id):
                feature = gateway["features"][feature_id]
                self.assertIs(feature["configured"], True)
                self.assertEqual(feature["operational"], "unknown")

    def test_gateway_manifest_freezes_the_negotiated_health_representation(
        self,
    ) -> None:
        manifest = json.loads(
            (
                ROOT
                / "openusage_bar/resources/gateway-api-v1.schema.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(
            manifest["healthRepresentations"],
            {
                "default": {
                    "contentType": "application/json",
                    "apiVersion": "gateway.openusage/v1",
                },
                "runtimeCapability": {
                    "accept": RUNTIME_CAPABILITY_MEDIA_TYPE,
                    "contentType": "application/json",
                    "apiVersion": "runtime-capability.openusage/v1",
                    "object": "runtime.capability",
                },
            },
        )
        self.assertEqual(
            manifest["varyByRoute"],
            {
                "GET /gateway/v1/health": ["Accept"],
                "POST /gateway/v1/responses": ["Accept"],
            },
        )

    def test_health_adds_a_closed_runtime_snapshot_without_changing_legacy_facts(
        self,
    ) -> None:
        router = GatewayRouter(
            mode=GatewayMode.GATEWAY,
            policy=lambda _request: None,
            proxy=lambda _payload: {},
        )

        legacy_status, legacy_payload = router.dispatch(
            "GET", "/gateway/v1/health", b""
        )
        status, snapshot = router.dispatch_runtime_capability(
            "GET", "/gateway/v1/health", b""
        )

        self.assertEqual(legacy_status, 200)
        self.assertEqual(
            legacy_payload,
            {
                "apiVersion": "gateway.openusage/v1",
                "status": "ok",
                "mode": "gateway",
                "capabilities": {"shouldSend": True, "responses": True},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(validate_runtime_capability(snapshot), snapshot)
        self.assertEqual(
            snapshot,
            {
                "apiVersion": "runtime-capability.openusage/v1",
                "object": "runtime.capability",
                "observer": {
                    "operational": "unknown",
                    "generatedAt": None,
                    "lastGoodAt": None,
                    "dataRevision": None,
                    "schemaVersion": None,
                },
                "gateway": {
                    "mode": "gateway",
                    "operational": "unknown",
                    "features": {
                        "listener": {
                            "support": "supported",
                            "enabled": True,
                            "configured": True,
                            "operational": "unknown",
                        },
                        "should_send": {
                            "support": "supported",
                            "enabled": True,
                            "configured": True,
                            "operational": "unknown",
                        },
                        "responses": {
                            "support": "supported",
                            "enabled": True,
                            "configured": True,
                            "operational": "unknown",
                        },
                        "cache": dict(UNKNOWN_FEATURE),
                        "fallback": dict(UNKNOWN_FEATURE),
                        "pii_redaction": dict(UNKNOWN_FEATURE),
                        "streaming": dict(UNKNOWN_FEATURE),
                    },
                    "configuredProviderCount": None,
                    "healthyProviderCount": None,
                    "actions": ["retry"],
                    "lastError": None,
                },
            },
        )

    def test_renderer_snapshot_preserves_all_modes_and_operational_states(
        self,
    ) -> None:
        expected_modes = {
            GatewayMode.OBSERVE: ("disabled", "disabled", "disabled", "disabled"),
            GatewayMode.ADVISE: ("unknown", "unknown", "unknown", "disabled"),
            GatewayMode.GATEWAY: ("unknown", "unknown", "unknown", "unknown"),
        }
        for mode, expected in expected_modes.items():
            router = GatewayRouter(
                mode=mode,
                policy=lambda _request: None,
                proxy=lambda _payload: {},
            )
            status, snapshot = router.dispatch_runtime_capability(
                "GET", "/gateway/v1/health", b""
            )
            gateway = snapshot["gateway"]
            with self.subTest(mode=mode.value):
                self.assertEqual(status, 200)
                self.assertEqual(gateway["mode"], mode.value)
                self.assertEqual(gateway["operational"], expected[0])
                self.assertEqual(
                    gateway["features"]["listener"]["operational"], expected[1]
                )
                self.assertEqual(
                    gateway["features"]["should_send"]["operational"],
                    expected[2],
                )
                self.assertEqual(
                    gateway["features"]["responses"]["operational"], expected[3]
                )

        for operational in (
            "disabled",
            "starting",
            "ready",
            "degraded",
            "unavailable",
            "unknown",
        ):
            source = {
                "mode": "gateway",
                "operational": operational,
                "features": {
                    feature_id: dict(UNKNOWN_FEATURE)
                    for feature_id in (
                        "listener",
                        "should_send",
                        "responses",
                        "cache",
                        "fallback",
                        "pii_redaction",
                        "streaming",
                    )
                },
                "configuredProviderCount": None,
                "healthyProviderCount": None,
                "actions": [],
                "lastError": None,
            }
            snapshot = build_runtime_capability(
                {"operational": operational},
                source,
            )
            with self.subTest(operational=operational):
                self.assertEqual(snapshot["observer"]["operational"], operational)
                self.assertEqual(snapshot["gateway"]["operational"], operational)
                self.assertEqual(validate_runtime_capability(snapshot), snapshot)

    def test_authenticated_health_negotiates_the_runtime_capability_media_type(
        self,
    ) -> None:
        router = GatewayRouter(
            mode=GatewayMode.GATEWAY,
            policy=lambda _request: None,
            proxy=lambda _payload: {},
        )
        with tempfile.TemporaryDirectory() as directory:
            server = create_gateway_server(
                router,
                port=0,
                bearer_token=GATEWAY_TOKEN,
                token_path=Path(directory) / "gateway.token",
            )
            thread = _start(server)
            try:
                status, headers, raw = _request(
                    server.server_address[1],
                    GATEWAY_TOKEN,
                    accept=RUNTIME_CAPABILITY_MEDIA_TYPE,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

        snapshot = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(snapshot["apiVersion"], "runtime-capability.openusage/v1")
        self.assertEqual(snapshot["object"], "runtime.capability")
        self.assertEqual(validate_runtime_capability(snapshot), snapshot)

    def test_default_and_ambiguous_accept_values_keep_the_legacy_wire_body(
        self,
    ) -> None:
        router = GatewayRouter(
            mode=GatewayMode.GATEWAY,
            policy=lambda _request: None,
            proxy=lambda _payload: {},
        )
        expected = (
            b'{"apiVersion":"gateway.openusage/v1","capabilities":'
            b'{"responses":true,"shouldSend":true},"mode":"gateway",'
            b'"status":"ok"}'
        )
        with tempfile.TemporaryDirectory() as directory:
            server = create_gateway_server(
                router,
                port=0,
                bearer_token=GATEWAY_TOKEN,
                token_path=Path(directory) / "gateway.token",
            )
            thread = _start(server)
            try:
                for accept in (
                    None,
                    "application/json",
                    "*/*",
                    f"{RUNTIME_CAPABILITY_MEDIA_TYPE};q=1",
                    f"{RUNTIME_CAPABILITY_MEDIA_TYPE},application/json",
                ):
                    with self.subTest(accept=accept):
                        status, headers, raw = _request(
                            server.server_address[1],
                            GATEWAY_TOKEN,
                            accept=accept,
                        )
                        self.assertEqual(status, 200)
                        self.assertEqual(raw, expected)
                        self.assertEqual(headers["cache-control"], "no-store")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

    def test_authentication_precedes_runtime_capability_negotiation(self) -> None:
        router = GatewayRouter(
            mode=GatewayMode.GATEWAY,
            policy=lambda _request: None,
            proxy=lambda _payload: {},
        )
        with tempfile.TemporaryDirectory() as directory:
            server = create_gateway_server(
                router,
                port=0,
                bearer_token=GATEWAY_TOKEN,
                token_path=Path(directory) / "gateway.token",
            )
            thread = _start(server)
            try:
                status, headers, raw = _request(
                    server.server_address[1],
                    GATEWAY_TOKEN,
                    accept=RUNTIME_CAPABILITY_MEDIA_TYPE,
                    authorize=False,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

        self.assertEqual(status, 401)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(
            json.loads(raw),
            {
                "error": {
                    "code": "authentication_required",
                    "message": "Authentication is required.",
                    "retryable": False,
                }
            },
        )

    def test_runtime_media_type_does_not_expand_methods_or_paths(self) -> None:
        router = GatewayRouter(
            mode=GatewayMode.GATEWAY,
            policy=lambda _request: None,
            proxy=lambda _payload: {},
        )
        with tempfile.TemporaryDirectory() as directory:
            server = create_gateway_server(
                router,
                port=0,
                bearer_token=GATEWAY_TOKEN,
                token_path=Path(directory) / "gateway.token",
            )
            thread = _start(server)
            try:
                head_status, _, _ = _request(
                    server.server_address[1],
                    GATEWAY_TOKEN,
                    accept=RUNTIME_CAPABILITY_MEDIA_TYPE,
                    method="HEAD",
                )
                schema_status, _, schema_raw = _request(
                    server.server_address[1],
                    GATEWAY_TOKEN,
                    accept=RUNTIME_CAPABILITY_MEDIA_TYPE,
                    target="/gateway/v1/schema",
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

        self.assertEqual(head_status, 405)
        self.assertEqual(schema_status, 200)
        self.assertEqual(json.loads(schema_raw)["apiVersion"], "gateway.openusage/v1")

    def test_private_callable_state_and_producer_failures_never_cross_health(
        self,
    ) -> None:
        class PrivateCallable:
            def __init__(self, canary: str) -> None:
                self.token_path = canary
                self.authorization = f"Bearer {canary}"
                self.raw_provider_error = f"upstream:{canary}"

            def __call__(self, _value: object) -> dict[str, object]:
                return {}

        canary = "CAPABILITY_PRIVATE_CANARY_4f41"
        router = GatewayRouter(
            mode=GatewayMode.GATEWAY,
            policy=PrivateCallable(canary),
            proxy=PrivateCallable(canary),
        )

        status, snapshot = router.dispatch_runtime_capability(
            "GET", "/gateway/v1/health", b""
        )
        self.assertEqual(status, 200)
        self.assertNotIn(canary, json.dumps(snapshot, sort_keys=True))

        with patch(
            "openusage_bar.gateway.api.build_runtime_capability",
            side_effect=RuntimeError(f"raw:{canary}"),
        ):
            failure_status, failure = router.dispatch_runtime_capability(
                "GET", "/gateway/v1/health", b""
            )
            legacy_status, legacy = router.dispatch(
                "GET", "/gateway/v1/health", b""
            )
        self.assertEqual(failure_status, 500)
        self.assertEqual(
            failure,
            {
                "error": {
                    "code": "capability_invalid",
                    "message": "Gateway status could not be verified.",
                    "retryable": True,
                }
            },
        )
        self.assertNotIn(canary, json.dumps(failure, sort_keys=True))
        self.assertEqual(legacy_status, 200)
        self.assertEqual(legacy["apiVersion"], "gateway.openusage/v1")


if __name__ == "__main__":
    unittest.main()
