import io
import json
import unittest

from openusage_bar.gateway.accounts import ProviderAccountRef
from openusage_bar.gateway.config import GatewayConfig


def _account(
    provider_id: str = "openai",
    account_id: str = "account-0123456789abcdef0123456789abcdef",
    alias: str = "Work",
) -> ProviderAccountRef:
    return ProviderAccountRef(
        provider_id=provider_id,
        account_id=account_id,
        alias=alias,
        credential_account=f"{provider_id}.{account_id}.gateway-api-key",
    )


class FakeStore:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config

    def load(self) -> GatewayConfig:
        return self.config


class FakeView:
    def __init__(
        self,
        *,
        alias: str | None = "Work",
        secret: str | None = "sk-private",
        confirm: bool = True,
    ) -> None:
        self.alias = alias
        self.secret = secret
        self.confirm = confirm
        self.create_prompts = []
        self.alias_prompts = []
        self.secret_prompts = []
        self.confirm_prompts = []

    def prompt_create(self, intent, on_ready):
        self.create_prompts.append(intent)
        on_ready()
        if self.alias is None or self.secret is None:
            return None
        return self.alias, self.secret

    def prompt_alias(self, intent, account, on_ready):
        self.alias_prompts.append((intent, account))
        on_ready()
        return self.alias

    def prompt_secret(self, intent, account=None, on_ready=lambda: None):
        self.secret_prompts.append((intent, account))
        on_ready()
        return self.secret

    def confirm_remove(self, account, on_ready):
        self.confirm_prompts.append(account)
        on_ready()
        return self.confirm


class FakeMutation:
    def __init__(self, response: dict[str, object] | None = None) -> None:
        self.response = response or {"version": 1, "ok": True, "code": "ok"}
        self.requests = []

    def __call__(self, request: dict[str, object]) -> dict[str, object]:
        self.requests.append(request)
        return self.response


def _run(intent: dict[str, object], *, store=None, view=None, mutation=None):
    from openusage_bar.gateway.account_editor_model import GatewayAccountEditorController

    output = io.StringIO()
    controller = GatewayAccountEditorController(
        store=store or FakeStore(GatewayConfig()),
        view=view or FakeView(),
        mutation=mutation or FakeMutation(),
    )
    code = controller.run_from_stream(io.StringIO(json.dumps(intent)), output)
    return code, [json.loads(line) for line in output.getvalue().splitlines()]


class GatewayAccountEditorModelTests(unittest.TestCase):
    def test_create_collects_secret_in_view_and_writes_only_sanitized_events(self):
        view = FakeView(secret="sk-create-private")
        mutation = FakeMutation()

        code, events = _run(
            {
                "apiVersion": "gateway-account-host.openusage/v1",
                "action": "gatewayAccount.openCreate",
                "preset": "openai",
            },
            view=view,
            mutation=mutation,
        )

        self.assertEqual(code, 0)
        self.assertEqual(events, [
            {"version": 1, "event": "ready"},
            {"version": 1, "event": "terminal", "state": "succeeded", "code": "ok"},
        ])
        self.assertEqual(view.create_prompts[0].preset, "openai")
        self.assertEqual(mutation.requests, [{
            "version": 1,
            "action": "create_account",
            "account": {"providerId": "openai", "alias": "Work"},
            "credentialMaterial": {"providerKey": "sk-create-private"},
        }])
        rendered = json.dumps(events)
        self.assertNotIn("sk-create-private", rendered)
        self.assertNotIn("account-", rendered)
        self.assertNotIn("gateway-api-key", rendered)

    def test_rejects_private_or_credential_fields_before_prompt_or_mutation(self):
        view = FakeView()
        mutation = FakeMutation()

        for field in ("accountId", "credentialMaterial", "providerKey", "endpoint", "path"):
            with self.subTest(field=field):
                code, events = _run(
                    {
                        "apiVersion": "gateway-account-host.openusage/v1",
                        "action": "gatewayAccount.openCreate",
                        "preset": "openai",
                        field: "private",
                    },
                    view=view,
                    mutation=mutation,
                )
                self.assertEqual(code, 1)
                self.assertEqual(events[-1], {
                    "version": 1,
                    "event": "terminal",
                    "state": "failed",
                    "code": "invalid_intent",
                })

        self.assertEqual(view.create_prompts, [])
        self.assertEqual(mutation.requests, [])

    def test_invalid_intent_and_not_found_do_not_emit_ready(self):
        mutation = FakeMutation()
        constructed = []

        def view_factory():
            constructed.append(True)
            return FakeView()

        from openusage_bar.gateway.account_editor_model import GatewayAccountEditorController

        output = io.StringIO()
        controller = GatewayAccountEditorController(
            store=FakeStore(GatewayConfig()),
            view=view_factory,
            mutation=mutation,
        )

        code = controller.run_from_stream(
            io.StringIO(json.dumps({
                "apiVersion": "gateway-account-host.openusage/v1",
                "action": "gatewayAccount.openCreate",
                "preset": "openai",
                "credentialMaterial": {},
            })),
            output,
        )
        events = [json.loads(line) for line in output.getvalue().splitlines()]

        self.assertEqual(code, 1)
        self.assertEqual(events, [{
            "version": 1,
            "event": "terminal",
            "state": "failed",
            "code": "invalid_intent",
        }])
        self.assertEqual(constructed, [])

        code, events = _run(
            {
                "apiVersion": "gateway-account-host.openusage/v1",
                "action": "gatewayAccount.openEdit",
                "displayId": "acct_000000000000",
            },
            store=FakeStore(GatewayConfig()),
            mutation=mutation,
        )

        self.assertEqual(code, 1)
        self.assertEqual(events, [{
            "version": 1,
            "event": "terminal",
            "state": "failed",
            "code": "not_found",
        }])
        self.assertEqual(mutation.requests, [])

    def test_edit_alias_only_resolves_display_id_and_collects_alias_in_view(self):
        account = _account(alias="Old")
        view = FakeView(alias="New")
        mutation = FakeMutation()

        code, events = _run(
            {
                "apiVersion": "gateway-account-host.openusage/v1",
                "action": "gatewayAccount.openEdit",
                "displayId": account.display_id,
            },
            store=FakeStore(GatewayConfig(accounts=(account,))),
            view=view,
            mutation=mutation,
        )

        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["state"], "succeeded")
        self.assertEqual(view.secret_prompts, [])
        self.assertEqual(view.alias_prompts[0][1], account)
        self.assertEqual(mutation.requests, [{
            "version": 1,
            "action": "edit_account",
            "account": {"displayId": account.display_id, "alias": "New"},
            "credentialMaterial": {"providerKey": None},
        }])

    def test_replace_collects_secret_and_preserves_existing_alias(self):
        account = _account(alias="Keep")
        view = FakeView(secret="sk-replacement")
        mutation = FakeMutation()

        code, events = _run(
            {
                "apiVersion": "gateway-account-host.openusage/v1",
                "action": "gatewayAccount.openReplace",
                "displayId": account.display_id,
            },
            store=FakeStore(GatewayConfig(accounts=(account,))),
            view=view,
            mutation=mutation,
        )

        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["state"], "succeeded")
        self.assertEqual(mutation.requests, [{
            "version": 1,
            "action": "edit_account",
            "account": {"displayId": account.display_id, "alias": "Keep"},
            "credentialMaterial": {"providerKey": "sk-replacement"},
        }])
        self.assertNotIn("sk-replacement", json.dumps(events))

    def test_remove_requires_confirmation_before_mutation(self):
        account = _account()
        view = FakeView(confirm=False)
        mutation = FakeMutation()

        code, events = _run(
            {
                "apiVersion": "gateway-account-host.openusage/v1",
                "action": "gatewayAccount.openRemove",
                "displayId": account.display_id,
            },
            store=FakeStore(GatewayConfig(accounts=(account,))),
            view=view,
            mutation=mutation,
        )

        self.assertEqual(code, 0)
        self.assertEqual(events[-1], {
            "version": 1,
            "event": "terminal",
            "state": "cancelled",
            "code": "cancelled",
        })
        self.assertEqual(mutation.requests, [])

        view = FakeView(confirm=True)
        code, events = _run(
            {
                "apiVersion": "gateway-account-host.openusage/v1",
                "action": "gatewayAccount.openRemove",
                "displayId": account.display_id,
            },
            store=FakeStore(GatewayConfig(accounts=(account,))),
            view=view,
            mutation=mutation,
        )
        self.assertEqual(code, 0)
        self.assertEqual(events[-1]["state"], "succeeded")
        self.assertEqual(mutation.requests, [{
            "version": 1,
            "action": "remove_account",
            "account": {"displayId": account.display_id},
            "credentialMaterial": {},
        }])

    def test_missing_display_id_and_ambiguous_display_id_are_zero_write(self):
        account = _account()
        mutation = FakeMutation()

        code, events = _run(
            {
                "apiVersion": "gateway-account-host.openusage/v1",
                "action": "gatewayAccount.openEdit",
                "displayId": "acct_000000000000",
            },
            store=FakeStore(GatewayConfig(accounts=(account,))),
            mutation=mutation,
        )

        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["code"], "not_found")
        self.assertEqual(mutation.requests, [])

        duplicate_config = type("DuplicateConfig", (), {"accounts": (account, account)})()
        code, events = _run(
            {
                "apiVersion": "gateway-account-host.openusage/v1",
                "action": "gatewayAccount.openRemove",
                "displayId": account.display_id,
            },
            store=FakeStore(duplicate_config),
            mutation=mutation,
        )

        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["code"], "not_found")
        self.assertEqual(mutation.requests, [])

    def test_mutation_backend_codes_are_mapped_to_terminal_allowlist(self):
        account = _account()
        mutation = FakeMutation({
            "version": 1,
            "ok": False,
            "code": "credential_write_failed",
        })

        code, events = _run(
            {
                "apiVersion": "gateway-account-host.openusage/v1",
                "action": "gatewayAccount.openReplace",
                "displayId": account.display_id,
            },
            store=FakeStore(GatewayConfig(accounts=(account,))),
            mutation=mutation,
        )

        self.assertEqual(code, 1)
        self.assertEqual(events[-1], {
            "version": 1,
            "event": "terminal",
            "state": "failed",
            "code": "credential_unavailable",
        })

        mutation = FakeMutation({
            "version": 1,
            "ok": False,
            "code": "pool_references_account",
        })
        code, events = _run(
            {
                "apiVersion": "gateway-account-host.openusage/v1",
                "action": "gatewayAccount.openRemove",
                "displayId": account.display_id,
            },
            store=FakeStore(GatewayConfig(accounts=(account,))),
            mutation=mutation,
        )

        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["code"], "account_in_use")

if __name__ == "__main__":
    unittest.main()
