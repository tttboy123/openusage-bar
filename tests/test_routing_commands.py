from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from openusage_bar.routing_commands import _decode_target, run_routing_mutation
from openusage_bar.routing_execution import (
    ExecutionConnection,
    ExecutionConnectionStore,
    credential_account,
)
from openusage_bar.routing_targets import RouteTargetStore


def target_payload(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "targetId": "openai.work.gpt-5",
        "providerId": "openai",
        "accountRef": "account-1",
        "modelId": "gpt-5",
        "connectionRef": "connection-1",
        "executionClass": "direct_api",
        "executionAdapterId": "openai.direct",
        "resourceMode": "quota",
        "factAccountRef": "account-1",
        "runtimeScopeRef": None,
        "balanceCurrency": None,
        "costCurrency": "USD",
        "inputCostMicrosPerMillion": 3_000_000,
        "outputCostMicrosPerMillion": 3_000_000,
        "enabled": True,
        "regions": ["global"],
        "privacyClass": "direct_provider",
        "capabilities": ["chat", "reasoning", "tools"],
        "contextWindowTokens": 400_000,
        "qualityTier": 4,
    }
    value.update(changes)
    return value


def request(*, expected_revision: int, targets: list[dict[str, object]]) -> str:
    return json.dumps({
        "version": 1,
        "action": "replace_targets",
        "expectedRevision": expected_revision,
        "targets": targets,
    })


class RoutingMutationCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "route-targets.json"
        self.store = RouteTargetStore(self.path)
        self.connection_path = Path(self.temporary.name) / "execution-connections.json"
        self.connection_store = ExecutionConnectionStore(self.connection_path)
        self.keychain = Mock()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def mutate(self, raw: str) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        status = run_routing_mutation(
            io.StringIO(raw), output, store=self.store,
            connection_store=self.connection_store, keychain=self.keychain,
        )
        return status, json.loads(output.getvalue())

    def save_target_connection(self) -> ExecutionConnection:
        connection = ExecutionConnection(
            "connection-1", "openai", "account-1", "direct_api",
            "openai.direct", "https://api.example.com/v1", True,
            ("gpt-5", "gpt-5-mini"),
        )
        self.connection_store.save((connection,), revision=1)
        return connection

    def connection_request(
        self,
        *,
        action: str = "upsert_connection",
        expected_revision: int = 0,
        secret: str = "private-key",
        **changes: object,
    ) -> str:
        connection: dict[str, object] = {
            "connectionRef": "conn_0123456789abcdef",
            "providerId": "openai",
            "accountRef": "account-1",
            "executionClass": "openai_compatible",
            "executionAdapterId": "openai_compatible.direct",
            "baseURL": "https://api.example.com/v1",
            "enabled": True,
            "models": ["gpt-5", "gpt-5-mini"],
        }
        connection.update(changes)
        return json.dumps({
            "version": 1,
            "action": action,
            "expectedRevision": expected_revision,
            "connection": connection,
            "secret": secret,
        })

    def test_replaces_targets_with_monotonic_revision_and_private_file(self) -> None:
        self.save_target_connection()
        status, result = self.mutate(request(expected_revision=0, targets=[target_payload()]))
        self.assertEqual(status, 0)
        self.assertEqual(result, {
            "version": 1,
            "ok": True,
            "message": "Routing targets saved",
            "targetRevision": 1,
        })
        configuration = self.store.load(available_adapters=("openai.direct",))
        self.assertEqual(configuration.revision, 1)
        self.assertEqual(configuration.targets[0].target_id, "openai.work.gpt-5")
        self.assertTrue(configuration.targets[0].adapter_available)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_stale_revision_preserves_the_previous_document(self) -> None:
        self.save_target_connection()
        self.mutate(request(expected_revision=0, targets=[target_payload()]))
        before = self.path.read_bytes()
        status, result = self.mutate(request(
            expected_revision=0,
            targets=[target_payload(modelId="gpt-5-mini")],
        ))
        self.assertEqual(status, 1)
        self.assertEqual(result, {
            "version": 1,
            "ok": False,
            "message": "Routing targets changed; reload before saving",
        })
        self.assertEqual(self.path.read_bytes(), before)

    def test_save_failure_returns_sanitized_error_and_preserves_document(self) -> None:
        self.save_target_connection()
        self.mutate(request(expected_revision=0, targets=[target_payload()]))
        before = self.path.read_bytes()

        class FailingStore:
            def load(inner_self, *, available_adapters: object):
                return self.store.load(available_adapters=available_adapters)  # type: ignore[arg-type]

            def save(inner_self, targets: object, *, revision: int) -> None:
                raise OSError("private filesystem detail")

        output = io.StringIO()
        status = run_routing_mutation(
            io.StringIO(request(expected_revision=1, targets=[])),
            output,
            store=FailingStore(),  # type: ignore[arg-type]
        )
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue()), {
            "version": 1,
            "ok": False,
            "message": "Routing targets could not be saved",
        })
        self.assertEqual(self.path.read_bytes(), before)

    def test_rejects_targets_without_an_exact_execution_connection(self) -> None:
        status, result = self.mutate(request(
            expected_revision=0,
            targets=[target_payload()],
        ))
        self.assertEqual(status, 1)
        self.assertEqual(
            result["message"],
            "Routing target does not match an execution connection",
        )
        self.assertFalse(self.path.exists())

        self.save_target_connection()
        status, result = self.mutate(request(
            expected_revision=0,
            targets=[target_payload(modelId="not-registered")],
        ))
        self.assertEqual(status, 1)
        self.assertEqual(
            result["message"],
            "Routing target does not match an execution connection",
        )
        self.assertFalse(self.path.exists())

    def test_rejects_credentials_unknown_fields_duplicates_and_invalid_targets(self) -> None:
        hostile = json.loads(request(expected_revision=0, targets=[target_payload()]))
        hostile["apiKey"] = "secret-sentinel"
        cases = [
            json.dumps(hostile),
            '{"version":1,"version":1,"action":"replace_targets","expectedRevision":0,"targets":[]}',
            request(expected_revision=0, targets=[target_payload(accountRef="person@example.com")]),
            request(expected_revision=0, targets=[target_payload(capabilities=["chat", "chat"])]),
            request(expected_revision=0, targets=[target_payload(enabled=1)]),
        ]
        for raw in cases:
            with self.subTest(raw=raw[:80]):
                status, result = self.mutate(raw)
                self.assertEqual(status, 1)
                self.assertEqual(result, {
                    "version": 1,
                    "ok": False,
                    "message": "Routing target request is invalid",
                })
                self.assertFalse(self.path.exists())
                self.assertNotIn("secret", json.dumps(result).lower())

    def test_rejects_oversized_input_without_touching_storage(self) -> None:
        status, result = self.mutate(" " * (262_144 + 1))
        self.assertEqual(status, 1)
        self.assertFalse(result["ok"])
        self.assertFalse(self.path.exists())

    def test_creates_execution_connection_and_keeps_secret_only_in_keychain(self) -> None:
        self.keychain.get.return_value = None

        status, result = self.mutate(self.connection_request())

        self.assertEqual(status, 0)
        self.assertEqual(result, {
            "version": 1, "ok": True,
            "message": "Execution connection saved", "connectionRevision": 1,
        })
        saved = self.connection_store.load().connections[0]
        self.assertEqual(saved.connection_ref, "conn_0123456789abcdef")
        self.keychain.set.assert_called_once_with(
            credential_account(saved.connection_ref), "private-key"
        )
        self.assertNotIn("private-key", self.connection_path.read_text())

        status, listed = self.mutate(json.dumps({
            "version": 1, "action": "list_connections",
        }))
        self.assertEqual(status, 0)
        self.assertEqual(listed["connectionRevision"], 1)
        self.assertEqual(listed["connections"], [{
            "connectionRef": "conn_0123456789abcdef",
            "providerId": "openai",
            "accountRef": "account-1",
            "executionClass": "openai_compatible",
            "executionAdapterId": "openai_compatible.direct",
            "baseURL": "https://api.example.com/v1",
            "enabled": True,
            "models": ["gpt-5", "gpt-5-mini"],
        }])
        self.assertNotIn("private-key", json.dumps(listed))

    def test_execution_connection_accepts_the_store_limit_of_256_models(self) -> None:
        self.keychain.get.return_value = None
        models = [f"model-{index}" for index in range(256)]

        status, result = self.mutate(self.connection_request(models=models))

        self.assertEqual(status, 0)
        self.assertEqual(result["connectionRevision"], 1)
        self.assertEqual(
            self.connection_store.load().connections[0].models,
            tuple(sorted(models)),
        )

    def test_update_can_retain_secret_but_cannot_orphan_an_existing_target(self) -> None:
        existing = ExecutionConnection(
            "conn_0123456789abcdef", "openai", "account-1",
            "openai_compatible", "openai_compatible.direct",
            "https://api.example.com/v1", True, ("gpt-5", "gpt-5-mini"),
        )
        self.connection_store.save((existing,), revision=1)
        self.store.save((
            _decode_target(target_payload(
                connectionRef=existing.connection_ref,
                executionClass=existing.execution_class,
                executionAdapterId=existing.execution_adapter_id,
            )),
        ), revision=1)

        status, result = self.mutate(self.connection_request(
            expected_revision=1, secret="", models=["gpt-5"]
        ))
        self.assertEqual(status, 0)
        self.assertEqual(result["connectionRevision"], 2)
        self.keychain.set.assert_not_called()

        before = self.connection_path.read_bytes()
        status, result = self.mutate(self.connection_request(
            expected_revision=2, secret="", models=["gpt-5-mini"]
        ))
        self.assertEqual(status, 1)
        self.assertEqual(result["message"], "Execution connection conflicts with routing targets")
        self.assertEqual(self.connection_path.read_bytes(), before)

    def test_remove_rejects_referenced_connection_and_rolls_back_keychain_on_save_failure(self) -> None:
        existing = ExecutionConnection(
            "conn_0123456789abcdef", "openai", "account-1",
            "openai_compatible", "openai_compatible.direct",
            "https://api.example.com/v1", True, ("gpt-5",),
        )
        self.connection_store.save((existing,), revision=1)
        self.store.save((
            _decode_target(target_payload(
                connectionRef=existing.connection_ref,
                executionClass=existing.execution_class,
                executionAdapterId=existing.execution_adapter_id,
            )),
        ), revision=1)
        remove = json.dumps({
            "version": 1, "action": "remove_connection",
            "expectedRevision": 1, "connectionRef": existing.connection_ref,
        })
        status, result = self.mutate(remove)
        self.assertEqual(status, 1)
        self.assertEqual(result["message"], "Execution connection is used by routing targets")
        self.keychain.delete.assert_not_called()

        self.store.save((), revision=2)

        class FailingStore:
            def load(inner_self):
                return self.connection_store.load()

            def save(inner_self, connections: object, *, revision: int):
                raise OSError("private failure")

        self.keychain.get.return_value = "old-secret"
        output = io.StringIO()
        status = run_routing_mutation(
            io.StringIO(remove), output, store=self.store,
            connection_store=FailingStore(), keychain=self.keychain,
        )
        self.assertEqual(status, 1)
        self.keychain.delete.assert_called_once_with(
            credential_account(existing.connection_ref)
        )
        self.keychain.set.assert_called_once_with(
            credential_account(existing.connection_ref), "old-secret"
        )


if __name__ == "__main__":
    unittest.main()
