import http.client
import json
import tempfile
import threading
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from openusage_bar.activity_records import BalanceObservation, ProviderInstance
from openusage_bar.activity_records import DailyUsageRow
from openusage_bar.activity_store import ActivityStore
from openusage_bar.local_api import LocalAPIRouter, create_tcp_server
from openusage_bar.query import QueryService, to_wire
from openusage_bar.web_dashboard import make_dashboard_server, render_dashboard


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
TOKEN = "t" * 43


class WebDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ActivityStore(":memory:")
        self.store.record_balance(
            BalanceObservation(
                record_id="deepseek.balance",
                observed_at="2026-07-29T12:00:00Z",
                provider_id="deepseek",
                account_ref="deepseek",
                currency="CNY",
                available="10.50",
                voucher=None,
                cash=None,
                state="ok",
                quality="direct",
                stale=False,
                source_id="deepseek.balance",
            )
        )
        self.store.upsert_provider_instance(
            ProviderInstance(
                provider_id="deepseek",
                family_id="deepseek",
                display_name="DeepSeek",
                category="api",
                credential_source="deepseek_official_api",
                source_kind="official_api",
                observed_at="2026-07-29T12:00:00Z",
            )
        )
        self.query = QueryService(self.store, clock=lambda: NOW)

    def tearDown(self) -> None:
        self.store.close()

    def test_render_dashboard_contains_quota_hub_and_no_secrets(self):
        snapshot = self.query.resource_snapshot(date(2026, 7, 29))
        rendered = render_dashboard(to_wire(snapshot), "2026-07-29")

        self.assertIn("UsageHub", rendered)
        self.assertIn("Quota Hub", rendered)
        self.assertIn("CNY", rendered)
        self.assertNotIn("sk-", rendered)
        self.assertNotIn("Authorization", rendered)

    def test_dashboard_renders_model_cost_summary(self):
        self.store.commit_usage_import_success(
            provider_id="codex",
            source_id="codex.local_sessions",
            since=date(2026, 7, 23),
            until=date(2026, 7, 29),
            rows=[
                DailyUsageRow(
                    day="2026-07-29",
                    provider_id="codex",
                    model_id="deepseek-v4-flash",
                    input_tokens=100,
                    output_tokens=50,
                    cache_read_tokens=0,
                    cache_creation_tokens=0,
                    reasoning_tokens=None,
                    total_tokens=150,
                    cost_amount="1.25",
                    cost_currency="USD",
                    cost_basis="cc_switch.rollups",
                    quality="upstream_declared",
                    account_ref="",
                    token_counting_convention="components_disjoint",
                )
            ],
            attempted_at=NOW,
        )
        server = make_dashboard_server(self.query, port=0, today=date(2026, 7, 29))
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/")
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertIn("按模型消耗", body)
            self.assertIn("deepseek-v4-flash", body)
            self.assertIn("150", body)
            self.assertIn("1.25 USD", body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_loopback_server_serves_html_and_snapshot_json(self):
        server = make_dashboard_server(self.query, port=0, today=date(2026, 7, 29))
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/")
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertIn("UsageHub", body)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/v1/snapshot")
            response = connection.getresponse()
            payload = response.read()
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertIn(b'"quotaHub"', payload)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


    def test_dashboard_matches_local_api_envelopes(self):
        server = make_dashboard_server(self.query, port=0, today=date(2026, 7, 29))
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        temp = tempfile.TemporaryDirectory()
        local_api = create_tcp_server(
            self.query,
            port=0,
            bearer_token=TOKEN,
            token_path=Path(temp.name) / "api.token",
            clock=lambda: NOW,
        )
        local_thread = threading.Thread(target=local_api.serve_forever, daemon=True)
        local_thread.start()
        local_port = local_api.server_address[1]
        try:
            cases = (
                "/v1/summary?today=2026-07-29",
                "/v1/sources/status",
                "/v1/providers",
                "/v1/capabilities",
                "/v1/balances",
                "/v1/quick-connect",
                "/v1/changes?after=0&limit=10",
            )
            for path in cases:
                with self.subTest(path=path):
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                    connection.request("GET", path)
                    response = connection.getresponse()
                    body = response.read()
                    connection.close()
                    self.assertEqual(response.status, 200, path)
                    dashboard_payload = json.loads(body)

                    connection = http.client.HTTPConnection(
                        "127.0.0.1", local_port, timeout=3
                    )
                    connection.request(
                        "GET",
                        path,
                        headers={
                            "Host": f"127.0.0.1:{local_port}",
                            "Authorization": f"Bearer {TOKEN}",
                        },
                    )
                    response = connection.getresponse()
                    body = response.read()
                    connection.close()
                    self.assertEqual(response.status, 200, path)
                    local_payload = json.loads(body)
                    self.assertEqual(dashboard_payload, local_payload, path)
        finally:
            local_api.shutdown()
            local_api.server_close()
            local_thread.join(3)
            temp.cleanup()
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_dashboard_invalid_integer_query_returns_json_error(self):
        server = make_dashboard_server(self.query, port=0, today=date(2026, 7, 29))
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path in (
                "/v1/changes?after=nope&limit=10",
                "/v1/quotas/history?limit=nope",
            ):
                with self.subTest(path=path):
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                    connection.request("GET", path)
                    response = connection.getresponse()
                    body = response.read()
                    connection.close()
                    self.assertEqual(response.status, 400)
                    payload = json.loads(body)
                    self.assertEqual(payload["error"]["code"], "invalid_parameter")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_loopback_server_exposes_readonly_v1_endpoints(self):
        server = make_dashboard_server(self.query, port=0, today=date(2026, 7, 29))
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path, field in (
                ("/v1/sources/status", "sources"),
                ("/v1/providers", "providers"),
                ("/v1/capabilities", "providers"),
                ("/v1/balances", "balances"),
                ("/v1/quick-connect", "providers"),
            ):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                connection.request("GET", path)
                response = connection.getresponse()
                body = response.read()
                connection.close()
                self.assertEqual(response.status, 200, path)
                self.assertIn(f'"schemaVersion"'.encode(), body, path)
                self.assertIn(f'"{field}"'.encode(), body, path)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/v1/capacity")
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertIn('"providers"', body)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request(
                "GET", "/v1/activity/daily?from=2026-07-01&to=2026-07-29"
            )
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertIn('"rows"', body)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request(
                "GET", "/v1/costs/daily?from=2026-07-01&to=2026-07-29"
            )
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertIn('"rows"', body)

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/v1/activity/daily")
            response = connection.getresponse()
            body = response.read()
            connection.close()
            self.assertEqual(response.status, 400)
            self.assertEqual(
                json.loads(body)["error"]["code"],
                "invalid_parameter",
            )

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/v1/schema")
            response = connection.getresponse()
            schema = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(schema["routes"], list(LocalAPIRouter.ROUTES))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
