import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openusage_bar.gateway.accounts import AccountState, ProviderAccountRef
from openusage_bar.gateway.commands import run_gateway_account_mutation
from openusage_bar.gateway.config import GatewayConfig, GatewayConfigStore, GatewayMode
from openusage_bar.gateway.pools import (
    AccountPool,
    PoolMember,
    PoolStrategy,
    account_pools_public_payload,
    validate_account_pools_public_payload,
)


class FakeKeychain:
    def __init__(
        self,
        events: list[tuple[object, ...]],
        *,
        existing: str | None = None,
        set_error: Exception | None = None,
        delete_error: Exception | None = None,
        get_delay_seconds: float = 0.0,
    ) -> None:
        self.events = events
        self.existing = existing
        self.set_error = set_error
        self.delete_error = delete_error
        self.get_delay_seconds = get_delay_seconds

    def get(self, account_name: str) -> str | None:
        self.events.append(("keychain.get", account_name, self.existing))
        if self.get_delay_seconds:
            threading.Event().wait(self.get_delay_seconds)
        return self.existing

    def set(self, account_name: str, secret: str) -> None:
        self.events.append(("keychain.set", account_name, secret))
        if self.set_error is not None:
            raise self.set_error

    def delete(self, account_name: str) -> None:
        self.events.append(("keychain.delete", account_name, None))
        if self.delete_error is not None:
            raise self.delete_error


class FakeConfigStore:
    def __init__(
        self,
        events: list[tuple[object, ...]],
        *,
        error: Exception | None = None,
        initial: GatewayConfig | None = None,
    ) -> None:
        self.events = events
        self.error = error
        self.initial = initial or GatewayConfig(enabled=True, mode=GatewayMode.ADVISE)
        self.saved: list[GatewayConfig] = []

    def load(self) -> GatewayConfig:
        return self.initial

    def save(self, config: GatewayConfig) -> None:
        self.events.append(("store.save", config))
        if self.error is not None:
            raise self.error
        self.saved.append(config)


def create_account_request(
    *,
    provider_id: str = "openai",
    account_id: str = "work",
    alias: str = "Work",
    secret: str = "sk-private-material",
) -> io.StringIO:
    return io.StringIO(
        json.dumps(
            {
                "version": 1,
                "action": "create_account",
                "account": {
                    "providerId": provider_id,
                    "accountId": account_id,
                    "alias": alias,
                },
                "credentialMaterial": {"providerKey": secret},
            },
            separators=(",", ":"),
        )
    )


def edit_account_request(
    *,
    alias: str = "New",
    secret: str | None = "new-secret",
) -> io.StringIO:
    return io.StringIO(
        json.dumps(
            {
                "version": 1,
                "action": "edit_account",
                "account": {
                    "providerId": "openai",
                    "accountId": "work",
                    "alias": alias,
                },
                "credentialMaterial": {"providerKey": secret},
            },
            separators=(",", ":"),
        )
    )


def expected_account() -> ProviderAccountRef:
    return ProviderAccountRef(
        provider_id="openai",
        account_id="work",
        alias="Work",
        credential_account="openai.work.gateway-api-key",
    )


def expected_account_config() -> GatewayConfig:
    return GatewayConfig(
        enabled=True,
        mode=GatewayMode.ADVISE,
        accounts=(expected_account(),),
    )


class GatewayAccountMutationCommandTests(unittest.TestCase):
    def test_create_account_writes_credential_before_secret_free_config(self) -> None:
        events: list[tuple[object, ...]] = []
        keychain = FakeKeychain(events)
        store = FakeConfigStore(events)
        stdout = io.StringIO()

        exit_code = run_gateway_account_mutation(
            create_account_request(),
            stdout,
            store=store,
            keychain=keychain,
        )

        account = expected_account()
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            [event[0] for event in events],
            ["keychain.get", "keychain.set", "store.save"],
        )
        self.assertEqual(
            events[1],
            ("keychain.set", "openai.work.gateway-api-key", "sk-private-material"),
        )
        self.assertEqual(len(store.saved), 1)
        saved = store.saved[0]
        self.assertEqual(saved.accounts, (account,))
        self.assertEqual(saved.account_pools, ())
        self.assertNotIn(
            "sk-private-material",
            json.dumps(saved, default=repr, sort_keys=True),
        )
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "version": 1,
                "ok": True,
                "code": "ok",
                "account": account.to_public_dict(state=AccountState.UNKNOWN),
            },
        )
        rendered = stdout.getvalue().casefold()
        for forbidden in (
            "sk-private-material",
            "credential",
            "gateway-api-key",
            '"accountid"',
            "account_id",
            "providerkey",
            "token",
            "path",
            "header",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_create_account_rolls_back_credential_when_config_save_fails(self) -> None:
        events: list[tuple[object, ...]] = []
        keychain = FakeKeychain(events)
        store = FakeConfigStore(events, error=OSError("private config path leaked"))
        stdout = io.StringIO()

        exit_code = run_gateway_account_mutation(
            create_account_request(),
            stdout,
            store=store,
            keychain=keychain,
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            events,
            [
                ("keychain.get", "openai.work.gateway-api-key", None),
                ("keychain.set", "openai.work.gateway-api-key", "sk-private-material"),
                ("store.save", expected_account_config()),
                ("keychain.delete", "openai.work.gateway-api-key", None),
            ],
        )
        self.assertEqual(store.saved, [])
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "version": 1,
                "ok": False,
                "code": "config_write_failed",
            },
        )
        self.assertNotIn("private config path leaked", stdout.getvalue())
        self.assertNotIn("sk-private-material", stdout.getvalue())

    def test_create_account_reports_sanitized_rollback_failure(self) -> None:
        save_error = OSError("private config write failed at /Users/example/config")
        delete_error = OSError(
            "private rollback delete failed at /Users/example/keychain"
        )
        events: list[tuple[object, ...]] = []
        keychain = FakeKeychain(events, delete_error=delete_error)
        store = FakeConfigStore(events, error=save_error)
        stdout = io.StringIO()

        exit_code = run_gateway_account_mutation(
            create_account_request(secret="sk-private-material"),
            stdout,
            store=store,
            keychain=keychain,
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            events,
            [
                ("keychain.get", "openai.work.gateway-api-key", None),
                ("keychain.set", "openai.work.gateway-api-key", "sk-private-material"),
                ("store.save", expected_account_config()),
                ("keychain.delete", "openai.work.gateway-api-key", None),
            ],
        )
        self.assertEqual(store.saved, [])
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "version": 1,
                "ok": False,
                "code": "credential_rollback_failed",
            },
        )
        rendered = stdout.getvalue().casefold()
        for forbidden in (
            "private config write failed",
            "private rollback delete failed",
            "sk-private-material",
            "/users/example/config",
            "/users/example/keychain",
            "gateway-api-key",
            "path",
            "secret",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_create_account_never_overwrites_an_existing_credential(self) -> None:
        canary = "CANARY_EXISTING_GATEWAY_CREDENTIAL"
        events: list[tuple[object, ...]] = []
        keychain = FakeKeychain(events, existing=canary)
        store = FakeConfigStore(events)
        stdout = io.StringIO()

        exit_code = run_gateway_account_mutation(
            create_account_request(),
            stdout,
            store=store,
            keychain=keychain,
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            events,
            [("keychain.get", "openai.work.gateway-api-key", canary)],
        )
        self.assertEqual(store.saved, [])
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "version": 1,
                "ok": False,
                "code": "already_exists",
            },
        )
        self.assertNotIn(canary, stdout.getvalue())

    def test_create_account_rejects_duplicate_json_keys_before_mutation(self) -> None:
        cases = (
            (
                "duplicate account providerId",
                (
                    '{"version":1,"action":"create_account",'
                    '"account":{"providerId":"anthropic","providerId":"openai",'
                    '"accountId":"work","alias":"Work"},'
                    '"credentialMaterial":{"providerKey":"sk-private-material"}}'
                ),
            ),
            (
                "duplicate root action",
                (
                    '{"version":1,"action":"remove_account",'
                    '"action":"create_account",'
                    '"account":{"providerId":"openai",'
                    '"accountId":"work","alias":"Work"},'
                    '"credentialMaterial":{"providerKey":"sk-private-material"}}'
                ),
            ),
        )
        for name, raw in cases:
            with self.subTest(name=name):
                events: list[tuple[object, ...]] = []
                keychain = FakeKeychain(events)
                store = FakeConfigStore(events)
                stdout = io.StringIO()

                exit_code = run_gateway_account_mutation(
                    io.StringIO(raw),
                    stdout,
                    store=store,
                    keychain=keychain,
                )

                self.assertEqual(exit_code, 1)
                self.assertEqual(events, [])
                self.assertEqual(store.saved, [])
                self.assertEqual(
                    json.loads(stdout.getvalue()),
                    {
                        "version": 1,
                        "ok": False,
                        "code": "invalid_request",
                    },
                )
                self.assertNotIn("sk-private-material", stdout.getvalue())

    def test_create_account_rejects_unsupported_credential_providers_before_mutation(
        self,
    ) -> None:
        for provider_id in ("unknown-provider", "ollama"):
            with self.subTest(provider_id=provider_id):
                events: list[tuple[object, ...]] = []
                keychain = FakeKeychain(events)
                store = FakeConfigStore(events)
                stdout = io.StringIO()

                exit_code = run_gateway_account_mutation(
                    create_account_request(
                        provider_id=provider_id,
                        secret="sk-private-material",
                    ),
                    stdout,
                    store=store,
                    keychain=keychain,
                )

                self.assertEqual(exit_code, 1)
                self.assertNotIn("keychain.set", [event[0] for event in events])
                self.assertNotIn("store.save", [event[0] for event in events])
                self.assertEqual(store.saved, [])
                self.assertEqual(
                    json.loads(stdout.getvalue()),
                    {
                        "version": 1,
                        "ok": False,
                        "code": "unsupported_provider",
                    },
                )
                self.assertNotIn("sk-private-material", stdout.getvalue())

    def test_create_account_credential_write_failure_is_sanitized(self) -> None:
        private_error = OSError(
            "private credential write failed at /Users/example/.secret"
        )
        events: list[tuple[object, ...]] = []
        keychain = FakeKeychain(events, set_error=private_error)
        store = FakeConfigStore(events)
        stdout = io.StringIO()

        exit_code = run_gateway_account_mutation(
            create_account_request(secret="sk-private-material"),
            stdout,
            store=store,
            keychain=keychain,
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            events,
            [
                ("keychain.get", "openai.work.gateway-api-key", None),
                ("keychain.set", "openai.work.gateway-api-key", "sk-private-material"),
            ],
        )
        self.assertEqual(store.saved, [])
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "version": 1,
                "ok": False,
                "code": "credential_write_failed",
            },
        )
        rendered = stdout.getvalue().casefold()
        for forbidden in (
            "private credential write failed",
            "sk-private-material",
            "/users/example/.secret",
            "gateway-api-key",
            "path",
            "secret",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_edit_account_updates_credential_and_secret_free_config(self) -> None:
        old_secret = "old-secret"
        new_secret = "new-secret"
        old_account = ProviderAccountRef(
            provider_id="openai",
            account_id="work",
            alias="Old",
            credential_account="openai.work.gateway-api-key",
        )
        updated_account = ProviderAccountRef(
            provider_id="openai",
            account_id="work",
            alias="New",
            credential_account="openai.work.gateway-api-key",
        )
        backup_account = ProviderAccountRef(
            provider_id="openai",
            account_id="backup",
            alias="Backup",
            credential_account="openai.backup.gateway-api-key",
        )
        pool = AccountPool(
            pool_id="primary",
            revision=7,
            strategy=PoolStrategy.FIXED_FIRST,
            members=(
                PoolMember(account_id="work", priority=10, weight=1),
                PoolMember(account_id="backup", priority=20, weight=2),
            ),
            cross_provider_fallback=True,
            cross_model_fallback=False,
            cross_region_fallback=True,
        )
        initial = GatewayConfig(
            enabled=True,
            mode=GatewayMode.GATEWAY,
            proxy_enabled=True,
            cache_enabled=True,
            accounts=(old_account, backup_account),
            account_pools=(pool,),
        )
        events: list[tuple[object, ...]] = []
        keychain = FakeKeychain(events, existing=old_secret)
        store = FakeConfigStore(events, initial=initial)
        stdout = io.StringIO()

        exit_code = run_gateway_account_mutation(
            edit_account_request(),
            stdout,
            store=store,
            keychain=keychain,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            [event[0] for event in events],
            ["keychain.get", "keychain.set", "store.save"],
        )
        self.assertEqual(
            events[0],
            ("keychain.get", "openai.work.gateway-api-key", old_secret),
        )
        self.assertEqual(
            events[1],
            ("keychain.set", "openai.work.gateway-api-key", new_secret),
        )
        self.assertEqual(len(store.saved), 1)
        saved = store.saved[0]
        self.assertEqual(
            [account.account_id for account in saved.accounts],
            ["work", "backup"],
        )
        self.assertEqual(saved.accounts, (updated_account, backup_account))
        self.assertEqual(saved.account_pools, (pool,))
        self.assertEqual(saved.enabled, initial.enabled)
        self.assertEqual(saved.mode, initial.mode)
        self.assertEqual(saved.host, initial.host)
        self.assertEqual(saved.port, initial.port)
        self.assertEqual(saved.proxy_enabled, initial.proxy_enabled)
        self.assertEqual(saved.cache_enabled, initial.cache_enabled)
        projection = account_pools_public_payload(
            accounts=saved.accounts,
            pools=saved.account_pools,
        )
        self.assertEqual(validate_account_pools_public_payload(projection), projection)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "version": 1,
                "ok": True,
                "code": "ok",
                "account": updated_account.to_public_dict(
                    state=AccountState.UNKNOWN
                ),
            },
        )
        rendered = stdout.getvalue().casefold()
        for forbidden in (
            old_secret,
            new_secret,
            "credential",
            "gateway-api-key",
            '"accountid"',
            "account_id",
            "openai.work.gateway-api-key",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_edit_account_alias_only_does_not_touch_keychain(self) -> None:
        old_account = ProviderAccountRef(
            provider_id="openai",
            account_id="work",
            alias="Old",
            credential_account="openai.work.gateway-api-key",
        )
        updated_account = ProviderAccountRef(
            provider_id="openai",
            account_id="work",
            alias="New",
            credential_account="openai.work.gateway-api-key",
        )
        initial = GatewayConfig(
            enabled=True,
            mode=GatewayMode.ADVISE,
            accounts=(old_account,),
        )
        events: list[tuple[object, ...]] = []
        keychain = FakeKeychain(events, existing="old-secret")
        store = FakeConfigStore(events, initial=initial)
        stdout = io.StringIO()

        exit_code = run_gateway_account_mutation(
            edit_account_request(secret=None),
            stdout,
            store=store,
            keychain=keychain,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual([event[0] for event in events], ["store.save"])
        self.assertEqual(len(store.saved), 1)
        saved = store.saved[0]
        self.assertEqual(saved.accounts, (updated_account,))
        self.assertEqual(saved.account_pools, initial.account_pools)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "version": 1,
                "ok": True,
                "code": "ok",
                "account": updated_account.to_public_dict(
                    state=AccountState.UNKNOWN
                ),
            },
        )
        rendered = stdout.getvalue().casefold()
        for forbidden in (
            "old-secret",
            "new-secret",
            "credential",
            "gateway-api-key",
            '"accountid"',
            "account_id",
            "providerkey",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_remove_account_deletes_credential_and_secret_free_config(self) -> None:
        old_secret = "old-secret"
        removed_account = ProviderAccountRef(
            provider_id="openai",
            account_id="work",
            alias="Work",
            credential_account="openai.work.gateway-api-key",
        )
        backup_account = ProviderAccountRef(
            provider_id="openai",
            account_id="backup",
            alias="Backup",
            credential_account="openai.backup.gateway-api-key",
        )
        initial = GatewayConfig(
            enabled=True,
            mode=GatewayMode.GATEWAY,
            proxy_enabled=True,
            cache_enabled=True,
            accounts=(removed_account, backup_account),
            account_pools=(),
        )
        request = io.StringIO(
            json.dumps(
                {
                    "version": 1,
                    "action": "remove_account",
                    "account": {"displayId": removed_account.display_id},
                    "credentialMaterial": {},
                },
                separators=(",", ":"),
            )
        )
        events: list[tuple[object, ...]] = []
        keychain = FakeKeychain(events, existing=old_secret)
        store = FakeConfigStore(events, initial=initial)
        stdout = io.StringIO()

        exit_code = run_gateway_account_mutation(
            request,
            stdout,
            store=store,
            keychain=keychain,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            events,
            [
                ("keychain.get", "openai.work.gateway-api-key", old_secret),
                ("keychain.delete", "openai.work.gateway-api-key", None),
                (
                    "store.save",
                    GatewayConfig(
                        enabled=True,
                        mode=GatewayMode.GATEWAY,
                        proxy_enabled=True,
                        cache_enabled=True,
                        accounts=(backup_account,),
                        account_pools=(),
                    ),
                ),
            ],
        )
        self.assertEqual(len(store.saved), 1)
        saved = store.saved[0]
        self.assertEqual(saved.accounts, (backup_account,))
        self.assertEqual(saved.account_pools, initial.account_pools)
        self.assertEqual(saved.enabled, initial.enabled)
        self.assertEqual(saved.mode, initial.mode)
        self.assertEqual(saved.host, initial.host)
        self.assertEqual(saved.port, initial.port)
        self.assertEqual(saved.proxy_enabled, initial.proxy_enabled)
        self.assertEqual(saved.cache_enabled, initial.cache_enabled)
        projection = account_pools_public_payload(
            accounts=saved.accounts,
            pools=saved.account_pools,
        )
        self.assertEqual(validate_account_pools_public_payload(projection), projection)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "version": 1,
                "ok": True,
                "code": "ok",
                "account": removed_account.to_public_dict(
                    state=AccountState.UNKNOWN
                ),
            },
        )
        rendered = stdout.getvalue().casefold()
        for forbidden in (
            old_secret,
            "credential",
            "gateway-api-key",
            "openai.work.gateway-api-key",
            '"accountid"',
            "account_id",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_remove_account_rejects_pool_references_before_keychain_access(
        self,
    ) -> None:
        referenced_account = ProviderAccountRef(
            provider_id="openai",
            account_id="work",
            alias="Work",
            credential_account="openai.work.gateway-api-key",
        )
        backup_account = ProviderAccountRef(
            provider_id="openai",
            account_id="backup",
            alias="Backup",
            credential_account="openai.backup.gateway-api-key",
        )
        pool = AccountPool(
            pool_id="primary",
            revision=1,
            strategy=PoolStrategy.FIXED_FIRST,
            members=(PoolMember(account_id="work"),),
        )
        initial = GatewayConfig(
            enabled=True,
            mode=GatewayMode.ADVISE,
            accounts=(referenced_account, backup_account),
            account_pools=(pool,),
        )
        request = io.StringIO(
            json.dumps(
                {
                    "version": 1,
                    "action": "remove_account",
                    "account": {"displayId": referenced_account.display_id},
                    "credentialMaterial": {},
                },
                separators=(",", ":"),
            )
        )
        events: list[tuple[object, ...]] = []
        keychain = FakeKeychain(events, existing="old-secret")
        store = FakeConfigStore(events, initial=initial)
        stdout = io.StringIO()

        exit_code = run_gateway_account_mutation(
            request,
            stdout,
            store=store,
            keychain=keychain,
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(events, [])
        self.assertEqual(store.saved, [])
        self.assertEqual(store.load(), initial)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "version": 1,
                "ok": False,
                "code": "pool_references_account",
            },
        )
        rendered = stdout.getvalue().casefold()
        for forbidden in (
            "old-secret",
            "credential",
            "gateway-api-key",
            "openai.work.gateway-api-key",
            '"accountid"',
            "account_id",
            "work",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_create_pool_resolves_public_members_and_saves_secret_free_pool(
        self,
    ) -> None:
        work_account = ProviderAccountRef(
            provider_id="openai",
            account_id="work",
            alias="Work",
            credential_account="openai.work.gateway-api-key",
        )
        backup_account = ProviderAccountRef(
            provider_id="openai",
            account_id="backup",
            alias="Backup",
            credential_account="openai.backup.gateway-api-key",
        )
        initial = GatewayConfig(
            enabled=True,
            mode=GatewayMode.GATEWAY,
            proxy_enabled=True,
            cache_enabled=True,
            accounts=(work_account, backup_account),
            account_pools=(),
        )
        request = io.StringIO(
            json.dumps(
                {
                    "version": 1,
                    "action": "create_pool",
                    "pool": {
                        "poolId": "primary",
                        "strategy": "fixed-first",
                        "members": [
                            {
                                "displayId": work_account.display_id,
                                "priority": 10,
                                "weight": 2,
                            },
                            {
                                "displayId": backup_account.display_id,
                                "priority": 20,
                                "weight": 1,
                            },
                        ],
                        "crossProviderFallback": True,
                        "crossModelFallback": False,
                        "crossRegionFallback": True,
                    },
                    "expectedRevision": None,
                },
                separators=(",", ":"),
            )
        )
        events: list[tuple[object, ...]] = []
        keychain = FakeKeychain(events, existing="old-secret")
        store = FakeConfigStore(events, initial=initial)
        stdout = io.StringIO()

        exit_code = run_gateway_account_mutation(
            request,
            stdout,
            store=store,
            keychain=keychain,
        )

        expected_pool = AccountPool(
            pool_id="primary",
            revision=1,
            strategy=PoolStrategy.FIXED_FIRST,
            members=(
                PoolMember(account_id="work", priority=10, weight=2),
                PoolMember(account_id="backup", priority=20, weight=1),
            ),
            cross_provider_fallback=True,
            cross_model_fallback=False,
            cross_region_fallback=True,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual([event[0] for event in events], ["store.save"])
        self.assertEqual(len(store.saved), 1)
        saved = store.saved[0]
        self.assertEqual(saved.accounts, initial.accounts)
        self.assertEqual(saved.account_pools, (expected_pool,))
        self.assertEqual(saved.enabled, initial.enabled)
        self.assertEqual(saved.mode, initial.mode)
        self.assertEqual(saved.host, initial.host)
        self.assertEqual(saved.port, initial.port)
        self.assertEqual(saved.proxy_enabled, initial.proxy_enabled)
        self.assertEqual(saved.cache_enabled, initial.cache_enabled)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "version": 1,
                "ok": True,
                "code": "ok",
                "pool": {
                    "poolId": "primary",
                    "revision": 1,
                },
            },
        )
        rendered = stdout.getvalue().casefold()
        for forbidden in (
            work_account.display_id.casefold(),
            backup_account.display_id.casefold(),
            "work",
            "backup",
            "displayid",
            "members",
            "credential",
            "gateway-api-key",
            "old-secret",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_edit_pool_replaces_existing_pool_and_increments_revision(self) -> None:
        work_account = ProviderAccountRef(
            provider_id="openai",
            account_id="work",
            alias="Work",
            credential_account="openai.work.gateway-api-key",
        )
        backup_account = ProviderAccountRef(
            provider_id="openai",
            account_id="backup",
            alias="Backup",
            credential_account="openai.backup.gateway-api-key",
        )
        existing_pool = AccountPool(
            pool_id="primary",
            revision=1,
            strategy=PoolStrategy.FIXED_FIRST,
            members=(
                PoolMember(account_id="work", priority=10, weight=2),
                PoolMember(account_id="backup", priority=20, weight=1),
            ),
            cross_provider_fallback=False,
            cross_model_fallback=False,
            cross_region_fallback=False,
        )
        initial = GatewayConfig(
            enabled=True,
            mode=GatewayMode.GATEWAY,
            proxy_enabled=True,
            cache_enabled=True,
            accounts=(work_account, backup_account),
            account_pools=(existing_pool,),
        )
        request = io.StringIO(
            json.dumps(
                {
                    "version": 1,
                    "action": "edit_pool",
                    "pool": {
                        "poolId": "primary",
                        "strategy": "quota-aware",
                        "members": [
                            {
                                "displayId": backup_account.display_id,
                                "priority": 5,
                                "weight": 3,
                            },
                            {
                                "displayId": work_account.display_id,
                                "priority": 15,
                                "weight": 1,
                            },
                        ],
                        "crossProviderFallback": True,
                        "crossModelFallback": True,
                        "crossRegionFallback": False,
                    },
                    "expectedRevision": 1,
                },
                separators=(",", ":"),
            )
        )
        events: list[tuple[object, ...]] = []
        keychain = FakeKeychain(events, existing="old-secret")
        store = FakeConfigStore(events, initial=initial)
        stdout = io.StringIO()

        exit_code = run_gateway_account_mutation(
            request,
            stdout,
            store=store,
            keychain=keychain,
        )

        updated_pool = AccountPool(
            pool_id="primary",
            revision=2,
            strategy=PoolStrategy.QUOTA_AWARE,
            members=(
                PoolMember(account_id="backup", priority=5, weight=3),
                PoolMember(account_id="work", priority=15, weight=1),
            ),
            cross_provider_fallback=True,
            cross_model_fallback=True,
            cross_region_fallback=False,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual([event[0] for event in events], ["store.save"])
        self.assertEqual(len(store.saved), 1)
        saved = store.saved[0]
        self.assertEqual(saved.accounts, initial.accounts)
        self.assertEqual(saved.account_pools, (updated_pool,))
        self.assertEqual(saved.enabled, initial.enabled)
        self.assertEqual(saved.mode, initial.mode)
        self.assertEqual(saved.host, initial.host)
        self.assertEqual(saved.port, initial.port)
        self.assertEqual(saved.proxy_enabled, initial.proxy_enabled)
        self.assertEqual(saved.cache_enabled, initial.cache_enabled)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "version": 1,
                "ok": True,
                "code": "ok",
                "pool": {
                    "poolId": "primary",
                    "revision": 2,
                },
            },
        )
        rendered = stdout.getvalue().casefold()
        for forbidden in (
            work_account.display_id.casefold(),
            backup_account.display_id.casefold(),
            "work",
            "backup",
            "members",
            "displayid",
            "credential",
            "gateway-api-key",
            "old-secret",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_remove_pool_removes_only_target_pool_and_preserves_config(self) -> None:
        work_account = ProviderAccountRef(
            provider_id="openai",
            account_id="work",
            alias="Work",
            credential_account="openai.work.gateway-api-key",
        )
        backup_account = ProviderAccountRef(
            provider_id="openai",
            account_id="backup",
            alias="Backup",
            credential_account="openai.backup.gateway-api-key",
        )
        primary_pool = AccountPool(
            pool_id="primary",
            revision=1,
            strategy=PoolStrategy.FIXED_FIRST,
            members=(PoolMember(account_id="work", priority=10, weight=2),),
        )
        secondary_pool = AccountPool(
            pool_id="secondary",
            revision=4,
            strategy=PoolStrategy.QUOTA_AWARE,
            members=(PoolMember(account_id="backup", priority=20, weight=1),),
            cross_provider_fallback=True,
            cross_model_fallback=True,
            cross_region_fallback=False,
        )
        initial = GatewayConfig(
            enabled=True,
            mode=GatewayMode.GATEWAY,
            proxy_enabled=True,
            cache_enabled=True,
            accounts=(work_account, backup_account),
            account_pools=(primary_pool, secondary_pool),
        )
        request = io.StringIO(
            json.dumps(
                {
                    "version": 1,
                    "action": "remove_pool",
                    "pool": {"poolId": "primary"},
                    "expectedRevision": 1,
                },
                separators=(",", ":"),
            )
        )
        events: list[tuple[object, ...]] = []
        keychain = FakeKeychain(events, existing="old-secret")
        store = FakeConfigStore(events, initial=initial)
        stdout = io.StringIO()

        exit_code = run_gateway_account_mutation(
            request,
            stdout,
            store=store,
            keychain=keychain,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual([event[0] for event in events], ["store.save"])
        self.assertEqual(len(store.saved), 1)
        saved = store.saved[0]
        self.assertEqual(saved.accounts, initial.accounts)
        self.assertEqual(saved.account_pools, (secondary_pool,))
        self.assertEqual(saved.enabled, initial.enabled)
        self.assertEqual(saved.mode, initial.mode)
        self.assertEqual(saved.host, initial.host)
        self.assertEqual(saved.port, initial.port)
        self.assertEqual(saved.proxy_enabled, initial.proxy_enabled)
        self.assertEqual(saved.cache_enabled, initial.cache_enabled)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "version": 1,
                "ok": True,
                "code": "ok",
                "pool": {
                    "poolId": "primary",
                    "revision": 1,
                },
            },
        )
        rendered = stdout.getvalue().casefold()
        for forbidden in (
            "work",
            "backup",
            "members",
            "displayid",
            "credential",
            "gateway-api-key",
            "old-secret",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_concurrent_create_account_commands_do_not_lose_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "gateway.json"
            public_store = GatewayConfigStore(path)
            public_store.save(GatewayConfig(enabled=True, mode=GatewayMode.ADVISE))
            events: list[tuple[object, ...]] = []
            keychain = FakeKeychain(events, get_delay_seconds=0.1)
            start = threading.Event()
            cases = (
                ("work", "Work", "sk-private-work", GatewayConfigStore(path)),
                ("backup", "Backup", "sk-private-backup", GatewayConfigStore(path)),
            )

            def run_case(case: tuple[str, str, str, GatewayConfigStore]) -> str:
                account_id, alias, secret, store = case
                output = io.StringIO()
                start.wait(timeout=2)
                exit_code = run_gateway_account_mutation(
                    create_account_request(
                        account_id=account_id,
                        alias=alias,
                        secret=secret,
                    ),
                    output,
                    store=store,
                    keychain=keychain,
                )
                payload = json.loads(output.getvalue())
                self.assertEqual(exit_code, 0, payload)
                self.assertTrue(payload["ok"], payload)
                self.assertNotIn(secret, output.getvalue())
                return output.getvalue()

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(run_case, case) for case in cases]
                start.set()
                outputs = [future.result(timeout=5) for future in futures]

            loaded = public_store.load()
            self.assertEqual(
                sorted(account.account_id for account in loaded.accounts),
                ["backup", "work"],
            )
            for output in outputs:
                self.assertNotIn("sk-private", output)


if __name__ == "__main__":
    unittest.main()
