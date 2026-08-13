import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from openusage_bar.gateway.accounts import AccountState, ProviderAccountRef
from openusage_bar.gateway.config import GatewayConfig, GatewayConfigStore


SECRET1 = "sk-ci-openai-account-smoke-secret-one"
SECRET2 = "sk-ci-openai-account-smoke-secret-two"


def smoke_credentials():
    return importlib.import_module("scripts.smoke_gateway_account_credentials")


@contextmanager
def chdir(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def make_executable(path: Path, *, os_name: str = os.name) -> Path:
    if os_name == "nt" and path.suffix.casefold() != ".exe":
        path = path.with_name(f"{path.name}.exe")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


class FakeKeychain:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.deletes: list[str] = []

    def get(self, account: str) -> str | None:
        return self.values.get(account)

    def set(self, account: str, secret: str) -> None:
        self.values[account] = secret

    def delete(self, account: str) -> None:
        self.deletes.append(account)
        self.values.pop(account, None)


class GatewayAccountCredentialSmokeTests(unittest.TestCase):
    def test_macos_smoke_delegates_native_credential_checks_to_packaged_mutations(self) -> None:
        module = smoke_credentials()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            helper = make_executable(
                Path(directory) / "dist-settings" / "openusage-settings"
            )
            with patch.object(module.sys, "platform", "darwin"), patch.object(
                module,
                "default_keychain",
                side_effect=AssertionError("must not use a foreign reader identity"),
            ) as default:
                with self.assertRaises(module.SmokeFailure):
                    module.run_smoke(
                        helper,
                        secret1="",
                        secret2=SECRET2,
                        nonce="native-delegation",
                    )

        default.assert_not_called()

    def test_macos_smoke_runs_one_packaged_credential_roundtrip_process(self) -> None:
        module = smoke_credentials()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            helper = make_executable(
                Path(directory) / "dist-settings" / "openusage-settings"
            )
            store = GatewayConfigStore(Path(directory) / "gateway.json")
            keychain_path = Path(directory) / "openusage-ci.keychain-db"
            keychain_path.write_bytes(b"private-keychain")
            keychain_path.chmod(0o600)
            calls: list[tuple[list[str], dict[str, object]]] = []

            def runner(command, **kwargs):
                calls.append((command, kwargs))
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout='{"version":1,"ok":true,"code":"ok"}\n',
                    stderr="",
                )

            with patch.object(module.sys, "platform", "darwin"), patch.object(
                module,
                "default_keychain",
                side_effect=AssertionError("must not use a foreign reader identity"),
            ) as default:
                report = module.run_smoke(
                    helper,
                    store=store,
                    macos_keychain_path=str(keychain_path),
                    command_runner=runner,
                    secret1=SECRET1,
                    secret2=SECRET2,
                    nonce="single-process",
                )

        self.assertEqual(report, {"version": 1, "ok": True, "code": "ok"})
        default.assert_not_called()
        self.assertEqual(len(calls), 1)
        command, kwargs = calls[0]
        self.assertEqual(
            command,
            [
                str(helper.resolve()),
                "__gateway-account-credential-roundtrip",
                str(keychain_path.resolve()),
            ],
        )
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs.get("stdout"), subprocess.PIPE)
        self.assertEqual(kwargs.get("stderr"), subprocess.PIPE)
        request = json.loads(kwargs["input"])
        self.assertEqual(
            request,
            {
                "version": 1,
                "providerId": "openai",
                "aliasCreate": "CI Smoke single-process",
                "aliasEdit": "CI Smoke single-process Edited",
                "credentialMaterial": {
                    "initialProviderKey": SECRET1,
                    "editedProviderKey": SECRET2,
                },
            },
        )

    def test_macos_smoke_rejects_untrusted_keychain_before_helper_launch(self) -> None:
        module = smoke_credentials()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            helper = make_executable(root / "dist-settings" / "openusage-settings")
            keychain_path = root / "openusage-ci.keychain-db"
            keychain_path.write_bytes(b"private-keychain")
            keychain_path.chmod(0o644)
            calls: list[object] = []

            with patch.object(module.sys, "platform", "darwin"):
                with self.assertRaises(module.SmokeFailure) as raised:
                    module.run_smoke(
                        helper,
                        macos_keychain_path=str(keychain_path),
                        command_runner=lambda *args, **kwargs: calls.append(
                            (args, kwargs)
                        ),
                        secret1=SECRET1,
                        secret2=SECRET2,
                        nonce="untrusted-keychain",
                    )

        self.assertEqual(raised.exception.code, "invalid_native_keychain")
        self.assertEqual(calls, [])
        self.assertNotIn(str(keychain_path), repr(raised.exception))

    def test_packaged_helper_fixture_uses_native_windows_exe_suffix(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            helper = make_executable(
                Path(directory) / "dist-settings" / "openusage-settings",
                os_name="nt",
            )

            self.assertEqual(helper.name, "openusage-settings.exe")
            self.assertTrue(helper.is_file())

    def test_script_entrypoint_runs_from_repository_root_with_safe_protocol_output(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [
                sys.executable,
                "scripts/smoke_gateway_account_credentials.py",
                "--settings-helper",
            ],
            cwd=repository_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            shell=False,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(
            completed.stdout,
            '{"version":1,"ok":false,"code":"invalid_settings_helper"}\n',
        )
        self.assertEqual(completed.stderr, "")

    def test_packaged_helper_roundtrip_verifies_native_keychain_and_config(self) -> None:
        module = smoke_credentials()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            helper = make_executable(Path(directory) / "dist-settings" / "openusage-settings")
            store = GatewayConfigStore(Path(directory) / "gateway.json")
            keychain = FakeKeychain()
            calls: list[dict[str, object]] = []

            def runner(command, **kwargs):
                self.assertEqual(command, [str(helper.resolve()), "gateway-account-mutate"])
                self.assertFalse(kwargs.get("shell", False))
                self.assertNotIn(SECRET1, command)
                self.assertNotIn(SECRET2, command)
                request = json.loads(kwargs["input"])
                calls.append(request)
                self.assertEqual(kwargs.get("stderr"), subprocess.PIPE)
                self.assertEqual(kwargs.get("stdout"), subprocess.PIPE)
                self.assertNotIn("env", kwargs)
                action = request["action"]
                if action == "create_account":
                    self.assertEqual(request["account"]["alias"], "CI Smoke roundtrip")
                    account = ProviderAccountRef(
                        provider_id="openai",
                        account_id="account-ci-roundtrip",
                        alias=request["account"]["alias"],
                        credential_account="openai.account-ci-roundtrip.gateway-api-key",
                    )
                    keychain.set(account.credential_account, request["credentialMaterial"]["providerKey"])
                    store.save(GatewayConfig(accounts=(account,)))
                    payload = {
                        "version": 1,
                        "ok": True,
                        "code": "ok",
                        "account": account.to_public_dict(state=AccountState.UNKNOWN),
                    }
                elif action == "edit_account":
                    self.assertEqual(request["account"]["alias"], "CI Smoke roundtrip Edited")
                    current = store.load()
                    account = current.accounts[0]
                    edited = ProviderAccountRef(
                        provider_id=account.provider_id,
                        account_id=account.account_id,
                        alias=request["account"]["alias"],
                        credential_account=account.credential_account,
                    )
                    keychain.set(edited.credential_account, request["credentialMaterial"]["providerKey"])
                    store.save(GatewayConfig(accounts=(edited,)))
                    payload = {
                        "version": 1,
                        "ok": True,
                        "code": "ok",
                        "account": edited.to_public_dict(state=AccountState.UNKNOWN),
                    }
                elif action == "remove_account":
                    current = store.load()
                    account = current.accounts[0]
                    keychain.delete(account.credential_account)
                    store.save(GatewayConfig(accounts=()))
                    payload = {
                        "version": 1,
                        "ok": True,
                        "code": "ok",
                        "account": account.to_public_dict(state=AccountState.UNKNOWN),
                    }
                else:
                    raise AssertionError(action)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(payload, separators=(",", ":")) + "\n",
                    stderr="",
                )

            report = module.run_smoke(
                helper,
                store=store,
                keychain=keychain,
                command_runner=runner,
                secret1=SECRET1,
                secret2=SECRET2,
                nonce="roundtrip",
            )

            self.assertEqual(report, {"version": 1, "ok": True, "code": "ok"})
            self.assertEqual([call["action"] for call in calls], [
                "create_account",
                "edit_account",
                "remove_account",
            ])
            self.assertEqual(store.load().accounts, ())
            self.assertEqual(keychain.values, {})
            self.assertEqual(keychain.deletes, ["openai.account-ci-roundtrip.gateway-api-key"])
            rendered = json.dumps(report, sort_keys=True).casefold()
            for forbidden in (
                SECRET1,
                SECRET2,
                "account-ci-roundtrip",
                "gateway-api-key",
                "credential",
                "providerkey",
                str(Path(directory)),
            ):
                self.assertNotIn(forbidden.casefold(), rendered)

    def test_failures_emit_only_generic_code_and_try_cleanup(self) -> None:
        module = smoke_credentials()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            helper = make_executable(Path(directory) / "dist-settings" / "openusage-settings")
            account = ProviderAccountRef(
                provider_id="openai",
                account_id="account-private-failure",
                alias="CI Smoke failure",
                credential_account="openai.account-private-failure.gateway-api-key",
            )
            store = GatewayConfigStore(Path(directory) / "gateway.json")
            store.save(GatewayConfig(accounts=(account,)))
            keychain = FakeKeychain()
            keychain.set(account.credential_account, SECRET1)

            def runner(command, **kwargs):
                request = json.loads(kwargs["input"])
                if request["action"] == "remove_account":
                    keychain.delete(account.credential_account)
                    store.save(GatewayConfig(accounts=()))
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=json.dumps({
                            "version": 1,
                            "ok": True,
                            "code": "ok",
                            "account": account.to_public_dict(state=AccountState.UNKNOWN),
                        }),
                        stderr="",
                    )
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout='{"version":1,"ok":false,"code":"credential_write_failed"}\n',
                    stderr=f"private {SECRET1} {account.credential_account} {directory}",
                )

            stdout = io.StringIO()
            stderr = io.StringIO()
            code = module.main(
                [str(helper)],
                stdout=stdout,
                stderr=stderr,
                store=store,
                keychain=keychain,
                command_runner=runner,
                secret1=SECRET1,
                secret2=SECRET2,
                nonce="failure",
            )

            self.assertEqual(code, 1)
            self.assertEqual(
                json.loads(stdout.getvalue()),
                {"version": 1, "ok": False, "code": "credential_write_failed"},
            )
            rendered = (stdout.getvalue() + stderr.getvalue()).casefold()
            for forbidden in (
                SECRET1,
                SECRET2,
                account.account_id,
                account.credential_account,
                str(Path(directory)),
                "private",
            ):
                self.assertNotIn(forbidden.casefold(), rendered)
            self.assertEqual(keychain.values, {})
            self.assertEqual(store.load().accounts, ())

    def test_helper_path_accepts_safe_relative_or_absolute_without_leaking_invalid_path(self) -> None:
        module = smoke_credentials()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            helper = make_executable(root / "dist-settings" / "openusage-settings")
            relative_helper = helper.relative_to(root).as_posix()
            with chdir(root):
                self.assertEqual(
                    module.resolve_settings_helper(relative_helper),
                    helper.resolve(),
                )
            self.assertEqual(module.resolve_settings_helper(str(helper)), helper.resolve())

        stdout = io.StringIO()
        stderr = io.StringIO()
        code = module.main(["../private/openusage-settings"], stdout=stdout, stderr=stderr)

        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {"version": 1, "ok": False, "code": "invalid_settings_helper"},
        )
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("../private", stdout.getvalue())

    def test_helper_path_rejects_bare_relative_symlink_missing_and_posix_non_executable(self) -> None:
        module = smoke_credentials()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            target = make_executable(root / "dist-settings" / "real-helper")
            symlink = root / "dist-settings" / "symlink-helper"
            symlink.symlink_to(target)
            linked_parent = root / "linked-settings"
            linked_parent.symlink_to(root / "dist-settings")
            non_executable = root / "dist-settings" / "not-executable"
            non_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            candidates = [
                "openusage-settings",
                "dist-settings/missing-helper",
                "dist-settings/symlink-helper",
                "linked-settings/real-helper",
            ]
            if os.name != "nt":
                candidates.append("dist-settings/not-executable")
            with chdir(root):
                for candidate in candidates:
                    with self.subTest(candidate=candidate):
                        with self.assertRaises(module.SmokeFailure) as raised:
                            module.resolve_settings_helper(candidate)
                        self.assertEqual(raised.exception.code, "invalid_settings_helper")

    def test_helper_path_uses_windows_native_executable_contract(self) -> None:
        module = smoke_credentials()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            helper = root / "dist-settings" / "openusage-settings.exe"
            helper.parent.mkdir(parents=True, exist_ok=True)
            helper.write_text("packaged helper", encoding="utf-8")
            no_extension = root / "dist-settings" / "openusage-settings"
            no_extension.write_text("not native", encoding="utf-8")
            symlink = root / "dist-settings" / "linked-helper.exe"
            symlink.symlink_to(helper)
            linked_parent = root / "linked-settings"
            linked_parent.symlink_to(root / "dist-settings")

            with patch.object(module.sys, "platform", "win32"), chdir(root):
                self.assertEqual(
                    module.resolve_settings_helper("dist-settings/openusage-settings.exe"),
                    helper.resolve(),
                )
                for candidate in (
                    "openusage-settings.exe",
                    "dist-settings/openusage-settings",
                    "dist-settings/linked-helper.exe",
                    "linked-settings/openusage-settings.exe",
                    "dist-settings/missing-helper.exe",
                    "//server/share/openusage-settings.exe",
                    "dist-settings/bad\nhelper.exe",
                ):
                    with self.subTest(candidate=candidate):
                        with self.assertRaises(module.SmokeFailure) as raised:
                            module.resolve_settings_helper(candidate)
                        self.assertEqual(raised.exception.code, "invalid_settings_helper")

    def test_defaults_generate_nonce_and_secrets_without_module_level_secret_constants(self) -> None:
        module = smoke_credentials()
        self.assertFalse(hasattr(module, "SECRET1"))
        self.assertFalse(hasattr(module, "SECRET2"))
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            helper = make_executable(Path(directory) / "dist-settings" / "openusage-settings")
            store = GatewayConfigStore(Path(directory) / "gateway.json")
            keychain = FakeKeychain()
            random_values = iter(("nonce-value", "generated-secret-one", "generated-secret-two"))
            seen_aliases: list[str] = []
            seen_secrets: list[str] = []

            def runner(command, **kwargs):
                request = json.loads(kwargs["input"])
                action = request["action"]
                if action in {"create_account", "edit_account"}:
                    seen_aliases.append(request["account"]["alias"])
                    seen_secrets.append(request["credentialMaterial"]["providerKey"])
                if action == "create_account":
                    account = ProviderAccountRef(
                        provider_id="openai",
                        account_id="account-generated",
                        alias=request["account"]["alias"],
                        credential_account="openai.account-generated.gateway-api-key",
                    )
                    keychain.set(account.credential_account, request["credentialMaterial"]["providerKey"])
                    store.save(GatewayConfig(accounts=(account,)))
                    payload_account = account
                elif action == "edit_account":
                    account = store.load().accounts[0]
                    payload_account = ProviderAccountRef(
                        provider_id=account.provider_id,
                        account_id=account.account_id,
                        alias=request["account"]["alias"],
                        credential_account=account.credential_account,
                    )
                    keychain.set(payload_account.credential_account, request["credentialMaterial"]["providerKey"])
                    store.save(GatewayConfig(accounts=(payload_account,)))
                else:
                    payload_account = store.load().accounts[0]
                    keychain.delete(payload_account.credential_account)
                    store.save(GatewayConfig(accounts=()))
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps({
                        "version": 1,
                        "ok": True,
                        "code": "ok",
                        "account": payload_account.to_public_dict(state=AccountState.UNKNOWN),
                    }),
                    stderr="",
                )

            module.run_smoke(
                helper,
                store=store,
                keychain=keychain,
                command_runner=runner,
                token_factory=lambda _bytes: next(random_values),
            )

            self.assertEqual(seen_aliases, [
                "CI Smoke nonce-value",
                "CI Smoke nonce-value Edited",
            ])
            self.assertEqual(seen_secrets, [
                "generated-secret-one",
                "generated-secret-two",
            ])

    def test_remove_cleanup_still_runs_when_post_remove_verification_fails(self) -> None:
        module = smoke_credentials()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            helper = make_executable(Path(directory) / "dist-settings" / "openusage-settings")
            store = GatewayConfigStore(Path(directory) / "gateway.json")
            keychain = FakeKeychain()
            remove_calls = 0

            def runner(command, **kwargs):
                nonlocal remove_calls
                request = json.loads(kwargs["input"])
                action = request["action"]
                if action == "create_account":
                    account = ProviderAccountRef(
                        provider_id="openai",
                        account_id="account-cleanup",
                        alias=request["account"]["alias"],
                        credential_account="openai.account-cleanup.gateway-api-key",
                    )
                    keychain.set(account.credential_account, request["credentialMaterial"]["providerKey"])
                    store.save(GatewayConfig(accounts=(account,)))
                    payload_account = account
                elif action == "edit_account":
                    account = store.load().accounts[0]
                    payload_account = ProviderAccountRef(
                        provider_id=account.provider_id,
                        account_id=account.account_id,
                        alias=request["account"]["alias"],
                        credential_account=account.credential_account,
                    )
                    keychain.set(payload_account.credential_account, request["credentialMaterial"]["providerKey"])
                    store.save(GatewayConfig(accounts=(payload_account,)))
                else:
                    remove_calls += 1
                    payload_account = store.load().accounts[0]
                    if remove_calls > 1:
                        keychain.delete(payload_account.credential_account)
                        store.save(GatewayConfig(accounts=()))
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps({
                        "version": 1,
                        "ok": True,
                        "code": "ok",
                        "account": payload_account.to_public_dict(state=AccountState.UNKNOWN),
                    }),
                    stderr="",
                )

            with self.assertRaises(module.SmokeFailure) as raised:
                module.run_smoke(
                    helper,
                    store=store,
                    keychain=keychain,
                    command_runner=runner,
                    secret1=SECRET1,
                    secret2=SECRET2,
                    nonce="cleanup",
                )

            self.assertEqual(raised.exception.code, "config_invalid")
            self.assertEqual(remove_calls, 2)
            self.assertEqual(store.load().accounts, ())
            self.assertEqual(keychain.values, {})

    def test_helper_output_rejects_duplicate_keys_nan_and_non_exact_display_id(self) -> None:
        module = smoke_credentials()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            helper = make_executable(Path(directory) / "dist-settings" / "openusage-settings")
            store = GatewayConfigStore(Path(directory) / "gateway.json")
            keychain = FakeKeychain()
            invalid_outputs = (
                '{"version":1,"ok":true,"ok":false,"code":"ok","account":{}}\n',
                '{"version":1,"ok":true,"code":NaN,"account":{}}\n',
                json.dumps({
                    "version": 1,
                    "ok": True,
                    "code": "ok",
                    "account": {
                        "alias": "CI Smoke bad",
                        "displayId": "acct_nothex",
                        "providerId": "openai",
                        "state": "unknown",
                    },
                }),
            )

            for output in invalid_outputs:
                with self.subTest(output=output):
                    def runner(command, **kwargs):
                        return subprocess.CompletedProcess(
                            command,
                            0,
                            stdout=output,
                            stderr="",
                        )

                    with self.assertRaises(module.SmokeFailure) as raised:
                        module.run_smoke(
                            helper,
                            store=store,
                            keychain=keychain,
                            command_runner=runner,
                            secret1=SECRET1,
                            secret2=SECRET2,
                            nonce="bad",
                        )
                    self.assertEqual(raised.exception.code, "settings_helper_failed")

    def test_missing_settings_helper_option_is_sanitized_without_argparse_stderr(self) -> None:
        module = smoke_credentials()
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = module.main(["--settings-helper"], stdout=stdout, stderr=stderr)

        self.assertEqual(code, 1)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {"version": 1, "ok": False, "code": "invalid_settings_helper"},
        )


if __name__ == "__main__":
    unittest.main()
