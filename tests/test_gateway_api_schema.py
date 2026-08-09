import json
import unittest
from pathlib import Path

from openusage_bar.local_api import LocalAPIRouter


ROOT = Path(__file__).resolve().parents[1]


class GatewayAPISchemaTests(unittest.TestCase):
    def test_gateway_schema_uses_a_separate_namespace(self) -> None:
        payload = json.loads(
            (ROOT / "openusage_bar/resources/gateway-api-v1.schema.json").read_text()
        )
        self.assertEqual(payload["apiVersion"], "gateway.openusage/v1")
        self.assertEqual(
            sorted(payload["routes"]),
            [
                "GET /gateway/v1/account-pools",
                "GET /gateway/v1/health",
                "GET /gateway/v1/schema",
                "POST /gateway/v1/responses",
                "POST /gateway/v1/should-send",
            ],
        )

    def test_local_api_schema_does_not_advertise_gateway_routes(self) -> None:
        self.assertTrue(
            all(not route.startswith("/gateway/") for route in LocalAPIRouter.ROUTES)
        )

    def test_local_api_target_rejects_gateway_namespace(self) -> None:
        with self.assertRaises(Exception):
            LocalAPIRouter._target("/gateway/v1/health")
