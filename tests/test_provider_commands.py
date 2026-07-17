import io
import json
import unittest
from unittest.mock import Mock

from openusage_bar.config import StepPlanConfig
from openusage_bar.provider_commands import run_provider_mutation


class ProviderMutationCommandTests(unittest.TestCase):
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
                "action": "update_step_plan",
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

    def test_rejects_unknown_fields_without_touching_storage(self):
        store = Mock()
        output = io.StringIO()

        status = run_provider_mutation(
            io.StringIO(json.dumps({
                "version": 1,
                "action": "update_step_plan",
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
                "action": "update_step_plan",
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
            "Step Plan connection was not found",
        )


if __name__ == "__main__":
    unittest.main()
