import io
import json
import unittest
from unittest.mock import Mock

from openusage_bar.config import (
    DailyUsageFeedConfig,
    GenericProviderConfig,
    MiniMaxConfig,
    OpenAIOrganizationConfig,
    StepPlanConfig,
)
from openusage_bar.provider_commands import run_provider_mutation


class ProviderMutationCommandTests(unittest.TestCase):
    def request_v2(self, *, action, provider_id, kind, configuration, credentials):
        return io.StringIO(json.dumps({
            "version": 2,
            "action": action,
            "providerId": provider_id,
            "kind": kind,
            "configuration": configuration,
            "credentialMaterial": credentials,
        }))

    def test_v2_creates_each_managed_connection_without_returning_secrets(self):
        cases = [
            ("minimax", "minimax-work", {"name": "MiniMax Work"}),
            ("step_plan", "step-cn", {"name": "Step CN", "site": "china"}),
            ("openai_organization", "openai", {"name": "OpenAI Org"}),
            ("generic", "glm-work", {
                "name": "GLM Work", "endpoint": "https://api.example.com/quota",
                "headerName": "Authorization", "authPrefix": "Bearer",
                "primaryPath": "data.remaining", "remainingPercentPath": None,
                "resetPath": "data.reset", "detailPath": None,
            }),
            ("daily_usage_feed", "kimi-work", {
                "name": "Kimi Work", "familyId": "kimi",
                "endpoint": "https://api.example.com/daily", "headerName": "Authorization",
                "authPrefix": "Bearer", "itemsPath": "data.items", "datePath": "day",
                "modelPath": "model", "inputTokensPath": "input",
                "outputTokensPath": "output", "cacheReadTokensPath": None,
                "cacheCreationTokensPath": None, "reasoningTokensPath": None,
                "totalTokensPath": "total", "sinceParameter": "from",
                "untilParameter": "to",
            }),
        ]
        for kind, provider_id, configuration in cases:
            with self.subTest(kind=kind):
                store = Mock()
                store.load.return_value = []
                keychain = Mock()
                output = io.StringIO()
                secret = f"secret-for-{kind}"

                run_provider_mutation(
                    self.request_v2(
                        action="create_connection", provider_id=provider_id,
                        kind=kind, configuration=configuration,
                        credentials={"primary": secret, "session": ""},
                    ), output, store=store, keychain=keychain,
                    resolver=lambda _host: ["93.184.216.34"],
                )

                response = json.loads(output.getvalue())
                self.assertTrue(response["ok"], response)
                self.assertNotIn(secret, output.getvalue())
                keychain.set.assert_any_call(provider_id, secret)
                self.assertEqual(store.save.call_count, 1)

    def test_v2_rejects_duplicate_wrong_site_and_unknown_fields(self):
        existing = MiniMaxConfig("minimax-work", "MiniMax")
        for configuration in (
            {"name": "MiniMax", "unexpected": "value"},
            {"name": "Step", "site": "moon"},
        ):
            with self.subTest(configuration=configuration):
                store = Mock()
                store.load.return_value = [existing]
                output = io.StringIO()
                kind = "step_plan" if "site" in configuration else "minimax"
                provider_id = "step-work" if kind == "step_plan" else "minimax-work"
                run_provider_mutation(
                    self.request_v2(
                        action="create_connection", provider_id=provider_id,
                        kind=kind, configuration=configuration,
                        credentials={"primary": "private", "session": ""},
                    ), output, store=store, keychain=Mock(),
                )
                self.assertFalse(json.loads(output.getvalue())["ok"])
                store.save.assert_not_called()

    def test_v2_remove_restores_all_step_plan_credentials_when_config_save_fails(self):
        existing = StepPlanConfig("step-work", "Step", site="international")
        store = Mock()
        store.load.return_value = [existing]
        store.save.side_effect = OSError("private disk failure")
        keychain = Mock()
        keychain.get.side_effect = ["api", "token", "webid"]
        output = io.StringIO()

        run_provider_mutation(
            self.request_v2(
                action="remove_connection", provider_id="step-work", kind="step_plan",
                configuration={}, credentials={},
            ), output, store=store, keychain=keychain,
        )

        response = json.loads(output.getvalue())
        self.assertFalse(response["ok"])
        self.assertNotIn("api", output.getvalue())
        self.assertEqual(keychain.delete.call_count, 3)
        self.assertEqual(keychain.set.call_count, 3)

    def test_v2_requires_nonempty_credential_for_new_connection(self):
        store = Mock()
        store.load.return_value = []
        output = io.StringIO()
        run_provider_mutation(
            self.request_v2(
                action="create_connection", provider_id="minimax-work", kind="minimax",
                configuration={"name": "MiniMax"},
                credentials={"primary": "", "session": ""},
            ), output, store=store, keychain=Mock(),
        )
        self.assertFalse(json.loads(output.getvalue())["ok"])
        store.save.assert_not_called()

    def test_v2_remove_rejects_a_kind_mismatch_without_touching_credentials(self):
        store = Mock()
        store.load.return_value = [StepPlanConfig("step-work", "Step", site="china")]
        keychain = Mock()
        output = io.StringIO()
        run_provider_mutation(
            self.request_v2(
                action="remove_connection", provider_id="step-work", kind="minimax",
                configuration={}, credentials={},
            ), output, store=store, keychain=keychain,
        )
        self.assertFalse(json.loads(output.getvalue())["ok"])
        keychain.get.assert_not_called()
        keychain.delete.assert_not_called()

    def test_v2_updates_full_generic_mapping_and_keeps_a_blank_saved_key(self):
        existing = GenericProviderConfig(
            provider_id="glm-work", name="GLM", endpoint="https://api.example.com/old",
            header_name="Authorization", auth_prefix="Bearer",
            primary_path="old.remaining",
        )
        store = Mock()
        store.load.return_value = [existing]
        keychain = Mock()
        keychain.get.return_value = "saved-key"
        output = io.StringIO()
        run_provider_mutation(
            self.request_v2(
                action="update_connection", provider_id="glm-work", kind="generic",
                configuration={
                    "name": "GLM Work", "endpoint": "https://api.example.com/new",
                    "headerName": "X-API-Key", "authPrefix": "",
                    "primaryPath": "data.remaining", "remainingPercentPath": "data.percent",
                    "resetPath": None, "detailPath": None,
                },
                credentials={"primary": "", "session": ""},
            ), output, store=store, keychain=keychain,
            resolver=lambda _host: ["93.184.216.34"],
        )
        self.assertTrue(json.loads(output.getvalue())["ok"])
        saved = store.save.call_args.args[0][0]
        self.assertEqual(saved.endpoint, "https://api.example.com/new")
        self.assertEqual(saved.remaining_percent_path, "data.percent")
        keychain.set.assert_not_called()

    def test_updates_exact_step_plan_account_without_returning_credentials(self):
        store = Mock()
        store.load.return_value = [
            StepPlanConfig("step-plan-main", "Main", site="china"),
            StepPlanConfig("step-plan-work", "Work", site="international"),
        ]
        keychain = Mock()
        keychain.get.return_value = "saved"
        output = io.StringIO()
        secret = "replacement-secret"

        status = run_provider_mutation(
            io.StringIO(json.dumps({
                "version": 1,
                "action": "update_connection",
                "providerId": "step-plan-work",
                "name": "Work Updated",
                "apiKey": secret,
                "sessionCookie": "",
            })),
            output,
            store=store,
            keychain=keychain,
        )

        self.assertEqual(status, 0)
        response = json.loads(output.getvalue())
        self.assertEqual(response, {
            "version": 1,
            "ok": True,
            "message": "Step Plan updated",
        })
        self.assertNotIn(secret, output.getvalue())
        keychain.set.assert_called_once_with("step-plan-work", secret)
        saved = store.save.call_args.args[0]
        self.assertEqual(saved[0].name, "Main")
        self.assertEqual(saved[1].name, "Work Updated")
        self.assertEqual(saved[1].site, "international")

    def test_updates_non_step_plan_connection_without_accepting_a_client_type(self):
        store = Mock()
        store.load.return_value = [MiniMaxConfig("minimax-main", "MiniMax")]
        keychain = Mock()
        keychain.get.return_value = "saved-key"
        output = io.StringIO()

        status = run_provider_mutation(
            io.StringIO(json.dumps({
                "version": 1,
                "action": "update_connection",
                "providerId": "minimax-main",
                "name": "MiniMax Work",
                "apiKey": "replacement",
                "sessionCookie": "",
            })),
            output,
            store=store,
            keychain=keychain,
        )

        self.assertEqual(status, 0)
        self.assertTrue(json.loads(output.getvalue())["ok"])
        saved = store.save.call_args.args[0]
        self.assertEqual(saved, [MiniMaxConfig("minimax-main", "MiniMax Work")])

    def test_legacy_step_plan_action_cannot_update_another_provider_type(self):
        store = Mock()
        store.load.return_value = [MiniMaxConfig("minimax-main", "MiniMax")]
        output = io.StringIO()

        run_provider_mutation(
            io.StringIO(json.dumps({
                "version": 1,
                "action": "update_step_plan",
                "providerId": "minimax-main",
                "name": "Changed",
                "apiKey": "replacement",
                "sessionCookie": "",
            })),
            output,
            store=store,
            keychain=Mock(),
        )

        self.assertFalse(json.loads(output.getvalue())["ok"])
        store.save.assert_not_called()

    def test_rejects_unknown_fields_without_touching_storage(self):
        store = Mock()
        output = io.StringIO()

        status = run_provider_mutation(
            io.StringIO(json.dumps({
                "version": 1,
                "action": "update_connection",
                "providerId": "step-plan-work",
                "name": "Work",
                "apiKey": "secret",
                "sessionCookie": "",
                "endpoint": "https://attacker.invalid",
            })),
            output,
            store=store,
            keychain=Mock(),
        )

        self.assertEqual(status, 0)
        self.assertFalse(json.loads(output.getvalue())["ok"])
        store.load.assert_not_called()
        store.save.assert_not_called()

    def test_missing_account_returns_sanitized_error(self):
        store = Mock()
        store.load.return_value = []
        output = io.StringIO()

        status = run_provider_mutation(
            io.StringIO(json.dumps({
                "version": 1,
                "action": "update_connection",
                "providerId": "step-plan-missing",
                "name": "Missing",
                "apiKey": "",
                "sessionCookie": "",
            })),
            output,
            store=store,
            keychain=Mock(),
        )

        self.assertEqual(status, 0)
        self.assertEqual(
            json.loads(output.getvalue())["message"],
            "Provider connection was not found",
        )


if __name__ == "__main__":
    unittest.main()
