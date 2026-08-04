import http.client
import threading
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from openusage_bar.activity_records import BalanceObservation, ProviderInstance
from openusage_bar.activity_records import DailyUsageRow
from openusage_bar.activity_store import ActivityStore
from openusage_bar.query import QueryService, to_wire
from openusage_bar.web_dashboard import make_dashboard_server, render_dashboard


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


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
