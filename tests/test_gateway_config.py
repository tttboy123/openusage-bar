import json
import tempfile
import unittest
from pathlib import Path

from openusage_bar.gateway.config import GatewayConfig, GatewayMode, load_gateway_config
from openusage_bar.gateway.pools import PoolStrategy


class GatewayConfigTests(unittest.TestCase):
    def test_missing_config_defaults_to_observe_and_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_gateway_config(Path(directory) / "missing-gateway.json")

        self.assertEqual(config.mode, GatewayMode.OBSERVE)
        self.assertFalse(config.enabled)
        self.assertEqual(config.host, "127.0.0.1")
        self.assertFalse(config.cache_enabled)
        self.assertEqual(config.accounts, ())
        self.assertEqual(config.account_pools, ())

    def test_loads_closed_local_accounts_and_pools_without_credentials(self) -> None:
        payload = {
            "enabled": True,
            "mode": "advise",
            "accounts": [
                {
                    "provider_id": "openai",
                    "account_id": "work",
                    "alias": "Work",
                },
                {
                    "provider_id": "openai",
                    "account_id": "backup",
                    "alias": "Backup",
                },
            ],
            "account_pools": [
                {
                    "pool_id": "daily-coding",
                    "revision": 4,
                    "strategy": "quota-aware",
                    "members": [
                        {"account_id": "work", "priority": 10, "weight": 2},
                        {"account_id": "backup", "priority": 20, "weight": 1},
                    ],
                    "cross_provider_fallback": False,
                    "cross_model_fallback": False,
                    "cross_region_fallback": False,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway.json"
            path.write_bytes(json.dumps(payload).encode("utf-8"))
            config = load_gateway_config(path)

        self.assertEqual(config.accounts[0].account_id, "work")
        self.assertEqual(
            config.accounts[0].credential_account,
            "openai.work.gateway-api-key",
        )
        self.assertEqual(config.account_pools[0].strategy, PoolStrategy.QUOTA_AWARE)
        self.assertEqual(config.account_pools[0].members[0].weight, 2)
        self.assertNotIn("credential", json.dumps(payload).casefold())

    def test_pool_config_rejects_unknown_or_unbound_accounts(self) -> None:
        cases = (
            {
                "accounts": [],
                "account_pools": [
                    {
                        "pool_id": "pool",
                        "revision": 1,
                        "strategy": "fixed-first",
                        "members": [
                            {"account_id": "missing", "priority": 1, "weight": 1}
                        ],
                    }
                ],
            },
            {
                "accounts": [
                    {
                        "provider_id": "openai",
                        "account_id": "work",
                        "alias": "Work",
                        "credential_account": "victim",
                    }
                ]
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway.json"
            for payload in cases:
                with self.subTest(payload=payload):
                    path.write_bytes(json.dumps(payload).encode("utf-8"))
                    with self.assertRaises(ValueError):
                        load_gateway_config(path)

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
