import json
import os
import stat
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from openusage_bar.routing_contract import RouteTarget
from openusage_bar.routing_execution import (
    ExecutionConnection,
    ExecutionConnectionConfigError,
    ExecutionConnectionStore,
    ExecutionAuthenticationError,
    ExecutionRegistry,
    ExecutionResolutionError,
    OpenAICompatibleExecutionAdapter,
    credential_account,
)


@dataclass(frozen=True)
class FakeAdapter:
    adapter_id: str
    execution_class: str

    def execute_chat(
        self,
        connection: ExecutionConnection,
        model_id: str,
        body: dict[str, object],
    ) -> dict[str, object]:
        return {"model": model_id, "connection": connection.connection_ref}


class FakeKeychain:
    def __init__(self, secret: str | None) -> None:
        self.secret = secret
        self.accounts: list[str] = []

    def get(self, account: str) -> str | None:
        self.accounts.append(account)
        return self.secret


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str], dict[str, object]]] = []

    def post_json(
        self,
        endpoint: str,
        headers: dict[str, str],
        body: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append((endpoint, headers, body))
        return {"id": "opaque", "model": body["model"], "choices": []}


def connection(**overrides: object) -> ExecutionConnection:
    values: dict[str, object] = {
        "connection_ref": "conn_0123456789abcdef",
        "provider_id": "openai",
        "account_ref": "account-1",
        "execution_class": "openai_compatible",
        "execution_adapter_id": "openai_compatible.direct",
        "base_url": "https://api.example.com/v1",
        "enabled": True,
        "models": ("gpt-5", "gpt-5-mini"),
    }
    values.update(overrides)
    return ExecutionConnection(**values)


def target(**overrides: object) -> RouteTarget:
    values: dict[str, object] = {
        "target_id": "openai.work.gpt-5",
        "provider_id": "openai",
        "account_ref": "account-1",
        "model_id": "gpt-5",
        "connection_ref": "conn_0123456789abcdef",
        "execution_class": "openai_compatible",
        "execution_adapter_id": "openai_compatible.direct",
        "resource_mode": "quota",
        "fact_account_ref": "account-1",
        "runtime_scope_ref": None,
        "balance_currency": None,
        "cost_currency": "USD",
        "input_cost_micros_per_million": 1_000_000,
        "output_cost_micros_per_million": 2_000_000,
        "enabled": True,
        "adapter_available": True,
        "regions": ("global",),
        "privacy_class": "direct_provider",
        "capabilities": ("chat",),
        "context_window_tokens": 128_000,
        "quality_tier": 4,
    }
    values.update(overrides)
    return RouteTarget(**values)


class ExecutionConnectionStoreTests(unittest.TestCase):
    def test_round_trip_is_private_canonical_and_contains_no_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution-connections.json"
            store = ExecutionConnectionStore(path)

            store.save((connection(),), revision=1)

            self.assertEqual(store.load().connections, (connection(),))
            self.assertEqual(store.load().revision, 1)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            payload = path.read_text(encoding="utf-8")
            self.assertNotIn("apiKey", payload)
            self.assertNotIn("credential", payload.casefold())
            self.assertEqual(json.loads(payload)["schemaVersion"], 1)

    def test_rejects_unknown_duplicate_secret_unsafe_url_and_non_private_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution-connections.json"
            valid = {
                "schemaVersion": 1,
                "revision": 1,
                "connections": [{
                    "connectionRef": "conn_0123456789abcdef",
                    "providerId": "openai",
                    "accountRef": "account-1",
                    "executionClass": "openai_compatible",
                    "executionAdapterId": "openai_compatible.direct",
                    "baseURL": "https://api.example.com/v1",
                    "enabled": True,
                    "models": ["gpt-5"],
                }],
            }
            invalid_documents = [
                {**valid, "apiKey": "not-a-real-secret"},
                {**valid, "connections": [valid["connections"][0], valid["connections"][0]]},
                {**valid, "connections": [{**valid["connections"][0], "baseURL": "http://api.example.com/v1"}]},
                {**valid, "connections": [{**valid["connections"][0], "baseURL": "https://user@example.com/v1"}]},
            ]
            store = ExecutionConnectionStore(path)
            for document in invalid_documents:
                path.write_text(json.dumps(document), encoding="utf-8")
                os.chmod(path, 0o600)
                with self.subTest(document=document), self.assertRaises(ExecutionConnectionConfigError):
                    store.load()
            path.write_text(json.dumps(valid), encoding="utf-8")
            os.chmod(path, 0o644)
            with self.assertRaises(ExecutionConnectionConfigError):
                store.load()

    def test_save_requires_monotonic_revision_and_preserves_last_good(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution-connections.json"
            store = ExecutionConnectionStore(path)
            store.save((connection(),), revision=3)
            before = path.read_bytes()

            with self.assertRaises(ExecutionConnectionConfigError):
                store.save((connection(enabled=False),), revision=3)

            self.assertEqual(path.read_bytes(), before)

    def test_root_base_url_normalizes_once_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution-connections.json"
            store = ExecutionConnectionStore(path)
            normalized = connection(base_url="https://api.example.com/")
            self.assertEqual(normalized.base_url, "https://api.example.com")
            store.save((normalized,), revision=1)
            self.assertEqual(store.load().connections, (normalized,))


class ExecutionRegistryTests(unittest.TestCase):
    def test_only_real_matching_enabled_adapters_make_connections_available(self) -> None:
        adapter = FakeAdapter("openai_compatible.direct", "openai_compatible")
        registry = ExecutionRegistry(
            adapters=(adapter,),
            connections=(
                connection(),
                connection(connection_ref="conn_disabled", enabled=False),
                connection(
                    connection_ref="conn_missing_adapter",
                    execution_adapter_id="missing.direct",
                ),
            ),
        )

        self.assertEqual(registry.available_adapter_ids(), ("openai_compatible.direct",))
        self.assertEqual(registry.available_connection_refs(), ("conn_0123456789abcdef",))
        resolved_adapter, resolved_connection = registry.resolve(target())
        self.assertIs(resolved_adapter, adapter)
        self.assertEqual(resolved_connection, connection())

    def test_resolution_fails_closed_across_every_execution_identity_boundary(self) -> None:
        registry = ExecutionRegistry(
            adapters=(FakeAdapter("openai_compatible.direct", "openai_compatible"),),
            connections=(connection(),),
        )
        invalid_targets = (
            target(provider_id="other"),
            target(account_ref="account-2"),
            target(model_id="not-allowed"),
            target(connection_ref="missing"),
            target(execution_adapter_id="other.direct"),
            target(execution_class="direct_api"),
            target(enabled=False),
        )
        for value in invalid_targets:
            with self.subTest(value=value), self.assertRaises(ExecutionResolutionError):
                registry.resolve(value)

    def test_duplicate_adapter_or_connection_identity_is_rejected(self) -> None:
        adapter = FakeAdapter("openai_compatible.direct", "openai_compatible")
        with self.assertRaises(ValueError):
            ExecutionRegistry(adapters=(adapter, adapter), connections=())
        with self.assertRaises(ValueError):
            ExecutionRegistry(adapters=(), connections=(connection(), connection()))


class OpenAICompatibleExecutionAdapterTests(unittest.TestCase):
    def test_executes_one_nonstreaming_request_with_isolated_keychain_account(self) -> None:
        keychain = FakeKeychain("test-secret")
        client = FakeClient()
        adapter = OpenAICompatibleExecutionAdapter(keychain=keychain, client=client)
        body: dict[str, object] = {
            "model": "openusage/reliable",
            "messages": [{"role": "user", "content": "kept only in memory"}],
            "stream": False,
        }

        response = adapter.execute_chat(connection(), "gpt-5", body)

        self.assertEqual(response["model"], "gpt-5")
        self.assertEqual(body["model"], "openusage/reliable")
        self.assertEqual(keychain.accounts, [credential_account(connection().connection_ref)])
        endpoint, headers, sent = client.calls[0]
        self.assertEqual(endpoint, "https://api.example.com/v1/chat/completions")
        self.assertEqual(headers, {"Authorization": "Bearer test-secret"})
        self.assertEqual(sent["model"], "gpt-5")

    def test_missing_secret_disallowed_model_streaming_and_large_body_fail_closed(self) -> None:
        missing = OpenAICompatibleExecutionAdapter(
            keychain=FakeKeychain(None),
            client=FakeClient(),
        )
        with self.assertRaises(ExecutionAuthenticationError):
            missing.execute_chat(connection(), "gpt-5", {"messages": []})

        adapter = OpenAICompatibleExecutionAdapter(
            keychain=FakeKeychain("test-secret"),
            client=FakeClient(),
        )
        invalid = (
            ("not-allowed", {"messages": []}),
            ("gpt-5", {"messages": [], "stream": True}),
            ("gpt-5", {"messages": [{"content": "x" * (2 * 1024 * 1024)}]}),
        )
        for model_id, body in invalid:
            with self.subTest(model_id=model_id), self.assertRaises(ValueError):
                adapter.execute_chat(connection(), model_id, body)


if __name__ == "__main__":
    unittest.main()
