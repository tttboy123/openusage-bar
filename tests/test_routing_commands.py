from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from openusage_bar.routing_commands import _decode_target, run_routing_mutation
from openusage_bar.routing_execution import (
    ExecutionConnection,
    ExecutionConnectionStore,
    credential_account,
)
from openusage_bar.routing_targets import RouteTargetStore
from openusage_bar.routing_policy_store import RoutingPolicyStore
from openusage_bar.routing_preferences import RoutingPreferencesStore
from openusage_bar.config import (
    MiniMaxConfig,
    OpenAIOrganizationConfig,
    ProviderConfigStore,
    StepPlanConfig,
)


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
        self.policy_path = Path(self.temporary.name) / "routing-policies.json"
        self.policy_store = RoutingPolicyStore(self.policy_path)
        self.preferences_path = Path(self.temporary.name) / "routing-preferences.json"
        self.preferences_store = RoutingPreferencesStore(self.preferences_path)
        self.provider_path = Path(self.temporary.name) / "providers.json"
        self.provider_store = ProviderConfigStore(self.provider_path)
        self.keychain = Mock()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def mutate(self, raw: str) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        status = run_routing_mutation(
            io.StringIO(raw), output, store=self.store,
            connection_store=self.connection_store, keychain=self.keychain,
            policy_store=self.policy_store,
            preferences_store=self.preferences_store,
            provider_store=self.provider_store,
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

    def policy_request(
        self,
        *,
        action: str = "upsert_policy",
        expected_revision: int = 0,
        **changes: object,
    ) -> str:
        policy: dict[str, object] = {
            "policyId": "custom_coding",
            "reliabilityWeight": 45,
            "headroomWeight": 25,
            "latencyWeight": 20,
            "costWeight": 10,
            "minimumHeadroomBasisPoints": 1_000,
            "maximumErrorRateBasisPoints": 1_500,
            "minimumRuntimeSamples": 3,
            "latencyReferenceMilliseconds": 8_000,
            "costReferenceMicrounits": 2_000,
            "costCurrency": "USD",
            "minimumBalanceMicrounits": 2_000_000,
            "balanceReferenceMicrounits": 25_000_000,
            "unknownPenaltyBasisPoints": 3_000,
            "requireCost": False,
            "requireRuntime": False,
            "allowedExecutionClasses": ["direct_api", "openai_compatible"],
            "allowedPrivacyClasses": ["direct_provider"],
        }
        policy.update(changes)
        return json.dumps({
            "version": 1,
            "action": action,
            "expectedRevision": expected_revision,
            "policy": policy,
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

    def test_lists_only_explicit_provider_center_inference_templates_without_secrets(self) -> None:
        self.provider_store.save([
            MiniMaxConfig("minimax-cn", "MiniMax CN", site="china"),
            StepPlanConfig("step-global", "Step Global", site="international"),
            OpenAIOrganizationConfig("openai-admin", "OpenAI Admin"),
        ])
        self.keychain.get.side_effect = lambda account: (
            "minimax-private-value" if account == "minimax-cn" else None
        )

        status, result = self.mutate(json.dumps({
            "version": 1, "action": "list_provider_execution_templates",
        }))

        self.assertEqual(status, 0)
        self.assertEqual(result["message"], "Provider execution templates loaded")
        self.assertEqual(
            [item["providerId"] for item in result["providerExecutionTemplates"]],
            ["minimax-cn", "step-global"],
        )
        self.assertEqual(
            result["providerExecutionTemplates"][0],
            {
                "providerId": "minimax-cn",
                "familyId": "minimax",
                "displayName": "MiniMax CN",
                "site": "china",
                "baseURL": "https://api.minimaxi.com/v1",
                "suggestedModels": ["MiniMax-M2.7", "MiniMax-M2.7-highspeed"],
                "credentialAvailable": True,
                "factAccountRef": None,
            },
        )
        encoded = json.dumps(result)
        self.assertNotIn("minimax-private-value", encoded)
        self.assertNotIn("openai-admin", encoded)

    def test_imports_provider_key_into_isolated_routing_account_without_exposing_it(self) -> None:
        self.provider_store.save([
            StepPlanConfig(
                "step-main", "Step Plan", site="china", account_ref="step-work"
            ),
        ])
        self.keychain.get.side_effect = lambda account: {
            "step-main": "step-private-value",
            credential_account("conn_step_main"): None,
        }.get(account)

        status, result = self.mutate(json.dumps({
            "version": 1,
            "action": "import_provider_execution_connection",
            "expectedRevision": 0,
            "providerId": "step-main",
            "connectionRef": "conn_step_main",
            "models": ["step-3.5-flash", "step-router-v1"],
            "enabled": True,
        }))

        self.assertEqual(status, 0)
        self.assertEqual(result, {
            "version": 1, "ok": True,
            "message": "Provider execution connection imported",
            "connectionRevision": 1,
        })
        connection = self.connection_store.load().connections[0]
        self.assertEqual(connection.provider_id, "step-main")
        self.assertEqual(connection.account_ref, "step-work")
        self.assertEqual(connection.base_url, "https://api.stepfun.com/step_plan/v1")
        self.assertEqual(connection.models, ("step-3.5-flash", "step-router-v1"))
        self.keychain.set.assert_called_once_with(
            credential_account("conn_step_main"), "step-private-value"
        )
        self.assertNotIn("step-private-value", self.connection_path.read_text())
        self.assertNotIn("step-private-value", json.dumps(result))

    def test_import_fails_closed_for_missing_or_ineligible_provider_credentials(self) -> None:
        self.provider_store.save([
            MiniMaxConfig("minimax-main", "MiniMax", site="china"),
            OpenAIOrganizationConfig("openai-admin", "OpenAI Admin"),
        ])
        self.keychain.get.return_value = None
        cases = [
            ("minimax-main", "Provider inference credential unavailable"),
            ("openai-admin", "Provider execution import is unavailable"),
            ("missing", "Provider execution import is unavailable"),
        ]
        for provider_id, message in cases:
            with self.subTest(provider=provider_id):
                status, result = self.mutate(json.dumps({
                    "version": 1,
                    "action": "import_provider_execution_connection",
                    "expectedRevision": 0,
                    "providerId": provider_id,
                    "connectionRef": "conn_import_test",
                    "models": ["safe-model"],
                    "enabled": True,
                }))
                self.assertEqual(status, 1)
                self.assertEqual(result["message"], message)
                self.assertFalse(self.connection_path.exists())
                self.keychain.set.assert_not_called()

    def test_provider_template_listing_survives_unavailable_native_keychain(self) -> None:
        self.provider_store.save([
            MiniMaxConfig("minimax-main", "MiniMax", site="china"),
        ])
        output = io.StringIO()
        readonly = Mock()
        readonly.get.return_value = "bounded-private-value"

        with patch(
            "openusage_bar.routing_commands.MacOSKeychain",
            side_effect=ModuleNotFoundError("No module named 'Security'"),
        ) as native, patch(
            "openusage_bar.routing_commands.BoundedReadOnlyKeychain",
            return_value=readonly,
        ) as bounded:
            status = run_routing_mutation(
                io.StringIO(json.dumps({
                    "version": 1,
                    "action": "list_provider_execution_templates",
                })),
                output,
                store=self.store,
                connection_store=self.connection_store,
                policy_store=self.policy_store,
                preferences_store=self.preferences_store,
                provider_store=self.provider_store,
            )

        result = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["providerExecutionTemplates"][0]["credentialAvailable"],
            True,
        )
        bounded.assert_called_once_with()
        readonly.get.assert_called_once_with("minimax-main")
        native.assert_not_called()
        self.assertNotIn("bounded-private-value", output.getvalue())

    def test_provider_import_reports_unavailable_native_keychain_without_leaking_details(self) -> None:
        self.provider_store.save([
            MiniMaxConfig("minimax-main", "MiniMax", site="china"),
        ])
        output = io.StringIO()

        with patch(
            "openusage_bar.routing_commands.MacOSKeychain",
            side_effect=ModuleNotFoundError("sensitive native failure"),
        ):
            status = run_routing_mutation(
                io.StringIO(json.dumps({
                    "version": 1,
                    "action": "import_provider_execution_connection",
                    "expectedRevision": 0,
                    "providerId": "minimax-main",
                    "connectionRef": "conn_minimax_main",
                    "models": ["MiniMax-M2.7"],
                    "enabled": True,
                })),
                output,
                store=self.store,
                connection_store=self.connection_store,
                policy_store=self.policy_store,
                preferences_store=self.preferences_store,
                provider_store=self.provider_store,
            )

        result = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertEqual(result["message"], "Provider inference credential unavailable")
        self.assertNotIn("sensitive native failure", output.getvalue())
        self.assertFalse(self.connection_path.exists())

    def test_custom_policy_lifecycle_uses_document_and_policy_revisions(self) -> None:
        status, result = self.mutate(self.policy_request())
        self.assertEqual(status, 0)
        self.assertEqual(result["policyDocumentRevision"], 1)

        status, listed = self.mutate(json.dumps({
            "version": 1, "action": "list_policies",
        }))
        self.assertEqual(status, 0)
        self.assertEqual(listed["policyDocumentRevision"], 1)
        self.assertEqual(listed["customPolicies"][0]["policyId"], "custom_coding")
        self.assertEqual(listed["customPolicies"][0]["policyRevision"], 1)

        status, result = self.mutate(self.policy_request(
            expected_revision=1,
            reliabilityWeight=40,
            headroomWeight=30,
        ))
        self.assertEqual(status, 0)
        self.assertEqual(result["policyDocumentRevision"], 2)
        self.assertEqual(self.policy_store.load().policies[0].revision, 2)

        remove = json.dumps({
            "version": 1, "action": "remove_policy",
            "expectedRevision": 2, "policyId": "custom_coding",
        })
        status, result = self.mutate(remove)
        self.assertEqual(status, 0)
        self.assertEqual(result["policyDocumentRevision"], 3)
        self.assertEqual(self.policy_store.load().policies, ())

    def test_custom_policy_mutation_rejects_builtin_and_stale_updates(self) -> None:
        status, result = self.mutate(self.policy_request(policyId="reliable"))
        self.assertEqual(status, 1)
        self.assertEqual(result["message"], "Custom routing policy request is invalid")

        self.mutate(self.policy_request())
        before = self.policy_path.read_bytes()
        status, result = self.mutate(self.policy_request(expected_revision=0))
        self.assertEqual(status, 1)
        self.assertEqual(
            result["message"], "Routing policies changed; reload before saving"
        )
        self.assertEqual(self.policy_path.read_bytes(), before)

    def test_preferences_toggle_and_default_policy_are_atomic_and_bounded(self) -> None:
        status, listed = self.mutate(json.dumps({
            "version": 1, "action": "get_preferences",
        }))
        self.assertEqual(status, 0)
        self.assertEqual(listed, {
            "version": 1,
            "ok": True,
            "message": "Routing preferences loaded",
            "preferencesRevision": 0,
            "routingPreferences": {
                "decisionApiEnabled": True,
                "defaultPolicyId": "reliable",
            },
        })

        status, result = self.mutate(json.dumps({
            "version": 1,
            "action": "set_preferences",
            "expectedRevision": 0,
            "decisionApiEnabled": False,
            "defaultPolicyId": "balanced",
        }))
        self.assertEqual(status, 0)
        self.assertEqual(result["preferencesRevision"], 1)
        self.assertEqual(self.preferences_store.load().default_policy_id, "balanced")
        self.assertFalse(self.preferences_store.load().decision_api_enabled)

        before = self.preferences_path.read_bytes()
        status, result = self.mutate(json.dumps({
            "version": 1,
            "action": "set_preferences",
            "expectedRevision": 0,
            "decisionApiEnabled": True,
            "defaultPolicyId": "reliable",
        }))
        self.assertEqual(status, 1)
        self.assertEqual(
            result["message"],
            "Routing preferences changed; reload before saving",
        )
        self.assertEqual(self.preferences_path.read_bytes(), before)

    def test_preferences_require_an_existing_policy_and_strict_boolean(self) -> None:
        cases = [
            {
                "version": 1, "action": "set_preferences",
                "expectedRevision": 0, "decisionApiEnabled": 1,
                "defaultPolicyId": "reliable",
            },
            {
                "version": 1, "action": "set_preferences",
                "expectedRevision": 0, "decisionApiEnabled": True,
                "defaultPolicyId": "missing_policy",
            },
        ]
        for value in cases:
            with self.subTest(value=value):
                status, result = self.mutate(json.dumps(value))
                self.assertEqual(status, 1)
                self.assertEqual(
                    result["message"], "Routing preferences request is invalid"
                )
                self.assertFalse(self.preferences_path.exists())

    def test_default_custom_policy_cannot_be_removed_until_default_changes(self) -> None:
        self.mutate(self.policy_request())
        status, _ = self.mutate(json.dumps({
            "version": 1,
            "action": "set_preferences",
            "expectedRevision": 0,
            "decisionApiEnabled": True,
            "defaultPolicyId": "custom_coding",
        }))
        self.assertEqual(status, 0)

        remove = json.dumps({
            "version": 1, "action": "remove_policy",
            "expectedRevision": 1, "policyId": "custom_coding",
        })
        status, result = self.mutate(remove)
        self.assertEqual(status, 1)
        self.assertEqual(result["message"], "Default routing policy cannot be removed")
        self.assertEqual(self.policy_store.load().policies[0].policy_id, "custom_coding")

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
