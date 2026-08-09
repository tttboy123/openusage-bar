import json
import tempfile
import unittest
from pathlib import Path

from openusage_bar.gateway.config import GatewayConfig, GatewayMode, load_gateway_config


class GatewayConfigTests(unittest.TestCase):
    def test_missing_config_defaults_to_observe_and_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_gateway_config(Path(directory) / "missing-gateway.json")

        self.assertEqual(config.mode, GatewayMode.OBSERVE)
        self.assertFalse(config.enabled)
        self.assertEqual(config.host, "127.0.0.1")
        self.assertFalse(config.cache_enabled)

    def test_config_recursively_rejects_secret_shaped_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway.json"
            for field in ("api_key", "secret", "token", "password", "cookie"):
                with self.subTest(field=field):
                    path.write_text(
                        json.dumps(
                            {
                                "providers": [
                                    {"name": "example", "options": {field: "secret"}}
                                ]
                            }
                        )
                    )
                    with self.assertRaisesRegex(ValueError, "Secret field"):
                        load_gateway_config(path)

    def test_advise_mode_cannot_enable_proxy(self) -> None:
        with self.assertRaisesRegex(ValueError, "proxy requires gateway mode"):
            GatewayConfig(
                enabled=True,
                mode=GatewayMode.ADVISE,
                proxy_enabled=True,
            )
