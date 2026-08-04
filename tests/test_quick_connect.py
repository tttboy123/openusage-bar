import unittest
from datetime import datetime, timezone

from openusage_bar.activity_store import ActivityStore
from openusage_bar.local_api import LocalAPIRouter
from openusage_bar.quick_connect import quick_connect
from openusage_bar.query import QueryService


class QuickConnectTests(unittest.TestCase):
    def test_deepseek_jumps_to_console_with_api_key_mode(self):
        item = quick_connect("deepseek")

        self.assertEqual(item.family_id, "deepseek")
        self.assertEqual(item.console_url, "https://platform.deepseek.com")
        self.assertIn("api_key", item.auth_modes)
        self.assertIn("auto_detect", item.auth_modes)

    def test_codex_uses_oauth_and_auto_detect(self):
        item = quick_connect("codex")

        self.assertEqual(item.console_url, "https://chatgpt.com/codex")
        self.assertEqual(item.auth_modes, ("oauth", "auto_detect"))

    def test_unknown_family_fails_closed(self):
        with self.assertRaises(ValueError):
            quick_connect("not-a-family")

    def test_local_api_quick_connect_route_exposes_families(self):
        store = ActivityStore(":memory:")
        try:
            query = QueryService(
                store, clock=lambda: datetime.now(timezone.utc)
            )
            payload = LocalAPIRouter(query)._payload("/v1/quick-connect", {})
        finally:
            store.close()

        self.assertEqual(payload["schemaVersion"], "1.0")
        families = {item["familyId"] for item in payload["providers"]}
        self.assertIn("deepseek", families)
        self.assertIn("codex", families)
        deepseek = next(
            item for item in payload["providers"] if item["familyId"] == "deepseek"
        )
        self.assertEqual(deepseek["consoleUrl"], "https://platform.deepseek.com")
        self.assertIn("api_key", deepseek["authModes"])
