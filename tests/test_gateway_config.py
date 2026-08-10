import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from openusage_bar.gateway.accounts import ProviderAccountRef
from openusage_bar.gateway.config import (
    GatewayConfig,
    GatewayConfigStore,
    GatewayMode,
    load_gateway_config,
)
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

    def test_config_store_round_trips_accounts_privately_and_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "private"
            path = root / "gateway.json"
            store = GatewayConfigStore(path)
            account = ProviderAccountRef(
                provider_id="openai",
                account_id="work",
                alias="Work",
                credential_account="openai.work.gateway-api-key",
            )
            config = GatewayConfig(
                enabled=True,
                mode=GatewayMode.ADVISE,
                accounts=(account,),
            )

            store.save(config)
            loaded = store.load()

            self.assertEqual(loaded, config)
            raw = path.read_text(encoding="utf-8").casefold()
            for forbidden in (
                "credential",
                "secret",
                "providerkey",
                "gateway-api-key",
            ):
                self.assertNotIn(forbidden, raw)
            if os.name != "nt":
                parent = path.parent.stat()
                file_info = path.stat()
                self.assertEqual(parent.st_uid, os.getuid())
                self.assertEqual(stat.S_IMODE(parent.st_mode), 0o700)
                self.assertTrue(stat.S_ISREG(file_info.st_mode))
                self.assertEqual(file_info.st_nlink, 1)
                self.assertEqual(file_info.st_uid, os.getuid())
                self.assertEqual(stat.S_IMODE(file_info.st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "Windows symlink and mode safety is native")
    def test_config_store_load_rejects_symlink_or_public_regular_file(self) -> None:
        payload = {
            "enabled": True,
            "mode": "advise",
            "host": "127.0.0.1",
            "port": 17823,
            "proxy_enabled": False,
            "cache_enabled": False,
            "accounts": [],
            "account_pools": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "private"
            private.mkdir(mode=0o700)

            public_target = root / "public-target.json"
            public_target.write_text(json.dumps(payload), encoding="utf-8")
            public_target.chmod(0o600)
            symlink_path = private / "gateway-symlink.json"
            symlink_path.symlink_to(public_target)

            public_file = private / "gateway-public.json"
            public_file.write_text(json.dumps(payload), encoding="utf-8")
            public_file.chmod(0o644)

            for path in (symlink_path, public_file):
                with self.subTest(path=path.name):
                    with self.assertRaisesRegex(ValueError, "unsafe"):
                        GatewayConfigStore(path).load()

    def test_config_store_load_handles_bounded_regular_file_short_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "gateway.json"
            store = GatewayConfigStore(path)
            account = ProviderAccountRef(
                provider_id="openai",
                account_id="work",
                alias="Work",
                credential_account="openai.work.gateway-api-key",
            )
            config = GatewayConfig(
                enabled=True,
                mode=GatewayMode.ADVISE,
                accounts=(account,),
            )
            store.save(config)
            real_read = os.read

            def short_read(descriptor: int, count: int) -> bytes:
                return real_read(descriptor, min(count, 7))

            with patch("openusage_bar.gateway.config.os.read", side_effect=short_read):
                loaded = store.load()

            self.assertEqual(loaded, config)

    def test_config_store_transaction_close_failure_never_leaks_process_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "gateway.json"
            store = GatewayConfigStore(path)
            failed_close_fds: list[int] = []
            real_close = os.close

            def fail_first_close(descriptor: int) -> None:
                if not failed_close_fds:
                    failed_close_fds.append(descriptor)
                    raise OSError("simulated lock close failure")
                real_close(descriptor)

            try:
                with patch(
                    "openusage_bar.gateway.config.os.close",
                    side_effect=fail_first_close,
                ):
                    with self.assertRaisesRegex(
                        OSError,
                        "simulated lock close failure",
                    ):
                        with store.transaction():
                            pass
            finally:
                for descriptor in failed_close_fds:
                    try:
                        real_close(descriptor)
                    except OSError:
                        pass

            started = time.monotonic()
            with store.transaction():
                pass
            self.assertLess(time.monotonic() - started, 1.0)

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
